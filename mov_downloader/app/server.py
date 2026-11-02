"""影片下載器 — 本機網頁伺服器（僅監聽 127.0.0.1）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import downloader as dl
from . import subtitles as subs
from . import translator
from . import errlog
from . import summary as sm
from . import transcribe as asr
from .fsutil import atomic_write_json

# 版本號唯一來源：專案根目錄的 VERSION（1.24.0 起；介面、CHANGELOG 都以它為準）
try:
    __version__ = (dl.ROOT / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    __version__ = "0.0.0"
PORT = int(os.environ.get("MOVDL_PORT", "8765"))
STATIC = dl.ROOT / "static"

app = Flask(__name__, static_folder=None)
errlog.migrate_old()
queue = dl.JobQueue()


@app.errorhandler(Exception)
def _unhandled(e):
    """未預期的程式錯誤：寫入 error.log，回傳中文訊息（404 等 HTTP 錯誤照原樣回傳）"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    errlog.error("伺服器", f"{request.method} {request.path} 發生未預期的錯誤", e)
    return jsonify({"ok": False, "error": f"程式發生錯誤：{type(e).__name__} {str(e)[:200]}（詳見 data\\error.log）"}), 500

URL_RE = re.compile(r"https?://[^\s<>\"'，、]+", re.I)


# ------------------------------------------------------------------ 擴充功能配對（1.24.0，參考 transcribe）
# 原本任何 chrome-extension:// 來源都放行，電腦上別的擴充功能也能送 Cookie、加下載、甚至呼叫刪除影片的 API。
# 改為：第一次同步 Cookie 時記住那個擴充功能的 ID，之後只認它；而且擴充功能只能用下面這幾個 API。
PAIR_FILE = dl.DATA / "ext_pair.json"
EXT_PATHS = {"/api/cookies", "/api/enqueue", "/api/ping"}
EXT_MISMATCH = ("這個 Chrome 擴充功能和下載器配對的不是同一個（可能重新安裝或換了資料夾載入擴充功能）。"
                "請到下載器「設定 → 帳號授權」按「解除配對」，再回擴充功能按「同步 Cookie」重新配對。")


