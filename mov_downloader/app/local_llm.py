"""本機 LLM（1.25.0，移植自 transcribe 專案的 translate.py／llm.py）：完全在你的電腦上執行，不上傳、不需要金鑰。

- 翻譯：Google TranslateGemma 4B（OpenVINO int8）── <模型位置>\\llm\\translategemma-4b-it
- 重點摘要：Google Gemma 4（Intel 官方 OpenVINO int4 版）── <模型位置>\\llm\\gemma-4-<大小>-it-int4-ov
  依電腦記憶體建議 E2B（16 GB 以下）／E4B（16～63 GB）／26B-A4B（64 GB 以上），也可在設定指定
- 執行：openvino_genai.VLMPipeline，有 Intel 顯示卡用顯示卡，失敗自動改 CPU
- 同一時間只留一個模型在記憶體（翻譯與摘要輪流用時會自動切換）；閒置 10 分鐘自動釋放
- 載入前一律經 ovcompat 比對 OpenVINO 版本，避免新版模型讓程式當掉
"""
from __future__ import annotations

import ctypes
import gc
import json
import os
import re
import threading
import time
from typing import Callable

from . import modelpaths as mp

IDLE_SEC = 600
BATCH = 12          # TranslateGemma 一次送 12 句（保留前後文、較通順）

_lock = threading.RLock()
_pipe = None
_which = ""         # 目前載入的模型 key
_device = ""
_last = 0.0
_sdpa = False       # 顯示卡的連續批次模式不支援時，改用 SDPA 注意力機制


class LocalLLMError(RuntimeError):
    pass


# ------------------------------------------------------------------ 下載 Gemma 4（1.26.0，移植自 transcribe 的 llm.py）
# Intel 官方 OpenVINO int4 版，Hugging Face 公開、Apache-2.0、不需帳號或 Token。
REPOS = {"e2b": "OpenVINO/gemma-4-E2B-it-int4-ov", "e4b": "OpenVINO/gemma-4-E4B-it-int4-ov",
         "26b": "OpenVINO/gemma-4-26b-a4b-it-int4-ov"}
_dl = {"state": "idle", "key": "", "progress": 0.0, "msg": "", "error": "", "total_gb": None}
_NOWIN = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


def download_status() -> dict:
    return dict(_dl)


def _dir_size(d) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


def _ensure_hub():
    """huggingface_hub 只有下載模型才用得到：第一次下載時才安裝（只裝用得到的東西）"""
    try:
        import huggingface_hub  # noqa: F401
        return
    except ImportError:
        pass
    import subprocess
    import sys
    _dl["msg"] = "第一次下載，安裝下載元件（huggingface_hub）…"
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"],
                       capture_output=True, text=True, creationflags=_NOWIN)
    if r.returncode != 0:
        raise LocalLLMError("下載元件 huggingface_hub 安裝失敗，請確認網路：" + (r.stderr or r.stdout)[-300:])


def start_download(key: str) -> dict:
    """在背景下載 Gemma 4 到模型位置；中斷後再按一次會從中斷處續傳。只下載、不刪除任何檔案"""
    if key not in REPOS:
        raise LocalLLMError("只有 Gemma 4 可以從這裡下載")
    if _dl["state"] == "running":
        return dict(_dl)
    if mp.ready(key):
        return {**_dl, "state": "done", "key": key, "msg": "已經有這個模型"}
    repo, d = REPOS[key], mp.path(key)
    _dl.update(state="running", key=key, progress=0.0, msg="準備下載…", error="", total_gb=None)

    def run():
        from . import errlog
        done = threading.Event()
        try:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise LocalLLMError(f"無法在模型位置建立資料夾 {d}：{e}。請確認模型位置是否正確、有沒有寫入權限") from None
            _ensure_hub()
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from huggingface_hub import HfApi, snapshot_download
            try:
                info = HfApi().model_info(repo, files_metadata=True)
                total = sum((x.size or 0) for x in info.siblings)
            except Exception:
                total = 0
            _dl["total_gb"] = round(total / 1e9, 1) if total else None
            if total:
                import shutil
                free = shutil.disk_usage(d).free
                need = total - _dir_size(d)
                if free < need * 1.1:
                    raise LocalLLMError(f"磁碟空間不足：需要約 {need / 1e9:.1f} GB，{d.anchor or d} 只剩 {free / 1e9:.1f} GB")

            def watch():
                while not done.wait(2):
                    got = _dir_size(d)
                    if total:
                        _dl["progress"] = min(got / total, 0.99)
                        _dl["msg"] = f"下載中 {got / 1e9:.1f}／{total / 1e9:.1f} GB"
                    else:
                        _dl["msg"] = f"下載中 {got / 1e9:.1f} GB"
            threading.Thread(target=watch, daemon=True).start()
            print(f"開始下載 {mp.label(key)}：{repo} → {d}")
            snapshot_download(repo, local_dir=str(d))
            done.set()
            if not (d / "openvino_language_model.xml").exists():
                raise LocalLLMError("下載完成，但找不到 openvino_language_model.xml，模型格式可能已變更")
            (d / ".download_ok").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            _dl.update(state="done", progress=1.0, msg=f"{mp.label(key)} 下載完成")
        except Exception as e:
            done.set()
            errlog.error("本機 AI", f"下載 {mp.label(key)} 失敗", e)
            _dl.update(state="error", error=str(e)[:400], msg="下載失敗（再按一次會從中斷處續傳）")
    threading.Thread(target=run, daemon=True).start()
    return dict(_dl)


