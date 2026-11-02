"""本機 AI 模型的位置（1.25.0，參考 transcribe 專案 config.models_root 的做法）。

所有模型集中放在「模型位置」（預設 D:\\models），每個模型在底下有固定的子資料夾，與 transcribe 專案相同，
兩個專案可以共用同一份模型，不必重複下載。模型位置的優先順序：
    網頁「設定 → 本機 AI 模型」（settings.json 的 models_root）→ 環境變數 MODELS_ROOT → D:\\models
本專案不刪除模型。Gemma 4（Hugging Face 公開、不需帳號）可在設定頁一鍵下載（1.26.0）；
Breeze、turbo（需轉換）與 TranslateGemma（需同意條款）由管理者準備，或從 transcribe 專案的母機複製 D:\\models。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = r"D:\models"

# key: (子資料夾, 名稱, 判斷模型齊全要有的檔案, 約略大小 GB, 用途)
MODELS = {
    "breeze": ("asr/ov/breeze-asr-25", "Breeze-ASR-25", ("openvino_encoder_model.xml",), 1.5,
               "語音辨識：中文、中英夾雜（主力）"),
    "turbo": ("asr/ov/large-v3-turbo", "Whisper large-v3-turbo", ("openvino_encoder_model.xml",), 0.8,
              "語音辨識：英文、日文等外語影片"),
    "translategemma": ("llm/translategemma-4b-it", "TranslateGemma 4B", ("openvino_language_model.xml", "prompt_templates.json"), 4.7,
                       "本機翻譯：外語字幕翻成正體中文"),
    "e2b": ("llm/gemma-4-E2B-it-int4-ov", "Gemma 4 E2B（小、最快）", ("openvino_language_model.xml", ".download_ok"), None,
            "本機重點摘要：記憶體 16 GB 以下"),
    "e4b": ("llm/gemma-4-E4B-it-int4-ov", "Gemma 4 E4B（平衡）", ("openvino_language_model.xml", ".download_ok"), 6.1,
            "本機重點摘要：記憶體 16～63 GB"),
    "26b": ("llm/gemma-4-26b-a4b-it-int4-ov", "Gemma 4 26B-A4B（品質最好）", ("openvino_language_model.xml", ".download_ok"), 15,
            "本機重點摘要：記憶體 64 GB 以上"),
}


def _settings(s: dict | None) -> dict:
    if s is not None:
        return s
    from . import downloader as dl
    return dl.load_settings()


def root_source(s: dict | None = None) -> tuple[Path, str]:
    """回傳 (模型位置, 來源 settings | env | default)"""
    v = (_settings(s).get("models_root") or "").strip().strip('"')
    if v and Path(v).is_absolute():      # 不是完整路徑（例如瀏覽器自動填入的「download」）就不採用
        return Path(v), "settings"
    v = (os.environ.get("MODELS_ROOT") or "").strip().strip('"')
    if v:
        return Path(v), "env"
    return Path(DEFAULT_ROOT), "default"


def root(s: dict | None = None) -> Path:
    return root_source(s)[0]


def path(key: str, s: dict | None = None) -> Path:
    return root(s) / MODELS[key][0]


def label(key: str) -> str:
    return MODELS[key][1]


def ready(key: str, s: dict | None = None) -> bool:
    d = path(key, s)
    return d.is_dir() and all((d / f).exists() for f in MODELS[key][2])


def status(s: dict | None = None) -> dict:
    r, src = root_source(s)
    return {"root": str(r), "source": src, "root_exists": r.is_dir(),
            "models": [{"key": k, "label": v[1], "path": str(path(k, s)), "ready": ready(k, s), "gb": v[3], "use": v[4]}
                       for k, v in MODELS.items()]}


def ov_cache_dir() -> Path:
    """OpenVINO 編譯快取：第二次載入模型會快很多（執行快取，放專案內、不放模型位置）"""
    from . import downloader as dl
    d = dl.ROOT / ".cache" / "ov"
    d.mkdir(parents=True, exist_ok=True)
    return d
