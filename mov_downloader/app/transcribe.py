"""語音辨識：影片沒有字幕時，用 ffmpeg 抽出聲音（MP3），再以 Breeze-ASR-25（OpenVINO 格式）轉成 SRT。

流程：MP4 ──ffmpeg──▶ MP3 ──ffmpeg 解碼──▶ 16kHz 單聲道 ──Breeze-ASR-25──▶ SRT
模型以 openvino-genai 的 WhisperPipeline 執行；第一次使用時自動安裝該套件。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from .fsutil import atomic_write_text

SR = 16000
WINDOW = 30            # Whisper 一次處理 30 秒
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_pipe = None
_pipe_key: tuple | None = None
_lock = threading.Lock()          # 同一時間只跑一個辨識（模型很大）

OV_REQ = "openvino-genai>=2026.4"   # 模型用 2026.4 轉出；舊版載入會在底層當掉（見 ovcompat.py）

MODEL_FILES = ("openvino_encoder_model.xml", "openvino_decoder_model.xml",
               "openvino_tokenizer.xml", "openvino_detokenizer.xml")


class TranscribeError(Exception):
    pass


class Cancelled(Exception):
    pass


class NoSpeech(TranscribeError):
    """影片沒有聲音或沒有人說話：重試也不會有結果"""


def default_model_dir() -> str:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return str(Path(base) / "KeyboardFree" / "models" / "ov" / "breeze-asr-25")


def check_model(model_dir: str) -> str:
    """回傳問題說明；沒問題回傳空字串"""
    d = Path(model_dir or "")
    if not model_dir or not d.is_dir():
        return f"找不到模型資料夾：{model_dir or '（未設定）'}"
    miss = [f for f in MODEL_FILES if not (d / f).exists()]
    if miss:
        return "模型資料夾缺少檔案：" + "、".join(miss)
    if is_installed():
        from .ovcompat import problem as ov_problem
        return ov_problem(d)
    return ""


def is_installed() -> bool:
    try:
        import openvino_genai  # noqa: F401
        return True
    except Exception:
        return False


def ensure_installed(progress=None) -> None:
    if is_installed():
        return
    if progress:
        progress(0, "安裝語音辨識元件中（第一次需要幾分鐘）")
    if sys.version_info < (3, 10):   # 3.9 只裝得到 OpenVINO 2025.3，載入新版模型會當掉
        from .ovcompat import fix_hint
        raise TranscribeError("語音辨識需要 Python 3.10 以上。" + fix_hint())
    r = subprocess.run([sys.executable, "-m", "pip", "install", OV_REQ],
                       capture_output=True, text=True, creationflags=_NOWIN)
    if r.returncode != 0 or not is_installed():
        raise TranscribeError("語音辨識元件（openvino-genai）安裝失敗，請確認網路後再試。" + (r.stderr or "")[-300:])


def devices() -> list[str]:
    try:
        import openvino as ov
        return list(ov.Core().available_devices)
    except Exception:
        return []


def _load(model_dir: str, device: str, progress=None, model_label: str = "Breeze-ASR-25"):
    """載入模型（很大，載入一次後留在記憶體）。device=auto 時先試顯示卡，失敗改用處理器"""
    global _pipe, _pipe_key
    from .ovcompat import problem as ov_problem
    bad = ov_problem(model_dir)
    if bad:                         # 先擋下來：直接載入會讓整個程式當掉、攔不到錯誤
        raise TranscribeError(bad)
    import openvino_genai as og

    order = [device.upper()]
    if device.lower() == "auto":
        avail = devices()
        order = [d for d in ("GPU",) if any(a.startswith(d) for a in avail)] + ["CPU"]
    last = None
    for dev in order:
        if _pipe is not None and _pipe_key == (model_dir, dev):
            return _pipe, dev
        try:
            if progress:
                progress(0, f"載入 {model_label} 模型中（{dev}，第一次約需 1～3 分鐘）")
            _pipe = og.WhisperPipeline(model_dir, dev)
            _pipe_key = (model_dir, dev)
            return _pipe, dev
        except Exception as e:      # 例如顯示卡記憶體不足 → 換下一個裝置
            from . import errlog
            errlog.warn("語音辨識", f"模型無法在 {dev} 載入，改試下一個裝置", e)
            last = e
            _pipe, _pipe_key = None, None
    raise TranscribeError(f"模型載入失敗：{str(last)[:300]}")


# ------------------------------------------------------------------ 聲音處理
def extract_mp3(ffmpeg: str, video: Path, mp3: Path) -> None:
    r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR),
                        "-c:a", "libmp3lame", "-b:a", "64k", str(mp3)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
    if r.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
        mp3.unlink(missing_ok=True)
        if "does not contain any stream" in (r.stderr or ""):
            raise NoSpeech("這部影片沒有聲音，無法辨識")
        raise TranscribeError("無法從影片抽出聲音。" + (r.stderr or "")[-200:])


def load_audio(ffmpeg: str, src: Path):
    import numpy as np
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(src), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
    if r.returncode != 0 or not r.stdout:
        raise TranscribeError("聲音解碼失敗。" + r.stderr.decode("utf-8", "ignore")[-200:])
    return np.frombuffer(r.stdout, dtype=np.float32)


# ------------------------------------------------------------------ 辨識
_PUNCT = "，。！？；、,.!?;"


def _split_long(start: float, end: float, text: str, max_chars: int = 26) -> list[tuple[float, float, str]]:
    """一句太長時，依標點切成幾段並按字數比例分配時間，字幕才不會一次塞滿畫面"""
    text = text.strip()
    cjk = len(re.findall(r"[㐀-鿿]", text))
    limit = max_chars if cjk > len(text) * 0.3 else max_chars * 3
    if len(text) <= limit:
        return [(start, end, text)]
    parts, cur = [], ""
    for ch in text:
        cur += ch
        if ch in _PUNCT and len(cur) >= limit * 0.5:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    out = []
    for p in parts:                       # 沒標點的長句再硬切
        while len(p) > limit * 1.4:
            cut = p.rfind(" ", 0, limit) if cjk <= len(text) * 0.3 else limit
            cut = cut if cut > 0 else limit
            out.append(p[:cut])
            p = p[cut:]
        out.append(p)
    out = [p.strip() for p in out if p.strip()]
    total = sum(len(p) for p in out) or 1
    res, t = [], start
    for p in out:
        d = (end - start) * len(p) / total
        res.append((t, t + d, p.strip(_PUNCT[:7] + " ") if cjk else p))
        t += d
    return [r for r in res if r[2]]


def transcribe_audio(pipe, audio, language: str = "auto", progress=None, cancelled=None) -> list[tuple[float, float, str]]:
    """以 30 秒視窗逐段辨識；用上一段最後一句的結束時間決定下一段從哪裡開始，避免把句子切斷"""
    total = len(audio) / SR
    kw = {"return_timestamps": True, "task": "transcribe"}
    if language and language != "auto":
        kw["language"] = f"<|{language}|>"
    segs: list[tuple[float, float, str]] = []
    seek = 0.0
    while seek < total - 0.2:
        if cancelled and cancelled():
            raise Cancelled()
        win = audio[int(seek * SR): int((seek + WINDOW) * SR)]
        last_window = seek + WINDOW >= total
        try:
            res = pipe.generate(win, **kw)
        except Exception as e:
            raise TranscribeError(f"辨識失敗：{str(e)[:300]}")
        chunks = []
        for c in (getattr(res, "chunks", None) or []):
            a, b, t = float(c.start_ts), float(c.end_ts), (c.text or "").strip()
            if b < 0 or b > WINDOW:          # 最後一句沒有結束時間
                b = min(WINDOW, len(win) / SR)
            if t and b > a:
                chunks.append((a, b, t))
        if not chunks:
            text = "".join(getattr(res, "texts", []) or []).strip()
            if text:
                chunks = [(0.0, len(win) / SR, text)]
        # 視窗尾端被切到的最後一句留給下一個視窗重新辨識
        advance = WINDOW
        if not last_window and len(chunks) > 1 and chunks[-1][1] >= WINDOW - 0.5:
            chunks = chunks[:-1]
            advance = max(chunks[-1][1], 1.0)
        elif not last_window and chunks and chunks[-1][1] < WINDOW - 0.5:
            advance = max(chunks[-1][1], WINDOW * 0.5)      # 後面是靜音／音樂 → 往前跳
        for a, b, t in chunks:
            if segs and t == segs[-1][2]:                    # 模型偶爾重複同一句
                continue
            segs.extend(_split_long(seek + a, seek + b, t))
        seek += advance
        if progress:
            progress(min(seek / total, 1.0), f"{_fmt(min(seek, total))}／{_fmt(total)}")
    return segs


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _ts(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(segs: list[tuple[float, float, str]]) -> str:
    out = []
    for i, (a, b, t) in enumerate(segs, 1):
        out.append(f"{i}\n{_ts(a)} --> {_ts(max(b, a + 0.3))}\n{t}\n")
    return "\n".join(out)


def transcribe_video(video: Path, ffmpeg_dir: str, model_dir: str, device: str = "auto", language: str = "auto",
                     keep_mp3: bool = False, progress=None, cancelled=None, model_label: str = "Breeze-ASR-25") -> Path:
    """回傳產生的原始 SRT 路徑（<影片名>.asr.srt），後續交給字幕流程轉台灣用語或翻譯"""
    problem = check_model(model_dir)
    if problem:
        raise TranscribeError(problem)
    ffmpeg = str(Path(ffmpeg_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
    ensure_installed(progress)
    with _lock:
        mp3 = video.with_suffix(".mp3")
        made_mp3 = not mp3.exists()
        try:
            if progress:
                progress(0, "抽出聲音（MP4 → MP3）")
            if made_mp3:
                extract_mp3(ffmpeg, video, mp3)
            audio = load_audio(ffmpeg, mp3)
            if len(audio) < SR * 0.5:
                raise TranscribeError("影片聲音太短，無法辨識")
            pipe, dev = _load(model_dir, device, progress, model_label)
            segs = transcribe_audio(pipe, audio, language,
                                    progress=(lambda f, lb="": progress(f, f"語音辨識中（{model_label}，{dev}）{lb}")) if progress else None,
                                    cancelled=cancelled)
        finally:
            if made_mp3 and not keep_mp3:
                try:
                    mp3.unlink(missing_ok=True)
                except Exception:
                    pass
        if not segs:
            raise NoSpeech("沒有辨識出任何語音（影片可能只有音樂或沒有說話）")
        out = Path(str(video.with_suffix("")) + ".asr.srt")
        atomic_write_text(out, to_srt(segs))
        return out