def paired_ext() -> dict:
    try:
        return json.loads(PAIR_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ext_id(origin: str) -> str:
    return origin[len("chrome-extension://"):].strip("/")


@app.before_request
def guard_origin():
    """阻擋其他網站對本機 API 的跨站請求（僅允許本頁與已配對的 Chrome 擴充功能）。"""
    origin = request.headers.get("Origin", "")
    if origin.startswith("chrome-extension://"):
        if request.path not in EXT_PATHS:
            return jsonify({"ok": False, "error": "擴充功能不能使用這個功能"}), 403
        if request.method == "OPTIONS" or request.path == "/api/ping":
            return
        ext, paired = _ext_id(origin), paired_ext().get("id")
        if not paired:
            if request.path != "/api/cookies":
                return jsonify({"ok": False, "error": "擴充功能尚未配對：請先在擴充功能按「同步 Cookie」"}), 403
            atomic_write_json(PAIR_FILE, {"id": ext, "paired": time.time()})   # 第一次同步 → 自動配對
            errlog.warn("Cookie", f"已和 Chrome 擴充功能配對（ID：{ext}）")
        elif paired != ext:
            errlog.warn("Cookie", f"拒絕未配對的擴充功能 {ext}（已配對：{paired}）")
            return jsonify({"ok": False, "error": EXT_MISMATCH}), 403
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not origin:
        return
    if not re.match(rf"^https?://(127\.0\.0\.1|localhost):{PORT}$", origin):
        abort(403)


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    if origin.startswith("chrome-extension://") and request.path in EXT_PATHS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ------------------------------------------------------------------ 頁面
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    if name == "app.js":   # 填入版本號（瀏覽器若還留著舊版 app.js，會和伺服器版本不同而提示）
        js = (STATIC / "app.js").read_text(encoding="utf-8").replace("__APP_VERSION__", __version__, 1)
        return Response(js, mimetype="text/javascript; charset=utf-8", headers={"Cache-Control": "no-cache"})
    return send_from_directory(STATIC, name)


# ------------------------------------------------------------------ 隱藏影片（密碼鎖）
import hashlib
import hmac
import secrets

HIDDEN_TAGS = {"隱藏", "hidden"}
LOCK_FILE = dl.DATA / "lock.json"
TOKENS: dict[str, float] = {}          # 解鎖憑證 → 到期時間（只存在記憶體，重新啟動即上鎖）
UNLOCK_TTL = 2 * 3600


def _hash_pw(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000).hex()


def has_password() -> bool:
    return LOCK_FILE.exists()


def check_password(pw: str) -> bool:
    try:
        d = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        return hmac.compare_digest(_hash_pw(pw, bytes.fromhex(d["salt"])), d["hash"])
    except Exception:
        return False


def is_unlocked() -> bool:
    tok = request.cookies.get("movdl_unlock", "")
    exp = TOKENS.get(tok)
    if exp and exp > time.time():
        TOKENS[tok] = time.time() + UNLOCK_TTL      # 有在使用就延長
        return True
    TOKENS.pop(tok, None)
    return False


def is_hidden_meta(meta: dict) -> bool:
    return any(str(t).lower() in HIDDEN_TAGS for t in meta.get("hashtags") or [])


IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def slide_owner(name: str, stems: set[str]) -> tuple[str, int] | None:
    """「影片標題01.png」這類圖片摘要 → (影片檔名主體, 頁碼)；頁碼是檔名結尾 1～2 位數字"""
    m = re.match(r"^(.*?)(\d{1,2})(\.(?:png|jpe?g|webp))$", name, re.I)
    if not m:
        return None
    head, num = name[:-len(m.group(3))], None
    for k in (1, 2):                                  # 先試結尾 1 位數字（主體較長），再試 2 位數字
        stem, digits = head[:-k], head[-k:]
        if digits.isdigit() and stem in stems:
            return stem, int(digits)
    return None


def video_of_image(p: Path) -> Path | None:
    stems = {x.stem: x for x in p.parent.iterdir() if x.suffix.lower() in dl.VIDEO_EXTS}
    o = slide_owner(p.name, set(stems))
    return stems[o[0]] if o else None


def hidden_blocked(p: Path) -> bool:
    """這個檔案屬於隱藏影片、且目前尚未解鎖"""
    base = p
    if p.suffix.lower() == ".srt":
        stem = p.name.split(".")[0]
        cand = [x for x in p.parent.iterdir() if x.suffix.lower() in dl.VIDEO_EXTS and p.name.startswith(x.stem + ".")]
        base = cand[0] if cand else p
    elif p.suffix.lower() in IMG_EXTS:
        base = video_of_image(p) or p
    return is_hidden_meta(dl.read_meta(base)) and not is_unlocked()


@app.get("/api/lock/status")
def lock_status():
    return jsonify({"has_password": has_password(), "unlocked": is_unlocked()})


@app.post("/api/lock/set")
def lock_set():
    d = request.get_json(silent=True) or {}
    new = str(d.get("new", ""))
    if len(new) < 4:
        return jsonify({"ok": False, "error": "密碼至少 4 個字元"}), 400
    if has_password() and not check_password(str(d.get("old", ""))):
        time.sleep(1)
        return jsonify({"ok": False, "error": "舊密碼不正確"}), 403
    salt = secrets.token_bytes(16)
    atomic_write_json(LOCK_FILE, {"salt": salt.hex(), "hash": _hash_pw(new, salt)})
    TOKENS.clear()
    return jsonify({"ok": True})


@app.post("/api/lock/unlock")
def lock_unlock():
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if not has_password():
        return jsonify({"ok": False, "error": "尚未設定密碼，請先到「設定 → 隱藏影片」設定"}), 400
    if not check_password(pw):
        time.sleep(1)       # 放慢猜密碼
        return jsonify({"ok": False, "error": "密碼錯誤"}), 403
    tok = secrets.token_urlsafe(24)
    TOKENS[tok] = time.time() + UNLOCK_TTL
    resp = jsonify({"ok": True})
    resp.set_cookie("movdl_unlock", tok, httponly=True, samesite="Strict")
    return resp


@app.post("/api/lock/lock")
def lock_lock():
    TOKENS.pop(request.cookies.get("movdl_unlock", ""), None)
    resp = jsonify({"ok": True})
    resp.delete_cookie("movdl_unlock")
    return resp


# ------------------------------------------------------------------ 佇列 API
@app.get("/api/jobs")
def jobs():
    lst = queue.jobs
    if not is_unlocked():
        base = dl.download_dir()
        lst = [j for j in lst if not (j.get("file") and is_hidden_meta(dl.read_meta(base / j["file"])))]
    base = dl.download_dir()
    for j in lst:      # 已完成的工作：星號以影片資訊檔為準（可能在影片庫改過）
        if j.get("status") == "done" and j.get("file") and (base / j["file"]).exists():
            j["starred"] = bool(dl.read_meta(base / j["file"]).get("starred"))
    return jsonify({"jobs": lst, "paused": queue.paused})


@app.post("/api/jobs")
def add_jobs():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    urls = list(data.get("urls") or []) + URL_RE.findall(text)
    seen, clean = set(), []
    for u in urls:
        u = u.strip().rstrip(".,;)")
        if u and u not in seen:
            seen.add(u)
            clean.append(u)
    if not clean:
        return jsonify({"error": "沒有找到有效的網址"}), 400
    # 佇列中已在排隊／下載中的相同網址不再重複加入
    busy = {dl.norm_url(j["url"]) for j in queue.jobs if j["status"] in ("queued", "running")}
    dup = [u for u in clean if dl.norm_url(u) in busy]
    clean = [u for u in clean if dl.norm_url(u) not in busy]
    # 已下載過的影片：仍加入佇列，執行時會自動略過（播放清單 hashtag 仍會補上）
    idx = dl.library_index()
    exists = [u for u in clean if dl.find_existing(url=u, idx=idx)]
    return jsonify({"added": queue.add(clean) if clean else [], "duplicates": dup, "existing": exists})


@app.post("/api/jobs/<jid>/<action>")
def job_action(jid, action):
    if action == "cancel":
        queue.cancel(jid)
    elif action == "retry":
        queue.retry(jid)
    elif action == "force":
        queue.retry(jid, force=True)
    elif action == "up":
        queue.move(jid, -1)
    elif action == "down":
        queue.move(jid, 1)
    elif action == "remove":
        queue.remove(jid)
    elif action == "star":
        return jsonify({"ok": True, "starred": queue.star(jid)})
    elif action == "cancel_children":
        return jsonify({"ok": True, "canceled": queue.cancel_children(jid)})
    else:
        abort(404)
    return jsonify({"ok": True})


@app.post("/api/queue/<action>")
def queue_action(action):
    if action == "pause":
        queue.set_paused(True)
    elif action == "resume":
        queue.set_paused(False)
    elif action == "clear":
        queue.clear_finished()
    else:
        abort(404)
    return jsonify({"ok": True, "paused": queue.paused})


# ------------------------------------------------------------------ 訂閱清單
def _subs_view() -> list[dict]:
    out = []
    for x in dl.load_subs():
        scanning = any(j.get("sub_id") == x["id"] and j.get("is_scan") and j["status"] in ("queued", "running")
                       for j in queue.jobs)
        pending = sum(1 for j in queue.jobs if j.get("sub_id") == x["id"] and not j.get("is_scan")
                      and j["status"] in ("queued", "running"))
        out.append({k: v for k, v in x.items() if k != "seen"} | {"scanning": scanning, "pending": pending,
                                                                    "seen_count": len(x.get("seen") or [])})
    return out


@app.get("/api/subs")
def subs_list():
    return jsonify({"subs": _subs_view()})


@app.post("/api/subs")
def subs_add():
    d = request.get_json(silent=True) or {}
    urls = URL_RE.findall(str(d.get("text", ""))) + list(d.get("urls") or [])
    if not urls:
        return jsonify({"error": "請貼上播放清單或頻道的網址"}), 400
    import uuid
    subs_all = dl.load_subs()
    have = {dl.norm_url(x["url"]) for x in subs_all}
    added = []
    for u in urls:
        u = u.strip().rstrip(".,;)")
        if dl.norm_url(u) in have:
            continue
        have.add(dl.norm_url(u))
        sub = {"id": uuid.uuid4().hex[:8], "url": u, "title": str(d.get("title") or "")[:100], "added": time.time(),
               "track_only": bool(d.get("track_only")), "star": bool(d.get("star")),
               "fill": bool(d.get("fill")), "seen": [],
               "last_scan": None, "last_new": None, "last_total": None, "last_error": "", "initialized": False}
        subs_all.append(sub)
        added.append(sub)
    dl.save_subs(subs_all)
    for sub in added:           # 加入後立刻掃描一次
        queue.add_scan(sub)
    return jsonify({"ok": True, "added": len(added), "duplicates": len(urls) - len(added), "subs": _subs_view()})


@app.post("/api/subs/scan")
def subs_scan():
    """一鍵掃描：ids 省略時掃描全部"""
    d = request.get_json(silent=True) or {}
    ids = d.get("ids")
    fill = bool(d.get("fill"))       # 補齊：不管以前看過沒有，影片庫沒有的都下載
    n = 0
    for sub in dl.load_subs():
        if ids is None or sub["id"] in ids:
            if queue.add_scan(sub, fill=fill):
                n += 1
    return jsonify({"ok": True, "started": n, "paused": queue.paused, "subs": _subs_view()})


@app.post("/api/subs/<sid>")
def subs_update(sid):
    d = request.get_json(silent=True) or {}
    subs_all = dl.load_subs()
    if d.get("remove"):
        subs_all = [x for x in subs_all if x["id"] != sid]
    else:
        for x in subs_all:
            if x["id"] == sid:
                if "star" in d:
                    x["star"] = bool(d["star"])
                if "fill" in d:
                    x["fill"] = bool(d["fill"])
                if "no_summary" in d:
                    x["no_summary"] = bool(d["no_summary"])
                if "title" in d and str(d["title"]).strip():
                    x["title"] = str(d["title"]).strip()[:100]
    dl.save_subs(subs_all)
    return jsonify({"ok": True, "subs": _subs_view()})


# ------------------------------------------------------------------ 設定 / 環境
@app.get("/api/settings")
def get_settings():
    return jsonify(dl.load_settings())


@app.post("/api/settings")
def set_settings():
    return jsonify(dl.save_settings(request.get_json(silent=True) or {}))


@app.get("/api/env")
def env():
    import yt_dlp
    s = dl.load_settings()
    return jsonify({"version": __version__, "ytdlp": yt_dlp.version.__version__,
                    "ffmpeg": dl.find_ffmpeg(s), "download_dir": str(dl.download_dir(s)),
                    "cookies": dl.cookie_status(), "ext_pair": paired_ext(), "platform": sys.platform,
                    "llm": translator.llm_status()})


@app.post("/api/translator/diagnose")
def translator_diagnose():
    try:
        return jsonify(translator.diagnose())
    except Exception as e:
        errlog.error("AI 連線", "連線診斷程式發生錯誤", e)
        return jsonify({"ok": False, "steps": [{"name": "診斷程式", "status": "fail", "detail": str(e)[:300]}]})


@app.delete("/api/llm-mask")
def llm_mask_clear():
    """清除程式自動學到的遮蔽詞（.env 的 LLM_MASK_WORDS 不受影響）"""
    translator.MASK_FILE.unlink(missing_ok=True)
    translator._probed.clear()
    return jsonify({"ok": True})


@app.post("/api/translator/test")
def translator_test():
    try:
        return jsonify(translator.test_llm())
    except Exception as e:
        errlog.error("AI 連線", f"AI 翻譯測試失敗：{e}")
        return jsonify({"ok": False, "error": str(e)[:500]})


@app.post("/api/update-ytdlp")
def update_ytdlp():
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default,curl-cffi]"],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    if not ok:
        errlog.error("系統", "更新 yt-dlp 失敗\n" + (r.stdout + r.stderr)[-1500:])
    return jsonify({"ok": ok, "log": (r.stdout + r.stderr)[-1500:],
                    "note": "更新完成，請關閉並重新啟動程式以套用新版。" if ok else "更新失敗"})


