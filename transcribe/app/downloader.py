"""網址下載（階段 4，0.7.0）：用 yt-dlp 只下載音訊，再交給現有辨識流程。

- 呼叫專案 .venv 的 yt-dlp（子行程），取消時可立即停止。
- 只支援單一影片；播放清單、頻道、直播中的影片會先擋下。
- 只處理公開影片；需要登入的內容在下一階段（4-2）改用 cookies 重試。
- 下載直接存到 downloads\\<任務>\\（暫存名 media.*），完成後改成正體標題（Windows 安全檔名）。
- yt-dlp 背景自動更新，一天最多一次，失敗不影響使用。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Callable

from . import ROOT
from .config import find_ffmpeg
from .naming import clean_title
from .textout import to_tw

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_update_lock = threading.Lock()
UPDATE_STAMP = ROOT / "data" / "ytdlp_update.json"

# 下載 mp4 影片（給人觀看）：優先 H.264＋AAC（瀏覽器、Windows 播放器都能播），最高 1080p；
# 辨識直接讀 mp4 裡的聲音軌，不另存聲音檔（0.7.3，2026-11-01 使用者要求播放以影片為主）。
# 只有聲音的網站（沒有影片格式）最後會退回下載聲音。
FORMAT = ("bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
          "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
          "b[height<=1080][ext=mp4]/"
          "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b")


class DownloadError(Exception):
    """kind：login／drm／geo／invalid／unsupported／playlist／live／js／network／ratelimit／other"""

    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind, self.detail = kind, detail


class DownloadCancelled(Exception):
    pass


MESSAGES = {
    "playlist": "這是播放清單或頻道網址，目前只支援單一影片。請打開其中一部影片，再複製那部影片的網址。",
    "live": "這是直播中的影片，請等直播結束後再下載。",
    "login": "這部影片需要登入才能觀看（可能是私人、會員限定、有年齡限制，或被平台要求驗證）。請在 Chrome 安裝本專案的「逐字稿登入同步」擴充功能並按「立即同步」，再試一次（安裝方式見「參數設定」）。",
    "login_cookies": "已帶入你的登入資料仍無法下載：可能是這個帳號沒有觀看權限，或登入資料已過期。請先在 Chrome 開啟這部影片確認能播放，再按「逐字稿登入同步」的「立即同步」後重試。",
    "drm": "這個影片有 DRM 防拷保護，無法下載。請改用「上傳檔案」。",
    "geo": "這部影片在你所在的地區無法觀看。",
    "invalid": "網址無效，或影片已被移除。請確認網址正確、在瀏覽器能正常播放。",
    "unsupported": "不支援這個網站的網址。請改用「上傳檔案」。",
    "js": "下載 YouTube 需要 JavaScript 執行環境。請安裝 Deno（在命令提示字元輸入 winget install DenoLand.Deno），再重新開啟 start.cmd。",
    "network": "連不上影片網站，請確認網路連線（公司網路可能封鎖了這個網站）。",
    "ratelimit": "平台暫時限制下載次數，請過一段時間再試。",
    "other": "下載失敗，可能是平台改版。程式每天會自動更新下載工具，可以明天再試；仍然失敗請回報。",
}

# 依序比對（前面的優先）；全部轉小寫比對
_PATTERNS = [
    ("drm", ["drm"]),
    ("js", ["javascript runtime", "js runtime", "--js-runtimes"]),
    ("ratelimit", ["http error 429", "too many requests"]),
    ("geo", ["not available in your country", "geo restrict", "geo-restrict", "not available in your region"]),
    ("login", ["sign in to confirm", "login required", "log in to", "login to", "private video", "members-only",
               "members only", "join this channel", "age-restricted", "confirm your age", "use --cookies",
               "cookies-from-browser", "http error 403", "not a bot", "requires authentication",
               "registered users", "only available for"]),
    ("unsupported", ["unsupported url"]),
    ("invalid", ["is not a valid url", "incomplete youtube id", "video unavailable", "http error 404",
                 "has been removed", "does not exist", "no longer available", "not found"]),
    ("network", ["getaddrinfo", "unable to connect", "timed out", "name or service not known",
                 "connection refused", "failed to resolve", "network is unreachable", "connection reset"]),
]


def classify(text: str) -> str:
    t = (text or "").lower()
    for kind, keys in _PATTERNS:
        if any(k in t for k in keys):
            return kind
    return "other"


def _error(text: str) -> DownloadError:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    err = [ln for ln in lines if ln.startswith("ERROR")] or lines[-3:]
    detail = "\n".join(err)[-600:]
    kind = classify(detail or text)
    return DownloadError(kind, MESSAGES[kind], detail)


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+\.[^\s]+", url, re.I):
        raise DownloadError("invalid", "請貼上完整的影片網址（http:// 或 https:// 開頭）。")
    return url


# ------------------------------------------------------------------ yt-dlp 呼叫
def _env() -> dict:
    e = dict(os.environ)
    e.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return e


def _base_cmd() -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--ignore-config"]
    # yt-dlp 預設用 Deno；這台只有 Node.js 時改用 Node.js
    if not shutil.which("deno"):
        for rt in ("node", "bun"):
            if shutil.which(rt):
                cmd += ["--js-runtimes", rt]
                break
    try:
        cmd += ["--ffmpeg-location", str(Path(find_ffmpeg()).parent)]
    except FileNotFoundError:
        pass
    return cmd


# ------------------------------------------------------------------ 登入資料（cookies，0.10.0）
# 由本專案的 Chrome 擴充功能「逐字稿登入同步」送到 /api/cookies，存成 Netscape 格式。
# 等同帳號憑證：只存本機 data\\（不進版控、不打包）、內容不寫入任何紀錄。
COOKIES = ROOT / "data" / "cookies.txt"
COOKIES_META = ROOT / "data" / "cookies_meta.json"


def has_cookies() -> bool:
    return COOKIES.exists() and COOKIES.stat().st_size > 50


def save_cookies(cookies: list[dict], meta: dict) -> dict:
    """Chrome cookie 物件清單 → Netscape cookies.txt（整份取代），回傳摘要（不含內容）"""
    lines = ["# Netscape HTTP Cookie File", "# 由「逐字稿登入同步」擴充功能產生，請勿分享給他人", ""]
    domains = set()
    for c in cookies:
        try:
            dom = str(c["domain"]).strip()
            name, value = str(c["name"]), str(c.get("value", ""))
        except Exception:
            continue
        if not dom or "\t" in name + value or "\n" in name + value:
            continue
        host_only = bool(c.get("hostOnly"))
        if host_only:
            dom = dom.lstrip(".")
        elif not dom.startswith("."):
            dom = "." + dom
        exp = 0 if c.get("session") else int(float(c.get("expirationDate") or 0))
        lines.append("\t".join([dom, "FALSE" if host_only else "TRUE", str(c.get("path") or "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(exp), name, value]))
        domains.add(".".join(dom.lstrip(".").split(".")[-2:]))
    COOKIES.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIES.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(COOKIES)
    info = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "count": len(lines) - 3, "domains": sorted(domains), **meta}
    COOKIES_META.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def cookies_status() -> dict:
    try:
        info = json.loads(COOKIES_META.read_text(encoding="utf-8"))
    except Exception:
        info = {}
    return {"has": has_cookies(), **{k: info.get(k) for k in ("updated", "count", "domains", "ext_id", "source")}}


def clear_cookies() -> None:
    """使用者在「參數設定」按「刪除登入資料」（確認後）才會呼叫；只刪程式自己產生的這兩個檔"""
    for f in (COOKIES, COOKIES_META):
        f.unlink(missing_ok=True)


class _CookieCopy:
    """yt-dlp 會把更新後的 cookies 寫回檔案：給它一份暫存複本，用完刪除，正本不被改動"""

    def __init__(self, use: bool):
        self.use, self.path = use and has_cookies(), None

    def __enter__(self) -> list[str]:
        if not self.use:
            return []
        import uuid
        self.path = COOKIES.with_name(f"cookies.run-{uuid.uuid4().hex[:8]}.txt")
        shutil.copyfile(COOKIES, self.path)
        return ["--cookies", str(self.path)]

    def __exit__(self, *a):
        if self.path:
            self.path.unlink(missing_ok=True)


def probe_auto(url: str) -> dict:
    """先不帶登入資料確認；需要登入且有登入資料時，自動帶上重試。回傳的 meta 多一個 cookies（是否用到）"""
    try:
        return probe(url) | {"cookies": False}
    except DownloadError as e:
        if e.kind != "login" or not has_cookies():
            raise
    try:
        return probe(url, use_cookies=True) | {"cookies": True}
    except DownloadError as e:
        if e.kind == "login":
            raise DownloadError("login", MESSAGES["login_cookies"], e.detail)
        raise


def probe(url: str, timeout: int = 90, use_cookies: bool = False) -> dict:
    """確認網址：回傳 {title, id, duration, extractor, webpage_url}；播放清單、直播會擋下"""
    url = check_url(url)
    with _update_lock:  # 正在更新 yt-dlp 時先等它完成
        pass
    try:
        with _CookieCopy(use_cookies) as ck:
            r = subprocess.run(_base_cmd() + ck + ["--flat-playlist", "-J", url], capture_output=True,
                               timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               env=_env(), creationflags=_NOWIN)
    except subprocess.TimeoutExpired:
        raise DownloadError("network", "網站回應太慢，請稍後再試。")
    if r.returncode != 0 or not r.stdout.strip():
        raise _error(r.stderr or r.stdout)
    try:
        info = json.loads(r.stdout)
    except Exception:
        raise DownloadError("other", MESSAGES["other"], r.stdout[-300:])
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise DownloadError("playlist", MESSAGES["playlist"])
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError("live", MESSAGES["live"])
    raw = info.get("title") or info.get("id") or "影片"
    return {"title": clean_title(to_tw(raw)) or "影片", "original_title": raw, "id": info.get("id", ""),
            "duration": info.get("duration"), "extractor": info.get("extractor_key") or info.get("extractor", ""),
            "webpage_url": info.get("webpage_url") or url,
            "uploader": to_tw(info.get("uploader") or info.get("channel") or "")}


def _kill(proc: subprocess.Popen):
    """連同 yt-dlp 叫出的 ffmpeg 一起停止"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=_NOWIN)
    else:
        proc.kill()


