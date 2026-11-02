"""路徑與環境設定：模型位置一律從 MODELS_ROOT 讀取，不寫死路徑。"""
from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path

from . import ROOT

MODELS = {
    # 名稱: (OpenVINO 子資料夾, 說明)
    "breeze": ("breeze-asr-25", "Breeze-ASR-25（臺灣華語、中英夾雜）"),
    "turbo": ("large-v3-turbo", "Whisper large-v3-turbo（較快）"),
}


DEFAULT_MODELS_ROOT = r"D:\models"


def models_root_source() -> tuple[Path, str]:
    """回傳 (模型根目錄, 來源 'settings'|'env'|'default')。
    優先順序：網頁「參數設定」（data\\settings.json 的 models_root）→ 環境變數 MODELS_ROOT → D:\\models"""
    v = (load_settings().get("models_root") or "").strip()
    if v:
        return Path(v), "settings"
    v = (os.environ.get("MODELS_ROOT") or "").strip()
    if v:
        return Path(v), "env"
    return Path(DEFAULT_MODELS_ROOT), "default"


def models_root() -> Path:
    return models_root_source()[0]


def ov_model_dir(name: str) -> Path:
    sub = MODELS.get(name, (name, ""))[0]
    return models_root() / "asr" / "ov" / sub


# ------------------------------------------------------------------ 媒體檔位置（0.6.7 起）
# 上傳檔、轉檔結果、效能實測暫存都放在 MEDIA_ROOT（預設 D:\mediafiles），不放在專案內，
# 專案複製給使用者時才不會太大。優先順序：網頁「設定」→ 環境變數 MEDIA_ROOT → 預設值。
DEFAULT_MEDIA_ROOT = r"D:\mediafiles"
SETTINGS_FILE = ROOT / "data" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def media_root_source() -> tuple[Path, str]:
    """回傳 (媒體檔根目錄, 來源 'settings'|'env'|'default'|'fallback')"""
    v = (load_settings().get("media_root") or "").strip()
    if v:
        return Path(v), "settings"
    v = (os.environ.get("MEDIA_ROOT") or "").strip()
    if v:
        return Path(v), "env"
    p = Path(DEFAULT_MEDIA_ROOT)
    if os.name == "nt" and not Path(p.anchor).exists():  # 沒有 D 槽：暫存在專案內，setup 與設定頁會提示改位置
        return ROOT / "mediafiles", "fallback"
    return p, "default"


def media_root() -> Path:
    return media_root_source()[0]


def _media_sub(name: str) -> Path:
    d = media_root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def uploads_dir() -> Path:
    """每個任務一個資料夾：上傳的原始檔、字幕、純文字、MP3、處理紀錄"""
    return _media_sub("uploads")


def benchmark_dir() -> Path:
    """效能實測的暫存檔（結果報告仍在專案 env-reports\）"""
    return _media_sub("benchmark")


def downloads_dir() -> Path:
    """網址下載的任務：每個任務一個資料夾（下載的錄音、字幕、純文字、處理紀錄）"""
    return _media_sub("downloads")


jobs_dir = uploads_dir  # 舊名稱，保留相容


def task_bases() -> list[Path]:
    """存放任務資料夾的位置：上傳（uploads）與網址下載（downloads）"""
    return [uploads_dir(), downloads_dir()]


def count_tasks(base: Path) -> int:
    """數 base 底下的任務資料夾（不含 benchmark_* 與 _ 開頭）；資料夾不存在回傳 0"""
    try:
        return sum(1 for d in base.iterdir() if d.is_dir() and not d.name.startswith(("benchmark_", "_")))
    except Exception:
        return 0


def legacy_jobs_dir() -> Path:
    """0.6.6 以前的任務位置（專案內 jobs\）；只用來提示，不自動搬移或刪除"""
    return ROOT / "jobs"


def ov_cache_dir() -> Path:
    """OpenVINO 編譯快取：第二次載入模型會快很多（屬於執行快取，不放 D:\\models）"""
    d = ROOT / ".cache" / "ov"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_ffmpeg() -> str:
    """系統 PATH 優先，其次專案 tools\\ffmpeg"""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    p = shutil.which("ffmpeg")
    if p:
        return p
    for f in (ROOT / "tools" / "ffmpeg").rglob(exe):
        return str(f)
    raise FileNotFoundError("找不到 ffmpeg，請先執行 setup.cmd")


def find_ffprobe() -> str:
    ff = Path(find_ffmpeg())
    probe = ff.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if probe.exists():
        return str(probe)
    p = shutil.which("ffprobe")
    if p:
        return p
    raise FileNotFoundError("找不到 ffprobe（應與 ffmpeg 放在一起）")


def computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def benchmark_file() -> Path:
    """本機效能實測結果（與 env-check 的報告分開存，避免 setup 重跑時被覆蓋）"""
    d = ROOT / "env-reports"
    d.mkdir(exist_ok=True)
    return d / f"{computer_name()}.benchmark.json"


def recommended() -> dict:
    """回傳本機建議配置 {'model', 'device', 'chunk_min', 'source'}；尚未實測時用預設值"""
    out = {"model": "breeze", "device": "auto", "chunk_min": None, "chunk_over_min": None, "source": "default"}
    try:
        data = json.loads(benchmark_file().read_text(encoding="utf-8"))
        rec = data.get("recommended") or {}
        if rec.get("model") in MODELS and rec.get("device"):
            out.update(model=rec["model"], device=rec["device"], source="benchmark")
        lt = data.get("long_test") or {}
        if lt.get("chunk_min") is not None:
            out["chunk_min"] = lt["chunk_min"]
            out["chunk_over_min"] = lt.get("chunk_over_min")
    except Exception:
        pass
    return out


def env_report() -> dict:
    """讀取 setup.cmd 產生的環境初測報告；沒有就回傳空字典"""
    f = ROOT / "env-reports" / f"{computer_name()}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