class Cancelled(Exception):
    pass


def _settings() -> dict:
    from . import downloader as dl
    return dl.load_settings()


# ------------------------------------------------------------------ 記憶體與模型挑選
def ram_gb() -> float:
    try:
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2**30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except Exception:
        return 0.0


def recommend(ram: float | None = None) -> str:
    ram = ram_gb() if ram is None else ram
    return "26b" if ram >= 60 else "e4b" if ram >= 15 else "e2b"


def gemma_key(s: dict | None = None) -> str:
    """摘要用哪個 Gemma 4：設定有指定就用；自動時依記憶體建議，建議的沒有就用已有的"""
    s = s or _settings()
    k = s.get("local_llm") or "auto"
    if k in ("e2b", "e4b", "26b"):
        return k
    rec = recommend()
    if mp.ready(rec, s):
        return rec
    order = ["e4b", "e2b"] + (["26b"] if ram_gb() >= 60 else [])
    return next((x for x in order if mp.ready(x, s)), rec)


def status() -> dict:
    s = _settings()
    ram = ram_gb()
    g = gemma_key(s)
    return {"ram_gb": round(ram, 1), "recommended": recommend(ram), "gemma": g, "gemma_label": mp.label(g),
            "gemma_ready": mp.ready(g, s), "translate_ready": mp.ready("translategemma", s),
            "loaded": mp.label(_which) if _pipe is not None and _which else "", "device": _device,
            "download": dict(_dl), "downloadable": list(REPOS)}


def translate_problem() -> str:
    if mp.ready("translategemma"):
        return ""
    return (f"找不到本機翻譯模型 TranslateGemma：{mp.path('translategemma')}。"
            "請向管理者索取模型位置裡的 llm\\translategemma-4b-it 資料夾（transcribe 專案的母機已有），"
            "或到「設定 → 本機 AI 模型」確認模型位置。")


def summary_problem() -> str:
    k = gemma_key()
    if mp.ready(k):
        return ""
    return (f"找不到本機摘要模型 {mp.label(k)}：{mp.path(k)}。"
            "請到「設定 → 本機 AI 模型」按該模型的「下載」（不需帳號），"
            "或改選已有的大小、確認模型位置。")


# ------------------------------------------------------------------ 載入
def _unload():
    global _pipe, _which
    _pipe, _which = None, ""
    gc.collect()


def _load(which: str, log: Callable[[str], None] = print):
    """which：translategemma 或 e2b／e4b／26b。換模型時先釋放舊的，同一時間只留一個在記憶體"""
    global _pipe, _which, _device
    if _pipe is not None and _which == which:
        return _pipe
    _unload()
    d = mp.path(which)
    if not mp.ready(which):
        raise LocalLLMError(translate_problem() if which == "translategemma" else summary_problem())
    from . import transcribe as asr
    asr.ensure_installed()                 # 與語音辨識共用 openvino-genai（2026.4 以上）
    from .ovcompat import problem as ov_problem
    bad = ov_problem(d)
    if bad:
        raise LocalLLMError(bad)
    import openvino as ov
    import openvino_genai as ov_genai
    pref = (_settings().get("local_device") or "auto").upper()
    dev = "GPU" if pref in ("AUTO", "GPU") and any(a.startswith("GPU") for a in ov.Core().available_devices) else "CPU"
    props = {"CACHE_DIR": str(mp.ov_cache_dir())}
    if _sdpa:
        props["ATTENTION_BACKEND"] = "SDPA"
    t0 = time.time()
    try:
        _pipe = ov_genai.VLMPipeline(str(d), dev, **props)
    except Exception as e:
        if dev != "GPU":
            raise LocalLLMError(f"載入 {mp.label(which)} 失敗：{str(e)[:300]}") from None
        from . import errlog
        errlog.warn("本機 AI", f"顯示卡載入 {mp.label(which)} 失敗，改用 CPU", e)
        dev = "CPU"
        _pipe = ov_genai.VLMPipeline(str(d), dev, **props)
    if which == "translategemma":   # 提示已依模型範本組好：範本改成原樣輸出
        try:
            _pipe.set_chat_template("{% for message in messages %}{{ message['content'] }}{% endfor %}")
        except Exception:
            pass
    _which = which
    _device = "Intel 顯示卡" if dev == "GPU" else "CPU"
    log(f"載入 {mp.label(which)}（{_device}，{time.time() - t0:.0f} 秒）")
    threading.Thread(target=_idle_unload, daemon=True).start()
    return _pipe