@app.post("/api/open-folder")
def open_folder():
    p = dl.download_dir()
    try:
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        errlog.error("系統", f"無法開啟下載資料夾 {p}", e)
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 錯誤紀錄
@app.get("/api/errors")
def errors_list():
    items = errlog.read(int(request.args.get("limit", 300)), request.args.get("category", ""),
                        request.args.get("level", ""))
    size = errlog.LOG.stat().st_size if errlog.LOG.exists() else 0
    return jsonify({"items": items, "categories": errlog.CATEGORIES, "path": str(errlog.LOG), "size": size})


@app.post("/api/errors/clear")
def errors_clear():
    errlog.clear()
    return jsonify({"ok": True})


@app.post("/api/errors/open")
def errors_open():
    p = errlog.LOG
    if not p.exists():
        return jsonify({"ok": False, "error": "目前沒有錯誤紀錄"})
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})


# ------------------------------------------------------------------ Cookie
@app.route("/api/cookies", methods=["POST", "OPTIONS"])
def recv_cookies():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    cookies = data.get("cookies") or []
    if data.get("source") == "private":
        # 無痕視窗同步：只留 YouTube／Google 的 cookie，且必須是登入狀態
        yt = [c for c in cookies if dl._YT_DOMAIN.search(str(c.get("domain", "")).lstrip("."))]
        names = {c.get("name") for c in yt if str(c.get("domain", "")).lstrip(".").endswith("youtube.com")}
        if "LOGIN_INFO" not in names:
            return jsonify({"ok": False, "error": "無痕視窗裡還沒登入 YouTube：請在無痕視窗開 youtube.com 並登入會員帳號後再同步"}), 400
        n = dl.write_extension_cookies(yt, dl.YT_COOKIE_FILE)
        return jsonify({"ok": True, "count": n, "source": "private"})
    n = dl.write_extension_cookies(cookies)
    return jsonify({"ok": True, "count": n})


@app.post("/api/ext/unpair")
def ext_unpair():
    """解除擴充功能配對：下一個按「同步 Cookie」的擴充功能會自動配對"""
    PAIR_FILE.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.delete("/api/cookies/private")
def clear_private_cookies():
    dl.YT_COOKIE_FILE.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/api/ping", methods=["GET", "OPTIONS"])
def ping():
    return jsonify({"ok": True, "app": "mov_downloader", "version": __version__})


@app.route("/api/enqueue", methods=["POST", "OPTIONS"])
def ext_enqueue():
    """給擴充功能用：把目前分頁網址加入佇列。"""
    if request.method == "OPTIONS":
        return ("", 204)
    return add_jobs()


@app.get("/api/cookies/status")
def cookie_status():
    return jsonify(dl.cookie_status())


@app.post("/api/cookies/test")
def cookie_test():
    d = request.get_json(silent=True) or {}
    return jsonify(dl.test_browser_cookies(d.get("browser", "chrome"), d.get("profile", "")))


# ------------------------------------------------------------------ 影片庫
def _safe(name: str) -> Path:
    base = dl.download_dir().resolve()
    p = (base / name).resolve()
    if base not in p.parents and p != base:
        abort(403)
    return p


def rel(p: Path) -> str:
    """影片庫裡的檔案代號：相對於下載資料夾的路徑（子資料夾用 / 分隔，例如 local_movies/影片.mp4）"""
    try:
        return p.resolve().relative_to(dl.download_dir().resolve()).as_posix()
    except ValueError:
        return p.name


LOCAL_TAG = "本地影片"


def library_dirs() -> list[tuple[Path, str]]:
    """影片庫要掃描的資料夾：下載資料夾本身＋設定的子資料夾 → [(資料夾, 檔名前綴)]"""
    base = dl.download_dir()
    out = [(base, "")]
    raw = str(dl.load_settings().get("library_subdirs") or "")
    seen = set()
    for part in re.split(r"[,，;；\n]+", raw):
        part = part.strip().strip('"').replace("\\", "/").strip("/")
        if not part or part in seen or ".." in part.split("/"):
            continue
        seen.add(part)
        d = base / part
        if d.is_dir():
            out.append((d, part + "/"))
    return out