def download_auto(url: str, work: Path, progress=None, cancelled=lambda: False, log=print,
                  use_cookies: bool = False) -> Path:
    """下載；沒帶登入資料而被要求登入時，有登入資料就自動帶上重試一次"""
    try:
        return download(url, work, progress, cancelled, log, use_cookies)
    except DownloadError as e:
        if use_cookies or e.kind != "login" or not has_cookies():
            if use_cookies and e.kind == "login":
                raise DownloadError("login", MESSAGES["login_cookies"], e.detail)
            raise
    log("需要登入：改用你同步過來的登入資料重試")
    try:
        return download(url, work, progress, cancelled, log, True)
    except DownloadError as e:
        if e.kind == "login":
            raise DownloadError("login", MESSAGES["login_cookies"], e.detail)
        raise


def download(url: str, work: Path, progress: Callable[[float, str], None] | None = None,
             cancelled: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print,
             use_cookies: bool = False) -> Path:
    """下載影片（mp4）到 work（任務資料夾），暫存檔名為 media.<副檔名>，回傳下載好的檔案路徑"""
    with _CookieCopy(use_cookies) as ck:  # 登入資料的暫存複本，下載結束（含失敗、取消）就刪除
        return _download(url, work, progress, cancelled, log, ck)


def _download(url: str, work: Path, progress, cancelled, log, ck: list[str]) -> Path:
    url = check_url(url)
    work.mkdir(parents=True, exist_ok=True)
    if _update_lock.locked():
        progress and progress(0.0, "等待下載工具更新完成…")
    with _update_lock:
        pass
    cmd = _base_cmd() + ck + [
        "-f", FORMAT, "--merge-output-format", "mp4",
        "--newline", "--progress", "--no-mtime", "--windows-filenames", "--no-simulate",
        "--progress-template",
        "download:@@P %(info.vcodec)s %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
        "--print", "after_move:@@F %(filepath)s",
        # 暫存檔用固定的英數檔名，避免標題裡的特殊字元、過長路徑；完成後由程式改成正體標題
        "-o", str(work / "media.%(ext)s"), url]
    log("開始下載影片" + ("（帶入登入資料）" if ck else ""))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            encoding="utf-8", errors="replace", env=_env(), creationflags=_NOWIN, bufsize=1)
    stop = threading.Event()

    def watch():  # 取消時立刻停止（不必等下一行輸出）
        while not stop.wait(0.5):
            if cancelled():
                _kill(proc)
                return
    threading.Thread(target=watch, daemon=True).start()

    out_file, tail, last = None, [], ("", -10)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("@@P "):
                parts = line[4:].split()
                try:
                    vcodec = parts[0]
                    done = float(parts[1])
                    total = next((float(x) for x in parts[2:] if x not in ("NA", "None") and float(x) > 0), 0)
                except (ValueError, IndexError):
                    continue
                f = min(done / total, 1.0) if total else 0.0
                mb = done / 1024 ** 2
                # 影片與聲音分開下載時：影片佔 0～85%、聲音 85～97%、合併 97～100%
                is_audio = vcodec in ("none", "NA")
                what, lo, span = ("聲音", 0.85, 0.12) if is_audio else ("影片", 0.0, 0.85)
                pct = f"{f * 100:.0f}%（{mb:.1f} MB）" if total else f"（{mb:.1f} MB）"
                progress and progress(lo + f * span, f"下載{what} {pct}")
                if (what, int(f * 100) // 25) != (last[0], last[1] // 25):
                    last = (what, int(f * 100))
                    log(f"  下載{what} {last[1]}%（{mb:.1f} MB）")
            elif line.startswith("[Merger]") or line.startswith("[VideoConvertor]"):
                progress and progress(0.97, "合併影片與聲音")
                log("  合併影片與聲音")
            elif line.startswith("@@F "):
                out_file = Path(line[4:].strip())
            elif line:
                tail = (tail + [line])[-40:]
        proc.wait()
    finally:
        stop.set()
    if cancelled():
        raise DownloadCancelled()
    if proc.returncode != 0:
        raise _error("\n".join(tail))
    if not out_file or not out_file.exists():  # 保險：取暫存資料夾裡最新的檔案
        cands = [p for p in work.glob("media.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        out_file = max(cands, key=lambda p: p.stat().st_mtime) if cands else None
    if not out_file:
        raise DownloadError("other", MESSAGES["other"], "\n".join(tail)[-600:])
    log(f"下載完成：{out_file.name}（{out_file.stat().st_size / 1024 ** 2:.1f} MB）")
    return out_file


# ------------------------------------------------------------------ 自動更新
def _venv_python() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def maybe_update(log: Callable[[str], None] = print) -> None:
    """一天最多更新一次 yt-dlp；在背景執行，離線或失敗都不影響使用"""
    today = date.today().isoformat()
    try:
        if json.loads(UPDATE_STAMP.read_text(encoding="utf-8")).get("date") == today:
            return
    except Exception:
        pass
    if not _venv_python():  # 只更新專案 .venv，不動系統的 Python
        return
    with _update_lock:
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "-q", "--disable-pip-version-check",
                                "yt-dlp"], capture_output=True, timeout=300, encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            ok = r.returncode == 0
            msg = "" if ok else (r.stderr or r.stdout)[-300:]
        except Exception as e:
            ok, msg = False, str(e)
        ver = ""
        try:
            ver = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, timeout=60,
                                 encoding="utf-8", errors="replace", creationflags=_NOWIN).stdout.strip()
        except Exception:
            pass
        UPDATE_STAMP.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_STAMP.write_text(json.dumps({"date": today, "ok": ok, "version": ver, "error": msg},
                                           ensure_ascii=False), encoding="utf-8")
        log(f"yt-dlp {'已是最新版' if ok else '更新失敗（不影響使用）'}：{ver}（{time.time() - t0:.0f} 秒）")


def update_status() -> dict:
    try:
        return json.loads(UPDATE_STAMP.read_text(encoding="utf-8"))
    except Exception:
        return {}
