"""OpenVINO 辨識引擎（等級 C/D）。

以 openvino-genai 的 WhisperPipeline 執行；device=auto 時先試 Intel 顯示卡，失敗自動改用 CPU。
30 秒視窗逐段辨識的做法沿用 mov_downloader/app/transcribe.py（已實際驗證可用）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .audio import SR
from .config import ov_cache_dir
from .ovcompat import problem as ov_problem

WINDOW = 30  # Whisper 一次處理 30 秒
MODEL_FILES = ("openvino_encoder_model.xml", "openvino_decoder_model.xml",
               "openvino_tokenizer.xml", "openvino_detokenizer.xml")

Segment = tuple[float, float, str]
_lock = threading.Lock()
_cache: dict[tuple[str, str], object] = {}


class EngineError(Exception):
    pass


def check_model(model_dir: Path) -> str:
    if not model_dir.is_dir():
        return f"找不到模型資料夾：{model_dir}（請先執行 setup.cmd）"
    miss = [f for f in MODEL_FILES if not (model_dir / f).exists()]
    return ("模型資料夾缺少檔案：" + "、".join(miss)) if miss else ""


def devices() -> list[str]:
    try:
        import openvino as ov
        return list(ov.Core().available_devices)
    except Exception:
        return []


def load(model_dir: Path, device: str = "auto", log: Callable[[str], None] = print):
    """載入模型並回傳 (pipeline, 實際裝置, 載入秒數)。同一模型＋裝置只載入一次。"""
    import openvino_genai as og

    problem = check_model(model_dir) or ov_problem(model_dir)
    if problem:
        raise EngineError(problem)
    order = [device.upper()]
    if device.lower() == "auto":
        order = (["GPU"] if any(d.startswith("GPU") for d in devices()) else []) + ["CPU"]
    last = None
    for dev in order:
        key = (str(model_dir), dev)
        if key in _cache:
            return _cache[key], dev, 0.0
        t0 = time.perf_counter()
        try:
            log(f"載入模型 {model_dir.name}（{dev}，第一次可能需要 1～3 分鐘）...")
            try:
                pipe = og.WhisperPipeline(str(model_dir), dev, CACHE_DIR=str(ov_cache_dir()))
            except TypeError:
                pipe = og.WhisperPipeline(str(model_dir), dev)
            _cache[key] = pipe
            return pipe, dev, time.perf_counter() - t0
        except Exception as e:  # 例如顯示卡記憶體不足 → 換下一個裝置
            log(f"模型無法在 {dev} 載入，改試下一個裝置：{str(e)[:200]}")
            last = e
    raise EngineError(f"模型載入失敗：{str(last)[:300]}")


def unload() -> None:
    """釋放已載入的模型（實測時逐一測試，避免同時佔用太多記憶體）"""
    import gc
    _cache.clear()
    gc.collect()


def transcribe(pipe, audio, language: str = "auto", initial_prompt: str = "",
               progress: Callable[[float, str], None] | None = None) -> list[Segment]:
    """以 30 秒視窗逐段辨識；依上一段最後一句的結束時間決定下一段起點，避免把句子切斷。"""
    total = len(audio) / SR
    kw: dict = {"return_timestamps": True, "task": "transcribe"}
    if language and language != "auto":
        kw["language"] = f"<|{language}|>"
    if initial_prompt:
        kw["initial_prompt"] = initial_prompt
    segs: list[Segment] = []
    seek = 0.0
    with _lock:
        while seek < total - 0.2:
            win = audio[int(seek * SR): int((seek + WINDOW) * SR)]
            last_window = seek + WINDOW >= total
            try:
                res = pipe.generate(win, **kw)
            except TypeError:
                kw.pop("initial_prompt", None)  # 舊版 openvino-genai 不支援提示詞
                res = pipe.generate(win, **kw)
            except Exception as e:
                raise EngineError(f"辨識失敗：{str(e)[:300]}")
            chunks: list[Segment] = []
            for c in (getattr(res, "chunks", None) or []):
                a, b, t = float(c.start_ts), float(c.end_ts), (c.text or "").strip()
                if b < 0 or b > WINDOW:  # 最後一句沒有結束時間
                    b = min(WINDOW, len(win) / SR)
                if t and b > a:
                    chunks.append((a, b, t))
            if not chunks:
                text = "".join(getattr(res, "texts", []) or []).strip()
                if text:
                    chunks = [(0.0, len(win) / SR, text)]
            advance = WINDOW
            if not last_window and len(chunks) > 1 and chunks[-1][1] >= WINDOW - 0.5:
                chunks = chunks[:-1]  # 視窗尾端被切到的句子留給下一段重新辨識
                advance = max(chunks[-1][1], 1.0)
            elif not last_window and chunks and chunks[-1][1] < WINDOW - 0.5:
                advance = max(chunks[-1][1], WINDOW * 0.5)  # 後面是靜音／音樂 → 往前跳
            for a, b, t in chunks:
                if segs and t == segs[-1][2]:  # 模型偶爾重複同一句
                    continue
                segs.append((seek + a, seek + b, t))
            seek += advance
            if progress:
                progress(min(seek / total, 1.0), "")
    return segs