@app.get("/api/library")
def library():
    base = dl.download_dir()
    items = []
    unlocked = is_unlocked()
    job_url = {j["file"]: j["url"] for j in queue.jobs if j.get("file")}
    nosum = no_summary_subs()
    for folder, pre in library_dirs():
        # 整個資料夾只掃描一次（影片很多時，逐部影片再找字幕會非常慢）
        videos, srts = [], []
        summaries: set[str] = set()
        images: list[str] = []
        with os.scandir(folder) as it:
            for e in it:
                if not e.is_file():
                    continue
                low = e.name.lower()
                if low.endswith(".srt"):
                    srts.append(e.name)
                elif low.endswith(IMG_EXTS):
                    images.append(e.name)
                elif low.endswith(".summary.json"):
                    summaries.add(e.name[:-len(".summary.json")])
                elif low.endswith(dl.VIDEO_EXTS) and not low.endswith(".part"):
                    videos.append((e.name, e.stat()))
        srts.sort()
        stems = sorted((Path(n).stem for n, _ in videos), key=len, reverse=True)
        by_stem: dict[str, list[str]] = {}
        for name in srts:        # 字幕歸給「檔名最長且相符」的影片
            owner = next((st for st in stems if name == st + ".srt" or name.startswith(st + ".")), None)
            if owner:
                by_stem.setdefault(owner, []).append(name)
        stem_set = set(stems)
        slides: dict[str, list[tuple[int, str]]] = {}
        for n in images:                     # 圖片摘要：「影片標題01.png」「影片標題02.png」…
            o = slide_owner(n, stem_set)
            if o:
                slides.setdefault(o[0], []).append((o[1], n))
        for name, st in sorted(videos, key=lambda x: x[1].st_mtime, reverse=True):
            p = folder / name
            stem = p.stem
            tracks = []
            for sn in by_stem.get(stem, []):
                mid = sn[len(stem):-4].lstrip(".")
                if mid.count(".") == 0:
                    lang = mid or "預設"
                    tracks.append({"file": pre + sn, "lang": lang, "label": lang_label(lang)})
            tracks.sort(key=lambda t: 0 if t["lang"] == "zh-TW" else 1 if t["lang"].startswith("zh-TW-") else 2)
            meta = dl.read_meta(p)
            if pre and not meta.get("local_tagged"):    # 子資料夾的本機影片：自動加上「本地影片」hashtag（只加一次，手動移除後不再加回）
                tags = meta.get("hashtags") or []
                if not any(str(t).lower() == LOCAL_TAG.lower() for t in tags):
                    tags.append(LOCAL_TAG)
                meta.update(hashtags=tags, local_tagged=True)
                try:
                    dl.write_meta(p, meta)
                except OSError:
                    pass
            hidden = is_hidden_meta(meta)
            if hidden and not unlocked:
                continue
            if not meta.get("source_url") and pre + name in job_url:
                meta["source_url"] = job_url[pre + name]
                meta.setdefault("webpage_url", job_url[pre + name])
            items.append({"file": pre + name, "folder": pre.rstrip("/"), "title": stem, "size": dl.fmt_size(st.st_size), "bytes": st.st_size,
                          "mtime": st.st_mtime, "subs": tracks, "meta": meta,
                          "untranslated": count_untranslated(base, stem, tracks), "hidden": hidden,
                          "summary": stem in summaries, "no_summary": summary_blocked_by(meta, nosum),
                          "slides": [pre + n for _, n in sorted(slides.get(stem, []))]})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"items": items, "dir": str(base), "unlocked": unlocked, "has_password": has_password()})


_UNTR_CACHE: dict[str, tuple] = {}


def count_untranslated(base: Path, stem: str, tracks: list[dict]) -> int:
    """中文字幕裡還是外文（沒翻成功）的句數；有快取，檔案沒變就不重算"""
    zh = next((t for t in tracks if t["lang"] == "zh-TW"), None)
    src = next((t for t in tracks if not t["lang"].startswith("zh-TW")), None)
    if not zh or not src:
        return 0
    zp, sp = base / zh["file"], base / src["file"]
    key = (zp.stat().st_mtime, sp.stat().st_mtime)
    hit = _UNTR_CACHE.get(str(zp))
    if hit and hit[0] == key:
        return hit[1]
    try:
        miss = subs.untranslated_lines(subs.parse_srt(sp.read_text(encoding="utf-8", errors="replace")),
                                       subs.parse_srt(zp.read_text(encoding="utf-8", errors="replace")))
        n = len(miss) if miss is not None else 0
    except Exception:
        n = 0
    _UNTR_CACHE[str(zp)] = (key, n)
    return n


LANG_NAMES = {"en": "英文", "ar": "阿拉伯文", "ja": "日文", "ko": "韓文", "es": "西班牙文", "fr": "法文",
              "de": "德文", "pt": "葡萄牙文", "ru": "俄文", "it": "義大利文", "hi": "印地文", "id": "印尼文",
              "th": "泰文", "vi": "越南文", "tr": "土耳其文", "nl": "荷蘭文", "pl": "波蘭文", "ms": "馬來文",
              "und": "原文", "orig": "原文", "預設": "字幕"}


def lang_label(lang: str) -> str:
    if lang == "zh-TW":
        return "正體中文"
    if lang.startswith("zh-TW-"):
        return f"中文＋{LANG_NAMES.get(lang[6:], lang[6:])}雙語"
    base = re.sub(r"-orig$", "", lang)
    return LANG_NAMES.get(base, LANG_NAMES.get(base.split("-")[0], lang))


TASKS: dict[str, dict] = {}   # 背景翻譯工作（播放器「翻譯成中文字幕」）


@app.post("/api/library/translate")
def library_translate():
    """在背景把影片庫中既有的外語字幕翻成正體中文，回傳工作 ID 供前端查詢進度。"""
    import uuid
    d = request.get_json(silent=True) or {}
    name = d.get("file", "")
    fill = d.get("mode") == "fill"
    p = _safe(name)
    running = next((t for t in TASKS.values() if t["file"] == rel(p) and t["status"] == "running"), None)
    if running:
        return jsonify({"ok": True, "task": running["id"]})
    stem = p.stem
    srts = [f for f in p.parent.glob("*.srt")
            if f.name.startswith(stem + ".") and not re.search(r"\.zh-TW(-[\w-]+)?\.srt$", f.name)]
    if not srts:
        return jsonify({"ok": False, "error": "找不到可翻譯的外語字幕"}), 400
    src = srts[0]
    hint = src.name[len(stem) + 1:-4]
    st = dl.load_settings()
    mode = st.get("en_mode", "translate")
    engine = st.get("translator", "google")
    meta = dl.read_meta(p)
    ctx = "\n".join(filter(None, [meta.get("original_title") or stem, (meta.get("description") or "")[:1200]]))
    n_lines = len(subs.parse_srt(src.read_text(encoding="utf-8", errors="replace")))
    tid = uuid.uuid4().hex[:10]
    task = {"id": tid, "file": rel(p), "status": "running", "progress": 0, "detail": "準備中…",
            "engine": "本機" if engine == "local" else "Codex" if engine == "codex" else "AI" if engine == "llm" and translator.llm_config() else "Google",
            "lines": n_lines, "mode": "fill" if fill else "full", "started": time.time(), "error": "", "files": []}
    TASKS[tid] = task

    def prog(frac, label=""):
        task["progress"] = round(frac * 100, 1)
        task["detail"] = label

    def run():
        try:
            outs = subs.process_subtitle(src, p.with_suffix(""), hint, "translate" if mode == "keep" else mode,
                                         st.get("canto_written", True), progress=prog,
                                         engine=engine, context=ctx, fill_only=fill)
            left = 0
            zp = p.with_suffix("").parent / (stem + ".zh-TW.srt")
            if zp.exists():
                miss = subs.untranslated_lines(subs.parse_srt(src.read_text(encoding="utf-8", errors="replace"))
                                               if src.exists() else [],
                                               subs.parse_srt(zp.read_text(encoding="utf-8", errors="replace")))
                left = len(miss or [])
            task.update(status="done", progress=100, files=[rel(o) for o in outs], untranslated=left,
                        detail=f"完成，仍有 {left} 句未翻譯" if left else "完成，全部翻譯成功")
        except Exception as e:
            if not isinstance(e, subs.TranslationFailed):   # TranslationFailed 已在翻譯時記錄
                errlog.error("翻譯", f"翻譯字幕失敗：{p.name}", e)
            task.update(status="error", error=str(e))
        task["finished"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "task": tid})


