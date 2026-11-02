"""聲音處理：檢查檔案、轉 MP3（保存用）、解碼成 16kHz 單聲道（辨識用）。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import find_ffmpeg, find_ffprobe

SR = 16000
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".wma", ".opus", ".amr"}


class AudioError(Exception):
    pass


def _run(cmd: list[str], binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=not binary, stdin=subprocess.DEVNULL,
                          creationflags=_NOWIN, **({} if binary else {"encoding": "utf-8", "errors": "replace"}))


def probe(src: Path) -> dict:
    """回傳 {'duration': 秒, 'has_audio': bool, 'has_video': bool}"""
    if not src.exists():
        raise AudioError(f"找不到檔案：{src}")
    r = _run([find_ffprobe(), "-v", "error", "-show_entries", "format=duration:stream=codec_type",
              "-of", "json", str(src)])
    if r.returncode != 0:
        raise AudioError("無法讀取檔案（可能已損毀或不是影音檔）。" + (r.stderr or "")[-200:])
    info = json.loads(r.stdout or "{}")
    types = {s.get("codec_type") for s in info.get("streams", [])}
    dur = float(info.get("format", {}).get("duration") or 0)
    return {"duration": dur, "has_audio": "audio" in types, "has_video": "video" in types}


def to_mp3(src: Path, mp3: Path, bitrate: str = "128k") -> Path:
    """任何影音檔 → MP3（保存與下載用）。來源本來就是 mp3 時直接複製。"""
    mp3.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".mp3":
        if src.resolve() != mp3.resolve():
            mp3.write_bytes(src.read_bytes())
        return mp3
    r = _run([find_ffmpeg(), "-v", "error", "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame",
              "-b:a", bitrate, str(mp3)])
    if r.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
        raise AudioError("轉 MP3 失敗。" + (r.stderr or "")[-300:])
    return mp3


def load_16k(src: Path, start: float = 0.0, length: float | None = None):
    """直接從原始檔解碼成 16kHz 單聲道 float32（比從 MP3 再解碼少一次壓縮損失）。
    可只解碼一段（start 秒起、length 秒），長檔切段時用，記憶體用量固定。"""
    import numpy as np
    cut = (["-ss", f"{start:.3f}"] if start > 0 else []) + (["-t", f"{length:.3f}"] if length else [])
    r = _run([find_ffmpeg(), "-v", "error", *cut, "-i", str(src), "-vn", "-f", "f32le", "-ac", "1",
              "-ar", str(SR), "-"], binary=True)
    if r.returncode != 0 or not r.stdout:
        raise AudioError("聲音解碼失敗。" + r.stderr.decode("utf-8", "ignore")[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32)
