"""雙語對照翻譯（0.9.0）：Google TranslateGemma 4B（OpenVINO），把外文逐字稿翻成正體中文。

- 模型：<MODELS_ROOT>\\llm\\translategemma-4b-it（管理者用 admin-download-translator.cmd 下載轉換一次）
- 執行：openvino_genai.VLMPipeline，有 Intel 顯示卡用顯示卡，否則 CPU；不需要額外安裝元件
- 一次送約 12 句（保留前後文、較通順），翻出來的行數對不上時改逐句翻，確保每句都對得上
- 譯文一律再以 OpenCC s2tw 轉正體（模型的中文代碼不分正簡）
- 結果存在 <檔名>.zh-TW.json；另產生 <檔名>.zh-TW.srt（中文）與 <檔名>.bilingual.srt（雙語）
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import textout
from .config import load_settings, models_root

MODEL_NAME = "translategemma-4b-it"
BATCH = 12
_pipe = None
_device = ""

LANGS = {"en": "英文", "ja": "日文", "ko": "韓文", "fr": "法文", "de": "德文", "es": "西班牙文", "it": "義大利文",
         "pt": "葡萄牙文", "ru": "俄文", "vi": "越南文", "th": "泰文", "id": "印尼文", "ms": "馬來文", "nl": "荷蘭文",
         "pl": "波蘭文", "tr": "土耳其文", "ar": "阿拉伯文", "hi": "印地文", "zh": "中文"}


class TranslateError(Exception):
    pass


class TranslateCancelled(Exception):
    pass


def model_dir() -> Path:
    return models_root() / "llm" / MODEL_NAME


def _templates() -> dict:
    try:
        return json.loads((model_dir() / "prompt_templates.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def ready() -> bool:
    return (model_dir() / "openvino_language_model.xml").exists() and bool(_templates().get("templates"))


def status() -> dict:
    return {"ready": ready(), "model_dir": str(model_dir())}


# ------------------------------------------------------------------ 判斷原文語言
def detect(texts: list[str]) -> str:
    """依文字判斷：假名→日文、諺文→韓文、漢字為主→中文（不需翻譯）、其他→英文（拉丁字母最常見）"""
    s = "".join(texts)[:20000]
    if not s:
        return "zh"
    kana = len(re.findall(r"[\u3040-\u30ff]", s))
    hangul = len(re.findall(r"[\uac00-\ud7af]", s))
    han = len(re.findall(r"[\u4e00-\u9fff]", s))
    latin = len(re.findall(r"[A-Za-z]", s))
    cyr = len(re.findall(r"[\u0400-\u04ff]", s))
    thai = len(re.findall(r"[\u0e00-\u0e7f]", s))
    if kana > len(s) * 0.05:
        return "ja"
    if hangul > len(s) * 0.2:
        return "ko"
    if han > (latin / 3 + cyr + thai) and han > len(s) * 0.2:
        return "zh"
    if cyr > latin:
        return "ru"
    if thai > latin:
        return "th"
    return "en"


# ------------------------------------------------------------------ 模型
_sdpa = False  # 顯示卡的連續批次模式不支援時，改用 SDPA 注意力機制


def _load(log: Callable[[str], None] = print):
    global _pipe, _device
    if _pipe is None:
        if not ready():
            raise TranslateError(f"找不到翻譯模型：{model_dir()}。請管理者執行 admin-download-translator.cmd，"
                                 "或向管理者索取 D:\\models\\llm 資料夾。")
        from .ovcompat import problem as ov_problem
        bad = ov_problem(model_dir())
        if bad:
            raise TranslateError(bad)
        import openvino as ov
        import openvino_genai as ov_genai
        pref = (load_settings().get("translate_device") or "auto").upper()
        dev = "GPU" if pref in ("AUTO", "GPU") and "GPU" in ov.Core().available_devices else "CPU"
        from .config import ov_cache_dir
        t0 = time.time()
        props = {"CACHE_DIR": str(ov_cache_dir())}
        if _sdpa:
            props["ATTENTION_BACKEND"] = "SDPA"
        try:
            _pipe = ov_genai.VLMPipeline(str(model_dir()), dev, **props)
        except Exception as e:
            if dev == "GPU":
                log(f"  顯示卡載入翻譯模型失敗，改用 CPU：{e}")
                dev = "CPU"
                _pipe = ov_genai.VLMPipeline(str(model_dir()), dev, **props)
            else:
                raise
        try:  # 提示文字已依模型範本組好：範本改成原樣輸出（萬一仍被套用也不會重複包裝）
            _pipe.set_chat_template("{% for message in messages %}{{ message['content'] }}{% endfor %}")
        except Exception:
            pass
        _device = "Intel 顯示卡" if dev == "GPU" else "CPU"
        log(f"載入翻譯模型（{_device}，{time.time() - t0:.0f} 秒）")
    return _pipe


def _gen(pipe, prompt: str, max_new: int, log: Callable[[str], None] = print) -> str:
    import openvino_genai as ov_genai
    global _pipe, _sdpa
    cfg = ov_genai.GenerationConfig()
    cfg.max_new_tokens = max_new
    cfg.do_sample = False
    cfg.repetition_penalty = 1.05
    try:
        cfg.apply_chat_template = False  # 提示已組好，不要再套對話範本（顯示卡的連續批次模式不支援對話模式）
    except Exception:
        pass
    try:
        out = pipe.generate(prompt, generation_config=cfg)
    except Exception as e:
        if "Chat mode" not in str(e) or _sdpa:
            raise
        log("  顯示卡的連續批次模式不支援，改用 SDPA 重新載入翻譯模型")
        _sdpa, _pipe = True, None
        pipe = _load(log)
        out = pipe.generate(prompt, generation_config=cfg)
    text = out.texts[0] if hasattr(out, "texts") else str(out)
    return text.replace("<end_of_turn>", "").strip()


def _prompt(src: str, text: str) -> str:
    t = _templates()
    tpl = t["templates"].get(src) or t["templates"].get("en")
    return tpl.replace(t.get("mark", "@@TEXT@@"), text)


def _clean(s: str) -> str:
    s = re.sub(r"^\s*(\d+[\.\)、]\s*)", "", s.strip())  # 模型偶爾自己加上的編號
    return textout.to_tw(s).strip()


# ------------------------------------------------------------------ 翻譯
def translate(texts: list[str], src: str, progress: Callable[[float, str], None] | None = None,
              cancelled: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print) -> list[str]:
    """逐字稿每句翻成正體中文，回傳與 texts 等長的譯文清單"""
    pipe = _load(log)
    out: list[str] = []
    n = len(texts)
    t0 = time.time()
    fallback = 0
    for i in range(0, n, BATCH):
        if cancelled():
            raise TranslateCancelled()
        chunk = [x.strip() for x in texts[i:i + BATCH]]
        progress and progress(i / n, f"翻譯成中文 {i}／{n} 句")
        res = _gen(pipe, _prompt(src, "\n".join(chunk)), 48 * len(chunk) + 64, log)
        pipe = _pipe or pipe  # 可能已改用 SDPA 重新載入
        lines = [ln for ln in res.splitlines() if ln.strip()]
        if len(lines) != len(chunk):  # 行數對不上：逐句翻，確保每句對得上
            fallback += 1
            lines = []
            for x in chunk:
                if cancelled():
                    raise TranslateCancelled()
                lines.append(_gen(pipe, _prompt(src, x), 96, log).replace("\n", " ") if x else "")
        out.extend(_clean(x) for x in lines)
        el = time.time() - t0
        if i + BATCH < n:
            left = el / (i + BATCH) * (n - i - BATCH)
            progress and progress((i + BATCH) / n, f"翻譯成中文 {i + BATCH}／{n} 句（約剩 {left / 60:.0f} 分鐘）"
                                  if left >= 60 else f"翻譯成中文 {i + BATCH}／{n} 句（約剩 {left:.0f} 秒）")
    log(f"翻譯完成：{n} 句（{time.time() - t0:.0f} 秒，{_device}；{fallback} 組改逐句翻）")
    return out


# ------------------------------------------------------------------ 檔案
def trans_file(base: Path) -> Path:
    return textout.with_ext(base, ".zh-TW.json")


def load(base: Path) -> dict | None:
    try:
        return json.loads(trans_file(base).read_text(encoding="utf-8"))
    except Exception:
        return None


def save(base: Path, segs: list[dict], lines: list[str], src: str, seconds: float) -> dict:
    data = {"version": 1, "model": MODEL_NAME, "src": src, "target": "zh-TW", "lines": lines,
            "seconds": round(seconds, 1), "device": _device, "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    trans_file(base).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    ts = lambda s: f"{textout._ts(s['start'])} --> {textout._ts(max(s['end'], s['start'] + 0.3))}"
    zh = "\n".join(f"{i}\n{ts(s)}\n{lines[i - 1] if i - 1 < len(lines) else ''}\n" for i, s in enumerate(segs, 1))
    bi = "\n".join(f"{i}\n{ts(s)}\n{s['text']}\n{lines[i - 1] if i - 1 < len(lines) else ''}\n"
                   for i, s in enumerate(segs, 1))
    textout.with_ext(base, ".zh-TW.srt").write_text(zh, encoding="utf-8-sig")
    textout.with_ext(base, ".bilingual.srt").write_text(bi, encoding="utf-8-sig")
    return data