@app.post("/api/library/transcribe")
def library_transcribe():
    """在背景為沒有字幕的影片做語音辨識（MP4 → MP3 → Breeze-ASR-25 → SRT）"""
    import uuid
    d = request.get_json(silent=True) or {}
    p = _safe(d.get("file", ""))
    if not p.exists() or hidden_blocked(p):
        abort(404)
    running = next((t for t in TASKS.values() if t["file"] == rel(p) and t["status"] == "running"), None)
    if running:
        return jsonify({"ok": True, "task": running["id"]})
    st = dl.load_settings()
    problem = asr.check_model(dl.asr_model_dir(st))
    if problem:
        return jsonify({"ok": False, "error": problem + "。請到「設定 → 語音辨識」確認模型資料夾"}), 400
    meta = dl.read_meta(p)
    ctx = "\n".join(filter(None, [meta.get("original_title") or p.stem, (meta.get("description") or "")[:1200]]))
    tid = uuid.uuid4().hex[:10]
    task = {"id": tid, "file": rel(p), "status": "running", "progress": 0, "detail": "準備中…", "kind": "asr",
            "engine": "Breeze-ASR-25", "lines": 0, "mode": "asr", "started": time.time(), "error": "", "files": [],
            "cancel": False}
    TASKS[tid] = task

    def prog(frac, label=""):
        task["progress"] = round(frac * 100, 1)
        task["detail"] = label

    def run():
        try:
            outs = dl.run_asr(p, st, ctx, progress=prog, cancelled=lambda: task["cancel"])
            task.update(status="done", progress=100, files=[rel(o) for o in outs], detail="完成", untranslated=0)
        except asr.Cancelled:
            task.update(status="error", error="已取消語音辨識")
        except Exception as e:
            errlog.error("語音辨識", f"語音辨識失敗：{p.name}", e)
            task.update(status="error", error=str(e)[:400])
        task["finished"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "task": tid})


@app.post("/api/tasks/<tid>/cancel")
def task_cancel(tid):
    t = TASKS.get(tid)
    if t:
        t["cancel"] = True
    return jsonify({"ok": True})


# ---- 批次語音辨識：一次找出所有沒字幕的影片，在背景一部一部辨識
BATCH: dict = {"running": False, "cancel": False, "items": [], "started": None, "finished": None}


def videos_without_subs(include_hidden: bool) -> list[Path]:
    out = []
    for base, _ in library_dirs():
        names = [e.name for e in os.scandir(base) if e.is_file()]
        srt = [n for n in names if n.lower().endswith(".srt")]
        for n in sorted(names):
            if not n.lower().endswith(dl.VIDEO_EXTS) or n.lower().endswith(".part"):
                continue
            stem = Path(n).stem
            if any(x == stem + ".srt" or x.startswith(stem + ".") for x in srt):
                continue
            p = base / n
            meta = dl.read_meta(p)
            if meta.get("no_speech") or (not include_hidden and is_hidden_meta(meta)):
                continue          # 沒有聲音／沒人說話的影片不再重複嘗試
            out.append(p)
    return out


def _batch_view() -> dict:
    items = BATCH["items"]
    cur = next((i for i in items if i["status"] == "running"), None)
    c = lambda st: sum(1 for i in items if i["status"] == st)
    return {"running": BATCH["running"], "total": len(items), "done": c("done"), "failed": c("error"),
            "waiting": c("waiting"), "canceled": c("canceled"), "current": cur,
            "errors": [{"file": i["file"], "error": i["error"]} for i in items if i["status"] == "error"][:20],
            "elapsed": round((BATCH["finished"] or time.time()) - BATCH["started"], 1) if BATCH["started"] else 0,
            "missing": len(videos_without_subs(is_unlocked())) if not BATCH["running"] else None}


@app.get("/api/asr/batch")
def asr_batch_status():
    return jsonify(_batch_view())


@app.post("/api/asr/batch")
def asr_batch_start():
    """body: {files: [...]}；省略 files 時＝所有沒有字幕的影片"""
    if BATCH["running"]:
        return jsonify({"ok": False, "error": "批次語音辨識已經在執行中"}), 409
    d = request.get_json(silent=True) or {}
    st = dl.load_settings()
    problem = asr.check_model(dl.asr_model_dir(st))
    if problem:
        return jsonify({"ok": False, "error": problem + "。請到「設定 → 語音辨識」確認模型資料夾"}), 400
    if d.get("files"):
        targets = [p for p in (_safe(n) for n in d["files"]) if p.exists() and not hidden_blocked(p)]
    else:
        targets = videos_without_subs(is_unlocked())
    if not targets:
        return jsonify({"ok": False, "error": "沒有需要辨識的影片（每部影片都已經有字幕）"}), 400
    BATCH.update(running=True, cancel=False, started=time.time(), finished=None,
                 items=[{"file": rel(p), "status": "waiting", "progress": 0, "detail": "", "error": ""} for p in targets])

    def run():
        base = dl.download_dir()
        try:
            for it in BATCH["items"]:
                if BATCH["cancel"]:
                    it["status"] = "canceled"
                    continue
                p = base / it["file"]
                if not p.exists():
                    it.update(status="error", error="找不到影片檔")
                    continue
                it.update(status="running", detail="準備中…")
                meta = dl.read_meta(p)
                ctx = "\n".join(filter(None, [meta.get("original_title") or p.stem, (meta.get("description") or "")[:1200]]))

                def prog(frac, label="", it=it):
                    it["progress"] = round(frac * 100, 1)
                    it["detail"] = label
                try:
                    dl.run_asr(p, dl.load_settings(), ctx, progress=prog, cancelled=lambda: BATCH["cancel"])
                    it.update(status="done", progress=100, detail="完成")
                except asr.Cancelled:
                    it.update(status="canceled", detail="已取消")
                except Exception as e:
                    errlog.error("語音辨識", f"批次語音辨識失敗：{it.get('file') or it.get('name') or ''}", e)
                    it.update(status="error", error=str(e)[:300])
        finally:
            BATCH.update(running=False, finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, **_batch_view()})


@app.post("/api/asr/batch/cancel")
def asr_batch_cancel():
    BATCH["cancel"] = True
    return jsonify({"ok": True})


@app.get("/api/asr/status")
def asr_status():
    st = dl.load_settings()
    d = dl.asr_model_dir(st)
    from . import modelpaths
    t = str(modelpaths.path("turbo", st))
    return jsonify({"model_dir": d, "problem": asr.check_model(d), "installed": asr.is_installed(),
                    "devices": asr.devices() if asr.is_installed() else [],
                    "turbo_dir": t, "turbo_problem": asr.check_model(t), "asr_model": st.get("asr_model") or "auto"})


@app.post("/api/models/download")
def models_download():
    """在背景下載 Gemma 4（1.26.0）；body: {key: e2b|e4b|26b}"""
    from . import local_llm
    key = (request.get_json(silent=True) or {}).get("key", "")
    try:
        return jsonify({"ok": True, **local_llm.start_download(key)})
    except local_llm.LocalLLMError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.get("/api/models")
def models_status():
    """本機 AI 模型位置與各模型是否就緒（1.25.0）"""
    from . import modelpaths, local_llm
    return jsonify({**modelpaths.status(), "local": local_llm.status()})


# ------------------------------------------------------------------ 重點摘要
def _list_id(url: str) -> str:
    m = re.search(r"[?&]list=([\w-]+)", url or "")
    return m.group(1) if m else ""


def no_summary_subs() -> list[dict]:
    return [x for x in dl.load_subs() if x.get("no_summary")]


