"""下載佇列與 yt-dlp 封裝。"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from . import subtitles as subs
from .fsutil import atomic_write_json
from . import transcribe as asr

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
JOBS_FILE = DATA / "jobs.json"
EXT_COOKIE_FILE = DATA / "cookies.txt"
# 從 Chrome 無痕視窗同步的 YouTube／Google 登入（不會被一般分頁輪替而失效），有的話優先使用
YT_COOKIE_FILE = DATA / "cookies_youtube.txt"
_YT_DOMAIN = re.compile(r"(^|\.)(youtube\.com|google\.com|youtu\.be)$", re.I)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")

DEFAULT_SETTINGS = {
    "download_dir": "download",
    "cookie_mode": "extension",   # extension | browser | file | none
    "browser": "chrome",
    "browser_profile": "",
    "cookie_file": "",
    "max_height": 1080,           # 0 = 最佳畫質
    "subtitles": True,
    "en_mode": "translate",       # keep | translate | bilingual
    "translator": "google",       # google | llm（AI 翻譯，連線資訊在 .env）| codex | local（本機 TranslateGemma）
    "canto_written": True,
    "append_id": False,
    "playlist": False,            # 影片網址同時帶有播放清單（watch?v=…&list=…）時，是否下載整個清單
    "playlist_tag": True,         # 播放清單的影片自動加上「清單名稱」hashtag
    "playlist_max": 500,          # 單一播放清單最多展開幾部
    "ffmpeg_path": "",
    "asr_auto": True,             # 影片沒有字幕可下載時，自動用語音辨識產生字幕
    "asr_model_dir": "",          # Breeze-ASR-25（OpenVINO）模型資料夾；空白＝模型位置\asr\ov\breeze-asr-25
    "asr_model": "auto",          # auto（中文用 Breeze、外語用 Whisper turbo）| breeze | turbo
    "models_root": "",            # 本機 AI 模型位置；空白＝環境變數 MODELS_ROOT → D:\models（與 transcribe 專案共用）
    "local_llm": "auto",          # 本機摘要用的 Gemma 4 大小：auto（依記憶體）| e2b | e4b | 26b
    "local_device": "auto",       # 本機翻譯／摘要的執行裝置：auto | GPU | CPU
    "asr_device": "auto",         # auto | CPU | GPU | NPU
    "asr_language": "auto",       # auto | zh | en | ja | ko …
    "asr_keep_mp3": False,        # 辨識後保留抽出的 MP3
    "summary_engine": "llm",      # 重點摘要用的 AI：llm（.env 的 AI）| codex（本機 Codex CLI）| local（本機 Gemma 4）
    "codex_path": "",             # codex 執行檔路徑（留空＝自動尋找）
    "codex_model": "",            # Codex 使用的模型（留空＝Codex 預設）
    "big5_names": True,           # 檔名只用 Big5 字元（簡體轉正體、去掉表情符號），舊式看圖軟體才開得了
    "library_subdirs": "local_movies",   # 影片庫也納入下載資料夾裡的這些子資料夾（逗號分隔），放本機原有的影片
}

SITE_NAMES = [
    (r"youtube\.com|youtu\.be", "YouTube"), (r"facebook\.com|fb\.watch", "Facebook"),
    (r"instagram\.com", "Instagram"), (r"bilibili\.com|b23\.tv", "B站"),
    (r"twitter\.com|x\.com", "X"), (r"tiktok\.com", "TikTok"), (r"threads\.(net|com)", "Threads"),
    (r"vimeo\.com", "Vimeo"), (r"dailymotion\.com", "Dailymotion"),
]


def site_of(url: str) -> str:
    for rx, name in SITE_NAMES:
        if re.search(rx, url, re.I):
            return name
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else "其他"


# ------------------------------------------------------------------ 設定
_lock = threading.RLock()


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s


def save_settings(new: dict) -> dict:
    s = load_settings()
    for k, v in new.items():
        if k in DEFAULT_SETTINGS:
            s[k] = v
    # 模型位置必須是完整路徑（像 D:\models）；瀏覽器自動填入的「download」這類字會讓模型全部找不到，直接清成空白（＝用預設）
    for k in ("models_root", "asr_model_dir"):
        v = str(s.get(k) or "").strip().strip('"')
        s[k] = v if v and Path(v).is_absolute() else ""
    atomic_write_json(SETTINGS_FILE, s, indent=2)
    return s


def download_dir(settings: dict | None = None) -> Path:
    s = settings or load_settings()
    p = Path(s.get("download_dir") or "download")
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_ffmpeg(settings: dict | None = None) -> str | None:
    s = settings or load_settings()
    cands = []
    if s.get("ffmpeg_path"):
        cands.append(Path(s["ffmpeg_path"]))
    for d in glob.glob(str(ROOT / "tools" / "**" / "bin"), recursive=True):
        cands.append(Path(d))
    for c in cands:
        if c.is_file():
            return str(c.parent)
        exe = c / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if exe.exists():
            return str(c)
    w = shutil.which("ffmpeg")
    return str(Path(w).parent) if w else None


# ------------------------------------------------------------------ Cookie
def write_extension_cookies(cookies: list[dict], target: Path | None = None) -> int:
    """把 Chrome 擴充功能送來的 cookie 轉為 Netscape cookies.txt。"""
    target = target or EXT_COOKIE_FILE
    lines = ["# Netscape HTTP Cookie File", "# 由「影片下載器同步」Chrome 擴充功能產生，請勿外流", ""]
    n = 0
    for c in cookies:
        domain = c.get("domain", "")
        if not domain or not c.get("name"):
            continue
        host_only = c.get("hostOnly", False)
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        flag = "FALSE" if host_only else "TRUE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = int(c.get("expirationDate") or 0)
        val = str(c.get("value", "")).replace("\n", "")
        lines.append("\t".join([domain, flag, c.get("path", "/"), secure, str(exp), c["name"], val]))
        n += 1
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return n


def _cookie_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip() and (not ln.startswith("#") or ln.startswith("#HttpOnly_"))]


def _cookie_domain(line: str) -> str:
    return line.split("\t")[0].replace("#HttpOnly_", "").lstrip(".")


def youtube_login_state(lines: list[str]) -> dict:
    """只看 cookie 名稱（不看內容）判斷 YouTube 是否為登入狀態，與 yt-dlp 的判斷方式相同"""
    names = {ln.split("\t")[5] for ln in lines
             if len(ln.split("\t")) >= 7 and _cookie_domain(ln).endswith("youtube.com")}
    login_info = "LOGIN_INFO" in names
    sapisid = bool(names & {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"})
    return {"logged_in": login_info and sapisid, "login_info": login_info, "sapisid": sapisid}


def effective_cookie_lines() -> tuple[list[str], str]:
    """擴充功能模式實際交給 yt-dlp 的 cookie：有無痕同步的 YouTube 登入時，YouTube／Google 部分改用它"""
    normal = _cookie_lines(EXT_COOKIE_FILE)
    private = _cookie_lines(YT_COOKIE_FILE)
    if not private:
        return normal, "一般分頁"
    keep = [ln for ln in normal if not _YT_DOMAIN.search(_cookie_domain(ln))]
    return keep + private, "無痕視窗"


def cookie_status() -> dict:
    if not EXT_COOKIE_FILE.exists():
        return {"exists": False}
    domains: dict[str, int] = {}
    count = 0
    for line in EXT_COOKIE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#"):
            continue
        d = line.split("\t")[0].lstrip(".")
        d = ".".join(d.split(".")[-2:])
        domains[d] = domains.get(d, 0) + 1
        count += 1
    lines, src = effective_cookie_lines()
    yt = youtube_login_state(lines)
    yt.update(source=src, private_updated=YT_COOKIE_FILE.stat().st_mtime if YT_COOKIE_FILE.exists() else None)
    return {"exists": True, "count": count, "domains": domains, "path": str(EXT_COOKIE_FILE),
            "youtube_path": str(YT_COOKIE_FILE) if YT_COOKIE_FILE.exists() else "",
            "updated": EXT_COOKIE_FILE.stat().st_mtime, "youtube": yt}


def cookie_opts(s: dict) -> tuple[dict, str]:
    """回傳 (yt-dlp 參數, 說明)。cookie 檔一律複製成暫存檔，避免 yt-dlp 回寫原檔。"""
    mode = s.get("cookie_mode", "extension")

    def from_file(path: Path):
        tmp = Path(tempfile.gettempdir()) / f"movdl_cookies_{uuid.uuid4().hex}.txt"
        shutil.copyfile(path, tmp)
        return {"cookiefile": str(tmp)}

    if mode == "extension":
        if EXT_COOKIE_FILE.exists() or YT_COOKIE_FILE.exists():
            lines, src = effective_cookie_lines()
            tmp = Path(tempfile.gettempdir()) / f"movdl_cookies_{uuid.uuid4().hex}.txt"
            tmp.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
            return {"cookiefile": str(tmp)}, f"擴充功能同步的 Cookie（YouTube 登入來源：{src}）"
        mode = "browser"
    if mode == "file" and s.get("cookie_file") and Path(s["cookie_file"]).exists():
        return from_file(Path(s["cookie_file"])), "cookies.txt 檔案"
    if mode == "browser":
        b = s.get("browser") or "chrome"
        return {"cookiesfrombrowser": (b, s.get("browser_profile") or None, None, None)}, f"直接讀取 {b} Cookie"
    return {}, "不使用 Cookie"


def test_browser_cookies(browser: str, profile: str = "") -> dict:
    from yt_dlp.cookies import extract_cookies_from_browser
    try:
        jar = extract_cookies_from_browser(browser, profile or None)
        doms = {}
        for c in jar:
            d = ".".join(c.domain.lstrip(".").split(".")[-2:])
            doms[d] = doms.get(d, 0) + 1
        return {"ok": True, "count": len(jar),
                "domains": dict(sorted(doms.items(), key=lambda x: -x[1])[:15])}
    except Exception as e:
        from . import errlog
        errlog.error("Cookie", "讀取 cookies.txt 失敗", e)
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ 影片資訊（原文簡介）
META_KEYS = ("title", "description", "uploader", "channel", "upload_date", "duration",
             "webpage_url", "tags", "extractor_key", "id")


def meta_path(video: Path) -> Path:
    """影片旁的資訊檔：標題.meta.json（與影片一起搬移也不會遺失）"""
    return video.with_suffix(".meta.json")


def build_meta(info: dict, url: str = "") -> dict:
    m = {k: info.get(k) for k in META_KEYS if info.get(k) not in (None, "", [])}
    m["original_title"] = info.get("title") or ""
    m["webpage_url"] = m.get("webpage_url") or info.get("original_url") or url
    m["source_url"] = url or m["webpage_url"]        # 使用者當初貼上的網址
    m["site"] = site_of(m["webpage_url"] or url)
    lang = original_language(info)                   # 原始語言：語音辨識依此挑模型（1.25.0）
    if lang:
        m["language"] = lang
    m["downloaded"] = time.time()
    return m


def read_meta(video: Path) -> dict:
    p = meta_path(video)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def write_meta(video: Path, meta: dict) -> None:
    atomic_write_json(meta_path(video), meta, indent=2)


def fetch_meta(url: str) -> dict:
    """為舊影片補抓原文簡介（不下載影片）。"""
    s = load_settings()
    copts, _ = cookie_opts(s)
    o = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, **copts}
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    finally:
        if "cookiefile" in copts:
            Path(copts["cookiefile"]).unlink(missing_ok=True)
    return build_meta(info, url)


# ------------------------------------------------------------------ 重複偵測
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})")


def video_key(extractor: str, vid: str) -> str:
    ex = (extractor or "").lower().replace(":tab", "")
    if not vid or not ex or ex == "generic":      # 一般網址的 ID 只是檔名，不能拿來判斷重複
        return ""
    return f"{ex}:{vid}"


def key_from_url(url: str) -> str:
    m = _YT_ID.search(url or "")
    return video_key("youtube", m.group(1)) if m else ""


def norm_url(u: str) -> str:
    u = re.sub(r"^https?://(www\.|m\.)?", "", (u or "").strip()).rstrip("/")
    return re.sub(r"[?&](si|feature|utm_[a-z]+|t|pp)=[^&]*", "", u)


def library_index(d: Path | None = None) -> dict[str, Path]:
    """已下載影片索引：影片 ID（網站:ID）與來源網址 → 影片檔"""
    d = d or download_dir()
    idx: dict[str, Path] = {}
    for mp in d.glob("*.meta.json"):
        stem = mp.name[:-len(".meta.json")]
        video = next((d / (stem + e) for e in VIDEO_EXTS if (d / (stem + e)).exists()), None)
        if not video:
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        ek = (m.get("extractor_key") or "").lower()
        if m.get("id") and ek:
            idx[video_key(ek, m["id"])] = video
        for u in (m.get("webpage_url"), m.get("source_url")):
            if u:
                k = key_from_url(u)
                if k:
                    idx[k] = video
                idx["url:" + norm_url(u)] = video
    return idx


def find_existing(info: dict | None = None, url: str = "", idx: dict | None = None) -> Path | None:
    idx = library_index() if idx is None else idx
    keys = []
    if info:
        ek = (info.get("extractor_key") or info.get("ie_key") or "").lower()
        if info.get("id") and ek:
            keys.append(video_key(ek, info["id"]))
        for u in (info.get("webpage_url"), info.get("url")):
            if u and str(u).startswith("http"):
                keys += [key_from_url(u), "url:" + norm_url(u)]
    if url:
        keys += [key_from_url(url), "url:" + norm_url(url)]
    for k in keys:
        if k and k in idx and idx[k].exists():
            return idx[k]
    return None


# ------------------------------------------------------------------ 語音辨識（沒有字幕時）
def asr_model_dir(s: dict | None = None) -> str:
    """設定的模型資料夾；空白、不是完整路徑或不存在時改用預設位置（避免瀏覽器自動填入錯字造成辨識失敗）"""
    s = s or load_settings()
    d = (s.get("asr_model_dir") or "").strip().strip('"')
    if d and Path(d).is_absolute() and Path(d).is_dir():
        return d
    from . import modelpaths
    shared = modelpaths.path("breeze", s)            # 1.25.0：模型位置（預設 D:\models）與 transcribe 共用
    fallback = str(shared) if shared.is_dir() else asr.default_model_dir()
    if d:
        from . import errlog
        errlog.warn("語音辨識", f"設定的模型資料夾「{d}」不存在或不是完整路徑，改用 {fallback}")
    return fallback


_WHISPER_LANGS = {"en", "ja", "ko", "fr", "de", "es", "it", "pt", "ru", "vi", "th", "id", "ms", "nl", "pl", "tr",
                  "ar", "hi", "uk", "sv", "fi", "da", "no", "cs", "el", "he", "hu", "ro", "fa", "tl"}


def guess_language(meta: dict) -> str:
    """影片說的語言：先看下載時記下的原始語言，沒有時看標題與簡介的文字。
    回傳 zh（中文）、語言代碼（en、ja…）、foreign（外語但不確定哪一種）或空字串（無法判斷）"""
    lang = str(meta.get("language") or "").lower().replace("_", "-")
    if lang:
        base = lang.split("-")[0]
        return "zh" if base in ("zh", "yue", "cmn", "nan", "hak") else base
    text = " ".join(str(meta.get(k) or "") for k in ("original_title", "title", "description", "uploader", "channel"))
    text = text[:3000]
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if kana >= 3 and kana >= cjk * 0.2:
        return "ja"
    if hangul >= 3:
        return "ko"
    if cjk >= 4:
        return "zh"
    if cjk == 0 and latin >= 30:                     # 標題、簡介完全沒有中文字
        return "foreign"
    return ""


def pick_asr(video: Path, s: dict) -> tuple[str, str, str, str]:
    """挑語音辨識模型（1.25.0）。回傳 (模型資料夾, 語言, 模型名稱, 挑選原因)。
    自動：中文、中英夾雜或無法判斷 → Breeze-ASR-25；確定是外語 → Whisper large-v3-turbo（外語較準、也較快）"""
    from . import modelpaths
    choice = s.get("asr_model") or "auto"
    lang = s.get("asr_language") or "auto"
    breeze = (asr_model_dir(s), lang, "Breeze-ASR-25")
    turbo_dir = str(modelpaths.path("turbo", s))
    turbo_ok = not asr.check_model(turbo_dir)
    if choice == "breeze":
        return (*breeze, "設定指定")
    if choice == "turbo":
        if not turbo_ok:
            raise asr.TranscribeError(f"找不到 Whisper large-v3-turbo 模型：{turbo_dir}（{asr.check_model(turbo_dir)}）。"
                                      "請到「設定 → 本機 AI 模型」確認模型位置，或把辨識模型改回「自動」")
        return turbo_dir, lang, "Whisper large-v3-turbo", "設定指定"
    guess = lang if lang != "auto" else guess_language(read_meta(video))
    if guess and guess != "zh":
        if turbo_ok:
            spoken = lang if lang != "auto" else (guess if guess in _WHISPER_LANGS else "auto")
            return turbo_dir, spoken, "Whisper large-v3-turbo", f"判斷為外語影片（{guess}）"
        from . import errlog
        errlog.warn("語音辨識", f"影片判斷為外語（{guess}），但找不到 Whisper large-v3-turbo（{turbo_dir}），改用 Breeze-ASR-25")
        return (*breeze, "外語影片，但沒有 turbo 模型")
    return (*breeze, "中文影片" if guess == "zh" else "無法判斷語言，用預設")


def run_asr(video: Path, s: dict, context: str = "", progress=None, cancelled=None) -> list[Path]:
    """MP4 →（ffmpeg）MP3 →（Breeze-ASR-25）SRT，再依字幕設定轉台灣用語或翻譯。回傳產生的字幕檔"""
    ffmpeg = find_ffmpeg(s)
    if not ffmpeg:
        raise asr.TranscribeError("找不到 ffmpeg")
    model_dir, language, model_label, why = pick_asr(video, s)
    try:
        raw = asr.transcribe_video(video, ffmpeg, model_dir, s.get("asr_device") or "auto",
                                   language, bool(s.get("asr_keep_mp3")),
                                   progress=progress, cancelled=cancelled, model_label=model_label)
    except asr.NoSpeech:
        m = read_meta(video)          # 記下來，批次辨識時不再反覆嘗試
        m["no_speech"] = True
        write_meta(video, m)
        raise
    text = raw.read_text(encoding="utf-8", errors="replace")
    letters = len(re.findall(r"[A-Za-z]", text))
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    hint = "zh-TW" if cjk >= letters * 0.3 else "en"
    engine = s.get("translator", "google")
    label = {"llm": "AI", "codex": "Codex", "local": "本機"}.get(engine, "Google")
    base = video.with_suffix("")
    try:
        outs = subs.process_subtitle(raw, base, hint, s.get("en_mode", "translate"), s.get("canto_written", True),
                                     progress=(lambda p, lb="": progress(p, f"翻譯字幕中（{label}）{lb}")) if progress else None,
                                     engine=engine, context=context)
    except subs.TranslationFailed:
        outs = [Path(p) for p in glob.glob(glob.escape(str(base)) + ".*.srt")]
    meta = read_meta(video)
    meta.pop("no_speech", None)
    meta["asr"] = True
    meta["asr_model"] = f"{model_label}（{why}）"
    write_meta(video, meta)
    return outs


# ------------------------------------------------------------------ 下載紀錄（用來認出「下載過、後來刪掉」的影片）
HISTORY_FILE = DATA / "history.json"
_history: set[str] | None = None


def meta_keys(meta: dict, url: str = "") -> list[str]:
    ks = [video_key(meta.get("extractor_key") or "", meta.get("id") or "")]
    for u in (meta.get("webpage_url"), meta.get("source_url"), url):
        if u:
            ks += [key_from_url(u), "url:" + norm_url(u)]
    return [k for k in ks if k and k != "url:"]


def history() -> set[str]:
    global _history
    if _history is None:
        try:
            _history = set(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
        except Exception:
            _history = set()
    return _history


def add_history(keys) -> None:
    h = history()
    new = [k for k in keys if k and k not in h]
    if not new:
        return
    with _lock:
        h.update(new)
        atomic_write_json(HISTORY_FILE, sorted(h))


def bootstrap_history(jobs: list[dict]) -> None:
    """把目前影片庫與過去完成的下載工作補進下載紀錄（舊版本沒有這份紀錄）"""
    keys: list[str] = list(library_index().keys())
    for j in jobs:
        if j.get("status") == "done" and j.get("file") and not j.get("is_playlist") and not j.get("is_scan"):
            keys += [key_from_url(j["url"]), "url:" + norm_url(j["url"])]
    add_history(keys)


# ------------------------------------------------------------------ 訂閱清單（固定追蹤的播放清單／頻道）
SUBS_FILE = DATA / "subscriptions.json"


def load_subs() -> list[dict]:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_subs(subs: list[dict]) -> None:
    atomic_write_json(SUBS_FILE, subs, indent=1)


def entry_keys(url: str, e: dict) -> list[str]:
    ks = [video_key(e.get("ie_key") or e.get("extractor_key") or "", e.get("id") or ""),
          key_from_url(url), "url:" + norm_url(url)]
    return [k for k in ks if k]


def playlist_tag(title: str) -> str:
    t = re.sub(r"\s+", "_", str(title).strip().lstrip("#＃"))
    return re.sub(r"[,，、;；#＃]", "", t)[:30]


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_S2T = None
_JP = {"ー": "－", "説": "說", "戦": "戰", "歩": "步", "気": "氣", "様": "樣", "関": "關", "読": "讀", "発": "發",
       "応": "應", "変": "變", "楽": "樂", "沢": "澤", "広": "廣", "対": "對", "画": "畫", "労": "勞", "効": "效"}
_LOOKALIKE = {"/": "／", "\\": "＼", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜",
              "⧸": "／", "⧹": "＼"}


def big5_name(name: str) -> str:
    """讓檔名每個字都能用 Big5（cp950）表示：舊式看圖／預覽軟體只認得 Big5，遇到簡體字、表情符號、
    「⧸」這類罕見字元就會開不了檔。簡體逐字轉正體（不做詞彙轉換）、日文新字體轉正體、表情符號拿掉。"""
    global _S2T
    if _S2T is None:
        try:
            import opencc
            _S2T = opencc.OpenCC("s2t")
        except Exception:
            _S2T = False
    out = []
    for ch in name:
        if ch in _LOOKALIKE:
            out.append(_LOOKALIKE[ch]); continue
        try:
            ch.encode("cp950"); out.append(ch); continue
        except UnicodeEncodeError:
            pass
        if ch in _JP:
            out.append(_JP[ch]); continue
        t = _S2T.convert(ch) if _S2T else ch
        if t != ch:
            try:
                t.encode("cp950"); out.append(t); continue
            except UnicodeEncodeError:
                pass
        import unicodedata
        n = unicodedata.normalize("NFKC", ch)
        if n != ch:
            try:
                n.encode("cp950"); out.append(n); continue
            except UnicodeEncodeError:
                pass
    return re.sub(r" {2,}", " ", "".join(out)).strip()


def safe_filename(name: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)", name):
        name += "_"
    return name.encode("utf-8")[:180].decode("utf-8", "ignore").strip()


# ------------------------------------------------------------------ 字幕選擇
_ZH_TW = re.compile(r"^(zh[-_](tw|hant.*)|zh-hant)$", re.I)
_ZH_HK = re.compile(r"^(zh[-_]hk|yue.*)$", re.I)
_ZH_CN = re.compile(r"^(zh([-_](cn|hans.*|sg))?|ai-zh|zh-hans)$", re.I)
_EN = re.compile(r"^en([-_].*)?$", re.I)


def original_language(info: dict) -> str:
    """找出影片「原始音軌」語言（YouTube 可能有 AI 配音的多語音軌）。"""
    if info.get("language"):
        return str(info["language"]).lower()
    best, pref = "", -10**9
    for f in info.get("formats") or []:
        lang = f.get("language")
        if not lang or f.get("acodec") in (None, "none"):
            continue
        p = f.get("language_preference") or 0
        note = (f.get("format_note") or "").lower()
        if "original" in note:
            p += 100
        if p > pref:
            best, pref = lang, p
    return best.lower()


def pick_subtitle(info: dict) -> tuple[str, bool] | None:
    """回傳 (語言代碼, 是否為自動字幕)"""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items()
              if k not in ("live_chat", "danmaku", "comments", "rechat")}
    auto = info.get("automatic_captions") or {}
    orig = original_language(info)
    base = lambda k: re.sub(r"-orig$", "", k.lower())
    same = lambda k: orig and (base(k) == orig or base(k).split("-")[0] == orig.split("-")[0])

    for rx in (_ZH_TW, _ZH_HK, _ZH_CN):
        for k in manual:
            if rx.match(k):
                return k, False
    for k in manual:
        if same(k):
            return k, False
    for k in manual:
        if _EN.match(k):
            return k, False
    if manual:
        return next(iter(manual)), False
    if auto:
        origs = [k for k in auto if k.lower().endswith("-orig")]
        # 有 AI 配音時會出現多個 -orig，須挑與原始音軌相同語言的那條
        for k in origs:
            if same(k):
                return k, True
        for rx in (_ZH_TW, _ZH_HK, _ZH_CN, _EN):
            for k in origs:
                if rx.match(base(k)):
                    return k, True
        if orig and orig in auto:
            return orig, True
        if origs:
            return origs[0], True
        for rx in (_ZH_TW, _ZH_HK, _ZH_CN, _EN):
            for k in auto:
                if rx.match(k):
                    return k, True
    return None


# ------------------------------------------------------------------ 佇列
class JobQueue:
    def __init__(self):
        self.jobs: list[dict] = []
        self.paused = False
        self._cancel: set[str] = set()
        self._event = threading.Event()
        self._ytlogs: dict[str, YtLogger] = {}
        self._load()
        try:
            bootstrap_history(self.jobs)
        except Exception:
            traceback.print_exc()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- 持久化
    def _load(self):
        if JOBS_FILE.exists():
            try:
                self.jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
                for j in self.jobs:
                    if j["status"] == "running":
                        j.update(status="queued", phase="等待中（程式重新啟動）", progress=0)
            except Exception:
                self.jobs = []

    def _save(self):
        with _lock:
            atomic_write_json(JOBS_FILE, self.jobs, indent=1)

    # ---- 操作
    def add(self, urls: list[str]) -> list[dict]:
        added = []
        with _lock:
            for u in urls:
                job = {"id": uuid.uuid4().hex[:10], "url": u, "site": site_of(u), "status": "queued",
                       "phase": "排隊中", "progress": 0, "speed": "", "eta": "", "title": "",
                       "thumbnail": "", "file": "", "subs": [], "error": "", "warning": "",
                       "created": time.time(), "finished": None}
                self.jobs.append(job)
                added.append(job)
            self._save()
        self._event.set()
        return added

    def add_scan(self, sub: dict, fill: bool = False) -> dict | None:
        """把「掃描訂閱清單」排到佇列最前面（不必等前面的下載全部跑完）"""
        with _lock:
            if any(j.get("sub_id") == sub["id"] and j.get("is_scan") and j["status"] in ("queued", "running")
                   for j in self.jobs):
                return None
            job = {"id": uuid.uuid4().hex[:10], "url": sub["url"], "site": site_of(sub["url"]), "status": "queued",
                   "phase": "等待掃描", "progress": 0, "speed": "", "eta": "",
                   "title": f"🔄 掃描：{sub.get('title') or sub['url']}", "thumbnail": "", "file": "", "subs": [],
                   "error": "", "warning": "", "created": time.time(), "finished": None,
                   "sub_id": sub["id"], "is_scan": True, "mark_only": bool(sub.get("track_only")) and not sub.get("initialized"),
                   "starred": bool(sub.get("star")), "fill": fill}
            # 清掉這個訂閱之前留下的掃描紀錄，佇列才不會越積越多
            self.jobs = [j for j in self.jobs if not (j.get("is_scan") and j.get("sub_id") == sub["id"])]
            pos = next((i for i, j in enumerate(self.jobs) if j["status"] == "queued"), len(self.jobs))
            self.jobs.insert(pos, job)
            self._save()
        self._event.set()
        return job

    def get(self, jid):
        return next((j for j in self.jobs if j["id"] == jid), None)

    def update(self, jid, **kw):
        with _lock:
            j = self.get(jid)
            if j:
                j.update(kw)

    def cancel(self, jid):
        j = self.get(jid)
        if not j:
            return
        if j["status"] == "running":
            self._cancel.add(jid)
        elif j["status"] == "queued":
            self.update(jid, status="canceled", phase="已取消")
            self._save()

    def star(self, jid, on: bool | None = None) -> bool:
        """在佇列中標示／取消星號；已下載完成的會同步寫入影片資訊檔，播放清單則套用到所有影片"""
        j = self.get(jid)
        if not j:
            return False
        on = (not j.get("starred")) if on is None else on
        targets = [j] + ([x for x in self.jobs if x.get("parent") == jid] if j.get("is_playlist") else [])
        base = download_dir()
        with _lock:
            for t in targets:
                t["starred"] = on
                if t.get("file") and (base / t["file"]).exists():
                    m = read_meta(base / t["file"])
                    if bool(m.get("starred")) != on:
                        m["starred"] = on
                        write_meta(base / t["file"], m)
            self._save()
        return on

    def cancel_children(self, jid):
        """取消此播放清單中尚未開始的影片"""
        n = 0
        with _lock:
            for j in self.jobs:
                if j.get("parent") == jid and j["status"] == "queued":
                    j.update(status="canceled", phase="已取消")
                    n += 1
            self._save()
        return n

    def retry(self, jid, force: bool = False):
        j = self.get(jid)
        if j and j["status"] in ("error", "canceled", "done"):
            self.update(jid, status="queued", phase="排隊中" if not force else "排隊中（強制重新下載）",
                        progress=0, error="", warning="", skipped=False, force=force)
            self._save()
            self._event.set()

    def remove(self, jid):
        j = self.get(jid)
        if j and j["status"] == "running":
            self._cancel.add(jid)
        with _lock:
            self.jobs = [x for x in self.jobs if x["id"] != jid]
            self._save()

    def move(self, jid, direction: int):
        with _lock:
            idx = next((i for i, j in enumerate(self.jobs) if j["id"] == jid), None)
            if idx is None:
                return
            k = idx + direction
            if 0 <= k < len(self.jobs):
                self.jobs[idx], self.jobs[k] = self.jobs[k], self.jobs[idx]
                self._save()

    def clear_finished(self):
        with _lock:
            self.jobs = [j for j in self.jobs if j["status"] in ("queued", "running")]
            self._save()

    def set_paused(self, v: bool):
        self.paused = v
        if not v:
            self._event.set()

    # ---- 背景工作
    def _next(self):
        with _lock:
            return next((j for j in self.jobs if j["status"] == "queued"), None)

    def _worker(self):
        while True:
            job = None if self.paused else self._next()
            if not job:
                self._event.wait(2)
                self._event.clear()
                continue
            jid = job["id"]
            self.update(jid, status="running", phase="解析網址中…", progress=0, started=time.time())
            self._save()
            try:
                self._run(job)
                if self.get(jid) and job.get("skipped"):
                    self.update(jid, status="done", progress=100, finished=time.time(), speed="", eta="",
                                phase="已下載過，略過（如需重新下載請按「強制重新下載」）")
                elif self.get(jid) and job.get("is_scan") and job.get("expanded") is not None \
                        and not any(j.get("parent") == jid for j in self.jobs):
                    with _lock:      # 沒有新影片的掃描不留在佇列（結果顯示在訂閱清單頁）
                        self.jobs = [j for j in self.jobs if j["id"] != jid]
                elif self.get(jid) and job.get("expanded") is not None:
                    self.update(jid, status="done", progress=100, finished=time.time(), speed="", eta="",
                                phase=job.get("expand_msg") or
                                f"播放清單已展開為 {job['expanded']} 部影片，已依序加入佇列")
                elif self.get(jid):
                    self.update(jid, status="done", phase="完成", progress=100, finished=time.time(),
                                speed="", eta="")
            except DownloadCancelled:
                self.update(jid, status="canceled", phase="已取消", speed="", eta="")
            except Exception as e:
                msg = friendly_error(str(e))
                ylog = self._ytlogs.pop(jid, None)
                warns = ylog.warnings if ylog else []
                if "youtu" in job.get("url", "") and (
                        re.search(r"members|Join this channel|Sign in|login|private", str(e), re.I)
                        or any("no longer valid" in w for w in warns)):
                    msg = youtube_auth_hint(msg, warns)
                from . import errlog
                extra = ("\nyt-dlp 警告：\n" + "\n".join(warns)) if warns else ""
                errlog.error("訂閱掃描" if job.get("is_scan") else "下載",
                             f"{job.get('title') or ''} {job.get('url', '')}\n顯示訊息：{msg}{extra}", e)
                if job.get("is_scan"):
                    with _lock:
                        subs_all = load_subs()
                        for x in subs_all:
                            if x["id"] == job.get("sub_id"):
                                x.update(last_scan=time.time(), last_error=msg[:300])
                        save_subs(subs_all)
                # 會員限定／私人影片重試也不會成功 → 不列入「下次掃描再試」，避免每次掃描都重複失敗
                locked = bool(re.search(r"members-only|channel's members|Join this channel|Private video|會員", str(e), re.I))
                if job.get("sub_id") and not job.get("is_scan") and not locked:
                    # 訂閱的影片下載失敗 → 從「看過」紀錄移除，下次掃描會再試一次
                    with _lock:
                        subs_all = load_subs()
                        drop = set(entry_keys(job["url"], {}))
                        for x in subs_all:
                            if x["id"] == job["sub_id"]:
                                x["seen"] = [k for k in x.get("seen") or [] if k not in drop]
                        save_subs(subs_all)
                self.update(jid, status="error", phase="失敗", error=msg, speed="", eta="",
                            finished=time.time())
                traceback.print_exc()
            finally:
                self._cancel.discard(jid)
                self._ytlogs.pop(jid, None)
                self._save()

    def _mark_existing(self, job: dict, video: Path, quiet: bool = False):
        jid = job["id"]
        meta = read_meta(video)
        changed = False
        for t in job.get("auto_tags") or []:     # 播放清單的 hashtag 仍補加到既有影片
            tags = meta.setdefault("hashtags", [])
            if t.lower() not in {x.lower() for x in tags}:
                tags.append(t)
                changed = True
        if job.get("playlist") and not meta.get("playlist"):
            meta["playlist"] = job["playlist"]
            changed = True
        if job.get("sub_id") and not meta.get("sub_id"):      # 記住來自哪個訂閱（「不做摘要」等設定用）
            meta["sub_id"] = job["sub_id"]
            changed = True
        if job.get("starred") and not meta.get("starred"):
            meta["starred"] = True
            changed = True
        if changed:
            write_meta(video, meta)
        if quiet:
            return
        subs_files = [p.name for p in video.parent.glob(glob.escape(video.stem) + ".*.srt")]
        self.update(jid, file=video.name, title=meta.get("original_title") or video.stem, subs=subs_files,
                    skipped=True, progress=100)
        job["skipped"] = True

    def _expand(self, job: dict, info: dict, s: dict):
        """把播放清單展開成一部一部的下載工作（各自有進度、字幕、翻譯與資訊檔）"""
        jid = job["id"]
        ptitle = info.get("title") or info.get("id") or "播放清單"
        self.update(jid, title=f"📃 {ptitle}", phase="讀取播放清單內容…", uploader=info.get("uploader") or "")
        limit = int(s.get("playlist_max") or 500)
        entries = []
        for e in info.get("entries") or []:
            if jid in self._cancel:
                raise DownloadCancelled("使用者取消")
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url") or ""
            if not url.startswith("http"):
                if (e.get("ie_key") or "").lower().startswith("youtube") and e.get("id"):
                    url = f"https://www.youtube.com/watch?v={e['id']}"
                else:
                    continue
            entries.append((url, e))
            if len(entries) % 50 == 0:
                self.update(jid, phase=f"讀取播放清單內容…已找到 {len(entries)} 部")
            if len(entries) >= limit:
                self.update(jid, warning=f"播放清單超過 {limit} 部，只加入前 {limit} 部（可在設定調整上限）")
                break
        if not entries:
            raise RuntimeError("播放清單是空的，或需要登入才能看到內容")
        total = len(entries)
        tag = playlist_tag(ptitle) if s.get("playlist_tag", True) else ""
        now = time.time()
        idx = library_index()
        existing = {}
        for url, e in entries:
            v = find_existing({"id": e.get("id"), "ie_key": e.get("ie_key"), "url": url}, url, idx)
            if v:
                existing[url] = v

        # 訂閱掃描：只把需要的影片加入佇列，其餘完全不進佇列
        #   一般模式：只抓「沒看過也沒下載過」的新影片
        #   補齊模式（fill）：不管以前看過沒有，影片庫裡沒有的一律下載
        if job.get("sub_id"):
            with _lock:
                subs_all = load_subs()
                sub = next((x for x in subs_all if x["id"] == job["sub_id"]), None)
                seen = set(sub.get("seen") or []) if sub else set()
                fill = bool(job.get("fill")) or bool(sub and sub.get("fill"))
                missing = [(u, e) for u, e in entries if u not in existing]
                # 下載過、後來被刪掉的影片（多半是看完了）→ 不論哪種模式都不再下載
                hist = history()
                deleted = [(u, e) for u, e in missing if set(entry_keys(u, e)) & hist]
                gone = {u for u, _ in deleted}
                missing = [(u, e) for u, e in missing if u not in gone]
                fresh = missing if fill else [(u, e) for u, e in missing if not (set(entry_keys(u, e)) & seen)]
                first_track_only = bool(job.get("mark_only")) and not fill
                if first_track_only:
                    fresh = []
                if sub is not None:
                    for u, e in entries:
                        seen.update(entry_keys(u, e))
                    sub.update(seen=sorted(seen), title=sub.get("title") or ptitle, last_scan=now,
                               last_total=total, last_new=len(fresh), last_have=len(existing),
                               last_deleted=len(deleted),
                               last_error="", initialized=True)
                    save_subs(subs_all)
            # 影片庫裡已經有的影片：補上清單名稱 hashtag 與清單資訊（用 hashtag 篩選時數量才會對）
            for k, (u, e) in enumerate(entries, 1):
                if u in existing:
                    try:
                        self._mark_existing({"id": "", "auto_tags": [tag] if tag else [],
                                             "playlist": {"id": info.get("id") or "", "title": ptitle, "index": k,
                                                          "count": total, "url": job["url"]}}, existing[u], quiet=True)
                    except Exception:
                        pass
            have, lack = len(existing), len(missing) - len(fresh)
            tail = f"清單共 {total} 部，已有 {have} 部" + (f"，{len(deleted)} 部看過已刪除不再下載" if deleted else "")
            if first_track_only:
                msg = f"已開始追蹤（{tail}；之後有新影片才會下載）"
            elif fresh:
                msg = f"{'補齊' if fill else '找到'} {len(fresh)} 部{'缺少的' if fill else '新'}影片，已加入佇列（{tail}）"
            else:
                msg = f"沒有{'缺少的' if fill else '新'}影片（{tail}" + (f"，另有 {lack} 部之前略過未下載" if lack else "") + "）"
            entries = fresh
            existing = {}
            job["expand_msg"] = msg
        with _lock:
            pos = next(i for i, j in enumerate(self.jobs) if j["id"] == jid) + 1
            for k, (url, e) in enumerate(entries, 1):
                thumbs = e.get("thumbnails") or []
                child = {"id": uuid.uuid4().hex[:10], "url": url, "site": site_of(url), "status": "queued",
                         "phase": "排隊中", "progress": 0, "speed": "", "eta": "", "title": e.get("title") or "",
                         "thumbnail": (thumbs[-1].get("url") if thumbs else "") or "", "file": "", "subs": [],
                         "error": "", "warning": "", "created": now, "finished": None, "parent": jid,
                         "playlist": {"id": info.get("id") or "", "title": ptitle, "index": k, "count": total,
                                      "url": job["url"]},
                         "auto_tags": [tag] if tag else [], "starred": bool(job.get("starred"))}
                if job.get("sub_id"):
                    child["sub_id"] = job["sub_id"]
                self.jobs.insert(pos + k - 1, child)
                if url in existing:
                    self._mark_existing(child, existing[url])
                    child.update(status="done", phase="已下載過，略過", finished=now)
            job["expanded"] = total
            self.update(jid, playlist_count=total, is_playlist=True, playlist_skipped=len(existing),
                        new_count=len(entries) if job.get("sub_id") else None)
        self._save()
        self._event.set()

    def _run(self, job: dict):
        jid = job["id"]
        s = load_settings()
        out_dir = download_dir(s)
        ffmpeg = find_ffmpeg(s)
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，請先執行 start.bat 自動安裝，或在「設定」指定 ffmpeg 路徑。")

        state = {"n": 0, "last_file": None, "final": None}

        def hook(d):
            if jid in self._cancel:
                raise DownloadCancelled("使用者取消")
            if d["status"] == "downloading":
                fn = d.get("filename")
                if fn != state["last_file"]:
                    state["last_file"] = fn
                    state["n"] += 1
                info = d.get("info_dict") or {}
                kind = "音訊" if info.get("vcodec") == "none" else "影片"
                if info.get("vcodec") == "none" and info.get("acodec") == "none":
                    kind = "檔案"
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = (d.get("downloaded_bytes", 0) / total * 100) if total else 0
                if not total and d.get("fragment_count"):
                    pct = d.get("fragment_index", 0) / d["fragment_count"] * 100
                self.update(jid, phase=f"下載{kind}中", progress=round(pct, 1),
                            speed=fmt_speed(d.get("speed")), eta=fmt_eta(d.get("eta")),
                            size=fmt_size(total))

        def pp_hook(d):
            if jid in self._cancel:
                raise DownloadCancelled("使用者取消")
            name = d.get("postprocessor", "")
            label = {"Merger": "合併影音中", "FFmpegVideoRemuxer": "轉換為 MP4 中",
                     "FFmpegVideoConvertor": "轉換為 MP4 中",
                     "FFmpegSubtitlesConvertor": "轉換字幕格式中"}.get(name)
            if label and d["status"] == "started":
                self.update(jid, phase=label, speed="", eta="")

        def post_hook(path):
            state["final"] = path

        h = int(s.get("max_height") or 0)
        sort = ["lang"] + ([f"res:{h}"] if h else ["res"]) + ["vcodec:h264", "acodec:m4a"]  # lang：優先原始音軌，避免選到 AI 配音
        name_tpl = "%(title).150B [%(id)s].%(ext)s" if s.get("append_id") else "%(title).150B.%(ext)s"
        job_force = bool(job.get("force"))
        copts, cdesc = cookie_opts(s)
        ytlog = YtLogger()
        self._ytlogs[jid] = ytlog
        opts = {
            "paths": {"home": str(out_dir), "temp": str(out_dir / ".tmp")},
            "outtmpl": {"default": name_tpl},
            "windowsfilenames": True,
            "format": "bv*+ba/b",
            "format_sort": sort,
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
            "ffmpeg_location": ffmpeg,
            "noplaylist": bool(job.get("parent")) or not (s.get("playlist") or job.get("is_scan")),
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            "post_hooks": [post_hook],
            "quiet": True, "noprogress": True, "logger": ytlog,
            "retries": 5, "fragment_retries": 10, "concurrent_fragment_downloads": 4,
            "socket_timeout": 30,
            **copts,
        }

        def probe(o):
            with yt_dlp.YoutubeDL(o) as ydl:
                return ydl.extract_info(job["url"], download=False, process=False)

        try:
            info = probe(opts)
        except DownloadCancelled:
            raise
        except Exception as e:
            if copts and re.search(r"cookie|decrypt|dpapi|could not copy|permission denied", str(e), re.I):
                from . import errlog
                errlog.warn("Cookie", f"Cookie 讀取失敗，改用未登入模式：{job.get('url', '')}", e)
                copts, cdesc = {}, "Cookie 讀取失敗，改用無登入模式"
                for k in ("cookiefile", "cookiesfrombrowser"):
                    opts.pop(k, None)
                self.update(jid, warning=f"Cookie 讀取失敗：{short(str(e))}，已改用未登入模式重試")
                info = probe(opts)
            else:
                raise

        # YouTube Cookie 已被輪替失效：帶著失效的登入去下載常會 403 → 拿掉 YouTube／Google cookie 改用未登入重抓
        if ("youtu" in job["url"] and copts.get("cookiefile")
                and any("no longer valid" in w for w in ytlog.warnings)):
            from . import errlog
            old = copts["cookiefile"]
            lines = [ln for ln in _cookie_lines(Path(old)) if not _YT_DOMAIN.search(_cookie_domain(ln))]
            new = Path(tempfile.gettempdir()) / f"movdl_cookies_{uuid.uuid4().hex}.txt"
            new.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
            Path(old).unlink(missing_ok=True)
            copts["cookiefile"] = opts["cookiefile"] = str(new)
            ytlog.warnings = [w for w in ytlog.warnings if "no longer valid" not in w]
            errlog.warn("Cookie", f"YouTube Cookie 已被輪替失效，改用未登入模式下載：{job['url']}")
            self.update(jid, warning="YouTube 登入 Cookie 已失效，本次改用未登入模式下載（公開影片不受影響；"
                                     "會員影片請用擴充功能「從無痕視窗同步 YouTube 登入」）")
            info = probe(opts)

        # 短網址等「轉址型」結果先解析到底，才能判斷是不是播放清單
        for _ in range(3):
            if info.get("_type") in ("url", "url_transparent") and info.get("url"):
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(info["url"], download=False, process=False)
            else:
                break
        if info.get("_type") in ("playlist", "multi_video") and not job.get("parent"):
            self._expand(job, info, s)
            return
        if job.get("is_scan"):
            raise RuntimeError("這個網址不是播放清單或頻道，無法訂閱掃描")

        # 已下載過（依影片 ID／網址比對，改過檔名也認得出來）→ 略過，不重複下載、不重複翻譯
        exist = find_existing(info, job["url"])
        if exist and not job.get("force"):
            self._mark_existing(job, exist)
            return
        if exist and job.get("force"):
            # 強制重新下載：保留備註與 hashtag，移除舊的影片／字幕／資訊檔後重新下載
            om = read_meta(exist)
            job["carry"] = {k: om[k] for k in ("note", "hashtags", "watched", "watched_at", "starred") if om.get(k)}
            for f in [exist, meta_path(exist), *exist.parent.glob(glob.escape(exist.stem) + ".*.srt")]:
                try:
                    Path(f).unlink(missing_ok=True)
                except Exception:
                    pass

        title = info.get("title") or ""
        thumb = info.get("thumbnail") or ""
        if not thumb and info.get("thumbnails"):
            thumb = info["thumbnails"][-1].get("url", "")
        self.update(jid, title=title, thumbnail=thumb, phase="準備下載…", cookie=cdesc,
                    uploader=info.get("uploader") or "", duration=info.get("duration"))

        chosen = pick_subtitle(info) if s.get("subtitles") else None
        if chosen:
            lang, is_auto = chosen
            opts.update({"writesubtitles": not is_auto, "writeautomaticsub": is_auto,
                         "subtitleslangs": [lang], "subtitlesformat": "srt/vtt/best"})
            opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}] + opts["postprocessors"]
        self.update(jid, sub_lang=(chosen[0] + ("（自動）" if chosen[1] else "")) if chosen else "")

        # 檔名只用 Big5 字元（避免舊式看圖／預覽軟體開不了檔）
        id_part = " [%(id)s]" if s.get("append_id") else ""
        base_tpl = "%(title).150B"
        if s.get("big5_names", True) and info.get("title"):
            nm = big5_name(info["title"]) or (info.get("id") or "video")
            nm = nm.encode("utf-8")[:150].decode("utf-8", "ignore").strip()
            base_tpl = nm.replace("%", "%%")
            opts["outtmpl"] = {"default": base_tpl + id_part + ".%(ext)s"}

        # 同名但是「不同影片」→ 檔名自動加上影片 ID，避免誤判為已下載或互相覆蓋
        if not s.get("append_id") and info.get("title"):
            from yt_dlp.utils import sanitize_filename
            guess = sanitize_filename(big5_name(info["title"]) if s.get("big5_names", True) else info["title"],
                                      restricted=False)
            same = next((out_dir / (guess + e) for e in VIDEO_EXTS if (out_dir / (guess + e)).exists()), None)
            if same:
                old_meta = read_meta(same)
                mine = {video_key(info.get("extractor_key") or "", info.get("id") or ""),
                        key_from_url(job["url"]), "url:" + norm_url(job["url"])} - {"", "url:"}
                theirs = {video_key(old_meta.get("extractor_key") or "", old_meta.get("id") or "")} - {""}
                for u in (old_meta.get("webpage_url"), old_meta.get("source_url")):
                    if u:
                        theirs |= {key_from_url(u), "url:" + norm_url(u)} - {""}
                if mine & theirs:
                    opts["overwrites"] = True          # 同一部影片 → 覆蓋
                else:
                    opts["outtmpl"] = {"default": base_tpl + " [%(id)s].%(ext)s"}
                    self.update(jid, warning=((self.get(jid) or {}).get("warning", "") +
                                              " 已有同名的其他影片，檔名自動加上影片 ID").strip())

        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.process_ie_result(info, download=True)

        final = state["final"]
        if not final and result:
            rd = (result.get("requested_downloads") or [{}])[0]
            final = rd.get("filepath")
        if not final:
            # 播放清單或已存在的檔案
            ents = (result or {}).get("entries") or []
            self.update(jid, file="", phase="完成")
            if ents:
                self.update(jid, title=f"{title}（{len(list(ents))} 部）")
            return
        final = Path(final)
        self.update(jid, file=final.name, title=title or final.stem)
        try:
            old = read_meta(final)
            meta = build_meta(result if isinstance(result, dict) and result.get("title") else info, job["url"])
            for k in ("note", "hashtags", "watched", "watched_at", "starred"):
                if old.get(k):
                    meta[k] = old[k]
            for k, v in (job.get("carry") or {}).items():
                if k == "hashtags":
                    have = {x.lower() for x in meta.get("hashtags") or []}
                    meta["hashtags"] = (meta.get("hashtags") or []) + [t for t in v if t.lower() not in have]
                elif not meta.get(k):
                    meta[k] = v
            if (self.get(jid) or job).get("starred"):     # 下載佇列中先標的星號
                meta["starred"] = True
            if job.get("playlist"):
                meta["playlist"] = job["playlist"]
            if job.get("sub_id"):
                meta["sub_id"] = job["sub_id"]
            for t in job.get("auto_tags") or []:
                tags = meta.setdefault("hashtags", [])
                if t.lower() not in {x.lower() for x in tags}:
                    tags.append(t)
            write_meta(final, meta)
            add_history(meta_keys(meta, job["url"]))
        except Exception:
            traceback.print_exc()

        if chosen:
            base = final.with_suffix("")
            raw = [Path(p) for p in glob.glob(glob.escape(str(base)) + ".*.srt")]
            raw = [p for p in raw if not re.search(r"\.zh-TW(-[\w-]+)?\.srt$", p.name)] or raw
            raw.sort(key=lambda p: f".{chosen[0]}." not in p.name)
            if raw:
                self.update(jid, phase="處理字幕中（轉台灣正體／翻譯）", progress=0, speed="", eta="")
                engine = s.get("translator", "google")
                eng_label = {"llm": "AI", "codex": "Codex", "local": "本機"}.get(engine, "Google")
                ctx = "\n".join(filter(None, [title, (info.get("description") or "")[:1200]]))
                try:
                    outs = subs.process_subtitle(
                        raw[0], base, chosen[0], s.get("en_mode", "translate"),
                        s.get("canto_written", True),
                        progress=lambda p, lb="": self.update(jid, phase=f"翻譯字幕中（{eng_label}）{lb}", progress=round(p * 100, 1)),
                        engine=engine, context=ctx)
                except subs.TranslationFailed as e:
                    outs = [Path(p) for p in glob.glob(glob.escape(str(base)) + ".*.srt")
                            if not re.search(r"\.zh-TW(-[\w-]+)?\.srt$", p)]
                    self.update(jid, warning=((self.get(jid) or {}).get("warning", "") + " " + str(e)).strip())
                self.update(jid, subs=[o.name for o in outs])
            else:
                chosen = None      # 字幕沒抓到 → 視同沒有字幕
        if s.get("subtitles") and not chosen:
            has_srt = glob.glob(glob.escape(str(final.with_suffix(""))) + ".*.srt")
            if has_srt:
                self.update(jid, subs=[Path(p).name for p in has_srt])
            elif not s.get("asr_auto", True):
                self.update(jid, warning=((self.get(jid) or {}).get("warning", "") + " 這部影片沒有字幕").strip())
            else:
                # 沒有字幕可下載 → 語音辨識
                self.update(jid, phase="沒有字幕，準備語音辨識…", progress=0, speed="", eta="", sub_lang="語音辨識")
                try:
                    outs = run_asr(final, s, "\n".join(filter(None, [title, (info.get("description") or "")[:1200]])),
                                   progress=lambda p, lb="": self.update(jid, phase=lb or "語音辨識中", progress=round(p * 100, 1)),
                                   cancelled=lambda: jid in self._cancel)
                    self.update(jid, subs=[o.name for o in outs])
                except asr.Cancelled:
                    raise DownloadCancelled("使用者取消")
                except Exception as e:      # 辨識失敗不影響影片本身
                    from . import errlog
                    errlog.error("語音辨識", f"下載後自動語音辨識失敗：{job.get('url', '')}", e)
                    self.update(jid, warning=((self.get(jid) or {}).get("warning", "") +
                                              f" 語音辨識失敗：{short(str(e), 200)}").strip())
        for k in ("cookiefile",):
            if k in copts:
                try:
                    os.remove(copts[k])
                except Exception:
                    pass


# ------------------------------------------------------------------ 工具
def fmt_speed(b):
    return f"{fmt_size(b)}/s" if b else ""


def fmt_size(b):
    if not b:
        return ""
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def fmt_eta(s):
    if s is None:
        return ""
    s = int(s)
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def youtube_auth_hint(msg: str, warnings: list[str]) -> str:
    """YouTube 需要登入／會員的錯誤：依 Cookie 狀態補上真正可能的原因"""
    s = load_settings()
    if s.get("cookie_mode", "extension") != "extension":
        return msg
    lines, src = effective_cookie_lines()
    st = youtube_login_state(lines)
    if any("no longer valid" in w or "rotated" in w for w in warnings):
        why = ("【原因】YouTube 已讓這份 Cookie 失效：在一般分頁登入的 Cookie 會被 YouTube 定期輪替，"
               "程式拿到的那份很快就不能用。請改用擴充功能的「從無痕視窗同步 YouTube 登入」（做法見使用說明），之後就不會再失效。")
    elif not st["logged_in"]:
        why = ("【原因】同步過來的 Cookie 裡沒有 YouTube 登入資訊"
               f"（LOGIN_INFO：{'有' if st['login_info'] else '無'}、SAPISID：{'有' if st['sapisid'] else '無'}）。"
               "請在安裝擴充功能的那個 Chrome 設定檔登入 YouTube，再按「同步 Cookie」。")
    else:
        why = (f"【檢查】Cookie 是 YouTube 登入狀態（來源：{src}），但 YouTube 仍不給看。"
               "最常見的是 Cookie 已被輪替失效：請改用擴充功能的「從無痕視窗同步 YouTube 登入」後重試。")
    return f"{msg}\n{why}"


class YtLogger:
    """收集 yt-dlp 的警告（例如「Cookie 已失效」），失敗時寫進錯誤紀錄"""

    def __init__(self):
        self.warnings: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg))
        if msg not in self.warnings and len(self.warnings) < 30:
            self.warnings.append(msg)

    def error(self, msg):
        pass


def short(s, n=160):
    s = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return s if len(s) <= n else s[:n] + "…"


def friendly_error(msg: str) -> str:
    m = re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR: ", "")
    tips = [
        (r"members-only|channel's members|Join this channel",
         "這是頻道會員限定影片。需要用「已加入該頻道會員、且等級足夠」的 YouTube 帳號登入 Chrome，再按擴充功能「同步 Cookie」後重試；沒有加入會員則無法下載。"),
        (r"Sign in to confirm|login required|log in|private|registered users|cookies", "需要登入：請確認已在 Chrome 登入該網站，並按擴充功能「同步 Cookie」。"),
        (r"Unable to connect|Connection refused|timed out|getaddrinfo|Name or service not known|proxy", "網路連線失敗，請確認網路是否正常。"),
        (r"Unsupported URL", "不支援此網址，請確認是影片頁面的網址。"),
        (r"HTTP Error 429|Too Many Requests", "請求太頻繁被網站暫時限制，請稍後再試。"),
        (r"HTTP Error 403", "網站拒絕存取（403），請更新 yt-dlp 或同步 Cookie 後重試。"),
        (r"Requested format is not available", "找不到可用的影片格式。"),
        (r"ffmpeg", "ffmpeg 發生問題，請確認已正確安裝。"),
    ]
    for rx, t in tips:
        if re.search(rx, m, re.I):
            return f"{t}\n（原始訊息：{short(m, 300)}）"
    return short(m, 400)