def _idle_unload():
    while True:
        time.sleep(30)
        with _lock:
            if _pipe is None:
                return
            if time.time() - _last > IDLE_SEC:
                _unload()
                return


def _generate(which: str, prompt: str, max_new: int, log: Callable[[str], None] = print) -> str:
    import openvino_genai as ov_genai
    global _sdpa, _last
    pipe = _load(which, log)
    cfg = ov_genai.GenerationConfig()
    cfg.max_new_tokens = max_new
    cfg.do_sample = False
    if which == "translategemma":
        cfg.repetition_penalty = 1.05
    try:
        cfg.apply_chat_template = False    # 提示已組好，不要再套對話範本
    except Exception:
        pass
    try:
        out = pipe.generate(prompt, generation_config=cfg)
    except Exception as e:
        if "Chat mode" not in str(e) or _sdpa:
            raise LocalLLMError(f"{mp.label(which)} 執行失敗：{str(e)[:300]}") from None
        log("  顯示卡的連續批次模式不支援，改用 SDPA 重新載入")
        _sdpa = True
        _unload()
        out = _load(which, log).generate(prompt, generation_config=cfg)
    _last = time.time()
    return out.texts[0] if hasattr(out, "texts") else str(out)


# ------------------------------------------------------------------ 重點摘要用：Gemma 4 對話
def chat(messages: list[dict], max_new: int = 2048, log: Callable[[str], None] = print) -> str:
    """messages：OpenAI 格式；system 與 user 合併成一則（Gemma 的對話範本以 user／model 為主）"""
    body = "\n\n".join(m["content"] for m in messages if m.get("content"))
    with _lock:
        key = gemma_key()
        pipe = _load(key, log)
        try:
            prompt = pipe.get_tokenizer().apply_chat_template([{"role": "user", "content": body}],
                                                              add_generation_prompt=True)
        except Exception:
            prompt = f"<start_of_turn>user\n{body}<end_of_turn>\n<start_of_turn>model\n"
        s = _generate(key, prompt, max_new, log)
    s = re.sub(r"<[^>]{1,24}>", "", s)          # 結尾標記
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip())
    return s.strip()


# ------------------------------------------------------------------ 翻譯用：TranslateGemma
def detect(texts: list[str]) -> str:
    """依文字判斷原文語言：假名→日文、諺文→韓文、西里爾→俄文、泰文→泰文，其他→英文（拉丁字母最常見）"""
    s = "".join(texts)[:20000]
    kana = len(re.findall(r"[぀-ヿ]", s))
    hangul = len(re.findall(r"[가-힯]", s))
    latin = len(re.findall(r"[A-Za-z]", s))
    cyr = len(re.findall(r"[Ѐ-ӿ]", s))
    thai = len(re.findall(r"[฀-๿]", s))
    if kana > len(s) * 0.05:
        return "ja"
    if hangul > len(s) * 0.2:
        return "ko"
    if cyr > latin:
        return "ru"
    if thai > latin:
        return "th"
    return "en"


def _templates() -> dict:
    try:
        return json.loads((mp.path("translategemma") / "prompt_templates.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _prompt(src: str, text: str) -> str:
    t = _templates()
    tpl = (t.get("templates") or {}).get(src) or (t.get("templates") or {}).get("en")
    if not tpl:
        raise LocalLLMError("TranslateGemma 模型資料夾的 prompt_templates.json 內容不完整，請向管理者重新索取模型")
    return tpl.replace(t.get("mark", "@@TEXT@@"), text)


def _clean(s: str) -> str:
    s = s.replace("<end_of_turn>", "")
    return re.sub(r"^\s*(\d+[\.\)、]\s*)", "", s.strip()).strip()   # 模型偶爾自己加上的編號


def translate(texts: list[str], progress=None, cancelled: Callable[[], bool] = lambda: False,
              log: Callable[[str], None] = print) -> list[str]:
    """每句翻成中文，回傳與 texts 等長的譯文（簡轉正、台灣用語由字幕流程統一處理）"""
    n = len(texts)
    if not n:
        return []
    src = detect(texts)
    out: list[str] = []
    t0 = time.time()
    with _lock:
        for i in range(0, n, BATCH):
            if cancelled():
                raise Cancelled()
            chunk = [x.replace("\n", " ").strip() for x in texts[i:i + BATCH]]
            if progress:
                progress(i / n, f"{i}/{n} 句")
            res = _generate("translategemma", _prompt(src, "\n".join(chunk)), 48 * len(chunk) + 64, log)
            lines = [ln for ln in res.replace("<end_of_turn>", "").splitlines() if ln.strip()]
            if len(lines) != len(chunk):        # 行數對不上：逐句翻，確保每句對得上
                lines = [_generate("translategemma", _prompt(src, x), 96, log).replace("\n", " ") if x else ""
                         for x in chunk]
            out.extend(_clean(x) for x in lines)
    if progress:
        progress(1.0, f"{n}/{n} 句")
    log(f"本機翻譯完成：{n} 句（{time.time() - t0:.0f} 秒，{_device}）")
    return out