def summary_blocked_by(meta: dict, subs: list[dict] | None = None) -> str:
    """影片屬於設定「不做重點摘要」的訂閱時，回傳訂閱名稱；否則回傳空字串。
    依序比對：下載時記下的訂閱、播放清單 ID、播放清單名稱（舊影片沒有記錄訂閱時用）"""
    subs = no_summary_subs() if subs is None else subs
    if not subs:
        return ""
    pl = meta.get("playlist") or {}
    for x in subs:
        if meta.get("sub_id") and meta["sub_id"] == x["id"]:
            return x.get("title") or x["url"]
        lid = _list_id(x.get("url", ""))
        if lid and pl.get("id") == lid:
            return x.get("title") or x["url"]
        if x.get("title") and pl.get("title") and pl["title"].strip() == x["title"].strip():
            return x["title"]
    return ""


def summarize_video(p: Path, progress, cancelled) -> dict:
    """沒有字幕先語音辨識 → AI 產生重點摘要 → AI 建議的 hashtag 加進影片"""
    problem = sm.engine_problem(sm.engine_config())
    if problem:
        raise sm.SummaryError(problem)
    meta = dl.read_meta(p)
    if not sm.pick_source(p):
        st = dl.load_settings()
        problem = asr.check_model(dl.asr_model_dir(st))
        if problem:
            raise sm.SummaryError(f"這部影片沒有字幕，需要先語音辨識，但{problem}")
        ctx = "\n".join(filter(None, [meta.get("original_title") or p.stem, (meta.get("description") or "")[:1200]]))
        dl.run_asr(p, st, ctx, progress=lambda f, lb="": progress(f * 0.6, f"沒有字幕，先語音辨識：{lb}"),
                   cancelled=cancelled)
        if cancelled():
            raise asr.Cancelled()
        meta = dl.read_meta(p)
        base_frac, span = 0.6, 0.4
    else:
        base_frac, span = 0.0, 1.0
    res = sm.generate(p, meta, progress=lambda f, lb="": progress(base_frac + f * span, lb))
    # 摘要裡的 hashtag 只顯示在摘要頁面，不會改動影片本身的 hashtag
    return res


@app.post("/api/codex/test")
def codex_test():
    """設定頁「測試 Codex 連線」：可帶入畫面上尚未儲存的路徑與模型"""
    from . import codex_cli
    d = request.get_json(silent=True) or {}
    st = dl.load_settings()
    path = d.get("path", st.get("codex_path", ""))
    model = d.get("model", st.get("codex_model", ""))
    try:
        return jsonify(codex_cli.diagnose(path, model))
    except Exception as e:
        errlog.error("AI 連線", "Codex 連線測試發生錯誤", e)
        return jsonify({"ok": False, "steps": [{"name": "測試程式", "status": "fail", "detail": str(e)[:300]}]})


@app.post("/api/summary/untag")
def summary_untag():
    """移除舊版（v1.17.0～v1.18.0）產生摘要時自動加到影片上的 hashtag：
    影片的 hashtag 若和它摘要裡的 hashtag 相同就移除（#隱藏 不動）"""
    videos = removed = 0
    for base, e in ((b, e) for b, _ in library_dirs() for e in os.scandir(b)):
        if not e.name.lower().endswith(".summary.json"):
            continue
        stem = e.name[:-len(".summary.json")]
        p = next((base / (stem + ext) for ext in dl.VIDEO_EXTS if (base / (stem + ext)).exists()), None)
        if not p:
            continue
        s = sm.read_summary(p) or {}
        ai = {norm_tag(t).lower() for t in s.get("hashtags") or []}
        meta = dl.read_meta(p)
        tags = meta.get("hashtags") or []
        keep = [t for t in tags if t.lower() not in ai or t == "隱藏"]
        if len(keep) != len(tags):
            removed += len(tags) - len(keep)
            videos += 1
            meta["hashtags"] = keep
            dl.write_meta(p, meta)
    return jsonify({"ok": True, "videos": videos, "removed": removed})


@app.get("/summary.css")
def summary_css():
    return Response(sm.SLIDE_CSS, mimetype="text/css")


@app.get("/api/summary/<path:name>")
def summary_get(name):
    p = _safe(name)
    if not p.exists() or hidden_blocked(p):
        abort(404)
    s = sm.read_summary(p)
    if not s:
        return jsonify({"ok": False, "error": "這部影片還沒有重點摘要"}), 404
    return jsonify({"ok": True, "summary": s})


@app.get("/api/summary-export/<path:name>")
def summary_export(name):
    p = _safe(name)
    if not p.exists() or hidden_blocked(p):
        abort(404)
    s = sm.read_summary(p)
    if not s:
        abort(404)
    from urllib.parse import quote
    fn = f"{p.stem}_重點摘要.html"
    return Response(sm.export_html(s, dl.read_meta(p)), mimetype="text/html",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn)}"})


@app.post("/api/library/summarize")
def library_summarize():
    """單一影片產生重點摘要（背景執行，用 /api/tasks/<id> 查進度）"""
    import uuid
    d = request.get_json(silent=True) or {}
    p = _safe(d.get("file", ""))
    if not p.exists() or hidden_blocked(p):
        abort(404)
    running = next((t for t in TASKS.values()
                    if t["file"] == rel(p) and t["status"] == "running" and t.get("kind") == "summary"), None)
    if running:
        return jsonify({"ok": True, "task": running["id"]})
    tid = uuid.uuid4().hex[:10]
    task = {"id": tid, "file": rel(p), "status": "running", "progress": 0, "detail": "準備中…", "kind": "summary",
            "started": time.time(), "error": "", "files": [], "cancel": False}
    TASKS[tid] = task

    def prog(frac, label=""):
        task["progress"] = round(min(frac, 1) * 100, 1)
        task["detail"] = label

    def run():
        try:
            summarize_video(p, prog, lambda: task["cancel"])
            task.update(status="done", progress=100, detail="完成")
        except asr.Cancelled:
            task.update(status="error", error="已取消")
        except Exception as e:
            errlog.error("重點摘要", f"產生重點摘要失敗：{p.name}", e)
            task.update(status="error", error=str(e)[:400])
        task["finished"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "task": tid})


SUMBATCH: dict = {"running": False, "cancel": False, "items": [], "started": None, "finished": None}


def _sumbatch_view() -> dict:
    items = SUMBATCH["items"]
    c = lambda st: sum(1 for i in items if i["status"] == st)
    return {"running": SUMBATCH["running"], "total": len(items), "done": c("done"), "failed": c("error"),
            "skipped": c("skipped"), "waiting": c("waiting"),
            "current": next((i for i in items if i["status"] == "running"), None),
            "errors": [{"file": i["file"], "error": i["error"]} for i in items if i["status"] == "error"][:20],
            "elapsed": round((SUMBATCH["finished"] or time.time()) - SUMBATCH["started"], 1) if SUMBATCH["started"] else 0}


@app.get("/api/summary-batch")
def summary_batch_status():
    return jsonify(_sumbatch_view())


@app.post("/api/summary-batch")
def summary_batch_start():
    """body: {files: [...], force: false}；force=false 時已有摘要的影片會略過"""
    from . import translator as tr
    if SUMBATCH["running"]:
        return jsonify({"ok": False, "error": "批次重點摘要已經在執行中"}), 409
    problem = sm.engine_problem(sm.engine_config())
    if problem:
        return jsonify({"ok": False, "error": problem}), 400
    d = request.get_json(silent=True) or {}
    targets = [p for p in (_safe(n) for n in d.get("files") or []) if p.exists() and not hidden_blocked(p)]
    if not targets:
        return jsonify({"ok": False, "error": "請先勾選影片"}), 400
    force = bool(d.get("force"))
    nosum = no_summary_subs()

    def plan(p: Path) -> tuple[str, str]:
        sub = summary_blocked_by(dl.read_meta(p), nosum)
        if sub:
            return "skipped", f"訂閱「{sub}」設定不做摘要"
        if not force and sm.summary_path(p).exists():
            return "skipped", "已有摘要"
        return "waiting", ""
    items = []
    for p in targets:
        st, why = plan(p)
        items.append({"file": rel(p), "status": st, "progress": 0, "detail": why, "error": ""})
    if not any(i["status"] == "waiting" for i in items):
        return jsonify({"ok": False, "error": "所選影片都已有摘要，或屬於設定「不做重點摘要」的訂閱"}), 400
    SUMBATCH.update(running=True, cancel=False, started=time.time(), finished=None, items=items)

    def run():
        base = dl.download_dir()
        try:
            for it in SUMBATCH["items"]:
                if it["status"] == "skipped":
                    continue
                if SUMBATCH["cancel"]:
                    it["status"] = "canceled"
                    continue
                p = base / it["file"]
                it.update(status="running", detail="準備中…")

                def prog(frac, label="", it=it):
                    it["progress"] = round(min(frac, 1) * 100, 1)
                    it["detail"] = label
                try:
                    summarize_video(p, prog, lambda: SUMBATCH["cancel"])
                    it.update(status="done", progress=100, detail="完成")
                except asr.Cancelled:
                    it.update(status="canceled", detail="已取消")
                except Exception as e:
                    errlog.error("重點摘要", f"批次重點摘要失敗：{it['file']}", e)
                    it.update(status="error", error=str(e)[:300])
        finally:
            SUMBATCH.update(running=False, finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, **_sumbatch_view()})


@app.post("/api/summary-batch/cancel")
def summary_batch_cancel():
    SUMBATCH["cancel"] = True
    return jsonify({"ok": True})


@app.get("/api/tasks/<tid>")
def task_status(tid):
    t = TASKS.get(tid)
    if not t:
        return jsonify({"error": "找不到工作"}), 404
    return jsonify({**t, "elapsed": round((t.get("finished") or time.time()) - t["started"], 1)})


def queue_url(filename: str) -> str:
    return next((j["url"] for j in queue.jobs if j.get("file") == filename), "")


def _related(p: Path) -> list[Path]:
    """影片本身＋同名字幕＋資訊檔"""
    stem = p.stem
    out = [p]
    for f in p.parent.glob("*.srt"):
        if f.stem == stem or f.name.startswith(stem + "."):
            out.append(f)
    if dl.meta_path(p).exists():
        out.append(dl.meta_path(p))
    if p.with_suffix(".mp3").exists():        # 語音辨識時保留的 MP3
        out.append(p.with_suffix(".mp3"))
    if sm.summary_path(p).exists():            # 重點摘要
        out.append(sm.summary_path(p))
    stems = {x.stem for x in p.parent.iterdir() if x.suffix.lower() in dl.VIDEO_EXTS}
    for f in p.parent.iterdir():               # 圖片摘要（影片標題01.png…）
        if f.suffix.lower() in IMG_EXTS and f.name.startswith(stem):
            o = slide_owner(f.name, stems)
            if o and o[0] == stem:
                out.append(f)
    return out


@app.post("/api/library/rename")
def library_rename():
    d = request.get_json(silent=True) or {}
    p = _safe(d.get("file", ""))
    if not p.exists():
        return jsonify({"ok": False, "error": "找不到影片檔"}), 404
    new_stem = dl.safe_filename(d.get("title", ""))
    if dl.load_settings().get("big5_names", True):
        new_stem = dl.big5_name(new_stem) or new_stem
    if not new_stem:
        return jsonify({"ok": False, "error": "名稱不可空白"}), 400
    if new_stem == p.stem:
        return jsonify({"ok": True, "file": rel(p)})
    target = p.with_name(new_stem + p.suffix)
    if target.exists() and target.resolve() != p.resolve():
        return jsonify({"ok": False, "error": "已有同名影片，請換一個名稱"}), 409
    old_stem = p.stem
    done: list[tuple[Path, Path]] = []
    try:
        for f in _related(p):
            dst = f.with_name(new_stem + f.name[len(old_stem):])
            for i in range(15):          # Windows 上檔案可能仍被播放器占用，稍候重試
                try:
                    f.rename(dst)
                    break
                except PermissionError:
                    if i == 14:
                        raise
                    time.sleep(0.2)
            done.append((f, dst))
    except PermissionError:
        for src, dst in reversed(done):
            try:
                dst.rename(src)
            except Exception:
                pass
        return jsonify({"ok": False, "error": "檔案正在使用中（可能被其他播放器開啟），請關閉後再試"}), 409
    for j in queue.jobs:
        if j.get("file") == rel(p):
            j["file"] = rel(target)
            j["title"] = new_stem
    queue._save()
    return jsonify({"ok": True, "file": rel(target)})


def norm_tag(t: str) -> str:
    """#AI 工具 → AI_工具；去掉 #、空白改底線、最長 30 字"""
    t = re.sub(r"\s+", "_", str(t).strip().lstrip("#＃").strip())
    t = re.sub(r"[,，、;；#＃]", "", t)
    return t[:30]


@app.post("/api/library/starred")
def library_starred():
    """標示（或取消）星號。body: {files: [...], starred: true/false}"""
    d = request.get_json(silent=True) or {}
    on = bool(d.get("starred", True))
    n = 0
    for name in d.get("files") or []:
        p = _safe(name)
        if not p.exists() or hidden_blocked(p):
            continue
        meta = dl.read_meta(p)
        if bool(meta.get("starred")) != on:
            meta["starred"] = on
            dl.write_meta(p, meta)
            n += 1
        for j in queue.jobs:
            if j.get("file") == rel(p):
                j["starred"] = on
    return jsonify({"ok": True, "changed": n})


@app.post("/api/library/watched")
def library_watched():
    """標示（或取消）已看完。body: {files: [...], watched: true/false}"""
    d = request.get_json(silent=True) or {}
    watched = bool(d.get("watched", True))
    n = 0
    for name in d.get("files") or []:
        p = _safe(name)
        if not p.exists() or hidden_blocked(p):
            continue
        meta = dl.read_meta(p)
        if bool(meta.get("watched")) != watched:
            meta["watched"] = watched
            if watched:
                meta["watched_at"] = time.time()
            else:
                meta.pop("watched_at", None)
            dl.write_meta(p, meta)
            n += 1
    return jsonify({"ok": True, "changed": n})


@app.post("/api/library/hashtags")
def library_hashtags():
    """批次為多部影片加上／移除 hashtag。body: {files: [...], add: [...], remove: [...]}"""
    d = request.get_json(silent=True) or {}
    add = [x for x in (norm_tag(t) for t in d.get("add") or []) if x]
    remove = {norm_tag(t).lower() for t in d.get("remove") or []}
    changed = 0
    for name in d.get("files") or []:
        p = _safe(name)
        if not p.exists():
            continue
        meta = dl.read_meta(p)
        tags = [t for t in meta.get("hashtags") or [] if t.lower() not in remove]
        for t in add:
            if t.lower() not in {x.lower() for x in tags}:
                tags.append(t)
        if tags != (meta.get("hashtags") or []):
            meta["hashtags"] = tags
            dl.write_meta(p, meta)
            changed += 1
    return jsonify({"ok": True, "changed": changed})


@app.post("/api/library/delete-many")
def library_delete_many():
    files = (request.get_json(silent=True) or {}).get("files") or []
    n = 0
    for name in files:
        p = _safe(name)
        if p.exists():
            dl.add_history(dl.meta_keys(dl.read_meta(p), queue_url(rel(p))))
            for f in _related(p):
                f.unlink(missing_ok=True)
            n += 1
    return jsonify({"ok": True, "deleted": n})


@app.post("/api/library/meta")
def library_meta():
    """儲存備註，或為舊影片補抓原文簡介。"""
    d = request.get_json(silent=True) or {}
    p = _safe(d.get("file", ""))
    if not p.exists():
        return jsonify({"ok": False, "error": "找不到影片檔"}), 404
    if hidden_blocked(p):
        abort(404)
    meta = dl.read_meta(p)
    if d.get("url"):                       # 手動補登來源網址
        u = str(d["url"]).strip()
        if not re.match(r"^https?://", u):
            return jsonify({"ok": False, "error": "網址格式不正確"}), 400
        meta["source_url"] = u
        meta.setdefault("webpage_url", u)
        meta["site"] = dl.site_of(u)
    if not meta.get("source_url"):
        j = next((j for j in queue.jobs if j.get("file") == rel(p)), None)
        if j:
            meta["source_url"] = j["url"]
    if d.get("fetch"):
        url = meta.get("source_url") or meta.get("webpage_url")
        if not url:
            return jsonify({"ok": False, "error": "不知道這部影片的來源網址"}), 400
        try:
            fresh = dl.fetch_meta(url)
        except Exception as e:
            errlog.error("影片資訊", f"重新抓取影片資訊失敗：{url}", e)
            return jsonify({"ok": False, "error": dl.friendly_error(str(e))}), 500
        fresh["downloaded"] = meta.get("downloaded") or p.stat().st_mtime
        fresh["source_url"] = url
        meta = {**fresh, **{k: v for k, v in meta.items() if k in ("note", "hashtags", "watched", "watched_at", "starred")}}
    if "note" in d:
        meta["note"] = str(d["note"])[:20000]
    dl.write_meta(p, meta)
    return jsonify({"ok": True, "meta": meta})


@app.post("/api/library/delete")
def library_delete():
    name = (request.get_json(silent=True) or {}).get("file", "")
    p = _safe(name)
    removed = []
    if p.exists():
        dl.add_history(dl.meta_keys(dl.read_meta(p), queue_url(rel(p))))   # 記下「下載過」，訂閱補齊時不再抓回來
        stem = p.stem
        for f in list(p.parent.glob("*")):
            if f == p or f == dl.meta_path(p) or (f.suffix == ".srt" and (f.stem == stem or f.name.startswith(stem + "."))):
                f.unlink()
                removed.append(f.name)
    return jsonify({"ok": True, "removed": removed})


THUMBS = dl.DATA / "thumbs"
_thumb_sem = threading.Semaphore(2)      # 同時最多 2 個 ffmpeg 產生縮圖
_PLACEHOLDER = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 9"><rect width="16" height="9" fill="#111827"/>'
                b'<path d="M6.8 3.2v2.6L9.4 4.5z" fill="#475569"/></svg>')


def _ffbin(name: str) -> str | None:
    d = dl.find_ffmpeg()
    if not d:
        return None
    exe = Path(d) / (name + (".exe" if os.name == "nt" else ""))
    return str(exe) if exe.exists() else None


@app.get("/thumb/<path:name>")
def thumb(name):
    """影片縮圖（第一次用 ffmpeg 擷取一張 JPG 後快取，之後直接回傳，不再占用影片連線）"""
    p = _safe(name)
    if not p.exists() or hidden_blocked(p):
        abort(404)
    st = p.stat()
    key = hashlib.sha1(f"{rel(p)}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()[:20]
    THUMBS.mkdir(exist_ok=True)
    out = THUMBS / f"{key}.jpg"
    if not out.exists():
        ff = _ffbin("ffmpeg")
        if ff:
            with _thumb_sem:
                if not out.exists():
                    tmp = out.with_suffix(".tmp.jpg")
                    for seek in ("5", "0"):       # 先取第 5 秒，影片太短就取開頭
                        try:
                            subprocess.run([ff, "-v", "error", "-y", "-ss", seek, "-i", str(p), "-frames:v", "1",
                                            "-vf", "scale=480:-2", "-q:v", "5", str(tmp)],
                                           timeout=30, stdin=subprocess.DEVNULL,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        except Exception:
                            pass
                        if tmp.exists() and tmp.stat().st_size > 0:
                            os.replace(tmp, out)
                            break
    if out.exists():
        resp = send_from_directory(out.parent, out.name, conditional=True)
        resp.headers["Cache-Control"] = "private, max-age=604800"
        return resp
    return Response(_PLACEHOLDER, mimetype="image/svg+xml")


@app.get("/api/library/probe")
def library_probe():
    """播放失敗時查詢影片編碼，判斷是不是瀏覽器不支援的格式"""
    p = _safe(request.args.get("file", ""))
    if not p.exists() or hidden_blocked(p):
        abort(404)
    fp = _ffbin("ffprobe")
    info = {"video": "", "audio": "", "size": dl.fmt_size(p.stat().st_size)}
    if fp:
        try:
            r = subprocess.run([fp, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
                                "-of", "json", str(p)], capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for stt in json.loads(r.stdout or "{}").get("streams", []):
                if stt.get("codec_type") == "video" and not info["video"]:
                    info["video"] = f"{stt.get('codec_name')} {stt.get('width')}x{stt.get('height')}"
                elif stt.get("codec_type") == "audio" and not info["audio"]:
                    info["audio"] = stt.get("codec_name") or ""
        except Exception:
            pass
    return jsonify(info)


@app.get("/media/<path:name>")
def media(name):
    p = _safe(name)
    if hidden_blocked(p):
        abort(404)
    if not p.exists():
        abort(404)
    return send_from_directory(p.parent, p.name, conditional=True)


@app.get("/simg/<path:name>")
def slide_image(name):
    """圖片摘要的圖片"""
    p = _safe(name)
    if p.suffix.lower() not in IMG_EXTS or not p.exists() or hidden_blocked(p):
        abort(404)
    resp = send_from_directory(p.parent, p.name, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/vtt/<path:name>")
def vtt(name):
    p = _safe(name)
    if hidden_blocked(p):
        abort(404)
    if not p.exists():
        abort(404)
    text = p.read_text(encoding="utf-8", errors="replace")
    return Response(subs.srt_to_vtt(text), mimetype="text/vtt; charset=utf-8")


def _port_busy() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        return sk.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    url = f"http://127.0.0.1:{PORT}"
    if _port_busy():
        print(f"\n  [錯誤] 連接埠 {PORT} 已被占用，可能還有舊版下載器在執行。")
        print("  請關閉所有「影片下載器」黑色視窗後，再重新執行 start.bat。\n")
        sys.exit(1)
    print(f"\n  影片下載器 v{__version__} 已啟動：{url}\n  關閉此視窗即可結束程式。\n")
    if os.environ.get("MOVDL_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
