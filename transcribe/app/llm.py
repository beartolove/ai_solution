"""本機 LLM（0.12.0）：Google Gemma 4 E4B（Intel 官方 OpenVINO int4 版），完全在本機執行、不上傳。

用途（共用同一個模型）：
- 校對時判讀「改過的地方」是不是要加入詞庫的專有名詞
- 階段 8：把一句一行的字幕整理成有標點、合宜分段的文章（之後）

- 模型：<MODELS_ROOT>\\llm\\gemma-4-<大小>-it-int4-ov，從 Hugging Face「OpenVINO/…」下載（Apache-2.0，不需帳號）；依記憶體建議 E2B／E4B／26B-A4B
- 在「參數設定 → 本機 LLM」按一下下載；下載在背景進行、不影響轉檔
- 執行：openvino_genai.VLMPipeline（這個模型匯出時包含看圖元件，只用文字），有 Intel 顯示卡用顯示卡
- 閒置 10 分鐘自動釋放記憶體
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import textout
from .config import load_settings, models_root

# 依電腦記憶體建議（都是 Google Gemma 4、Intel 官方 OpenVINO int4 版，Apache-2.0）
MODELS = {
    "e2b": {"repo": "OpenVINO/gemma-4-E2B-it-int4-ov", "label": "Gemma 4 E2B（小、最快）", "min_ram": 8,
            "note": "記憶體 16 GB 以下的電腦；判讀詞語可用，整理段落品質普通"},
    "e4b": {"repo": "OpenVINO/gemma-4-E4B-it-int4-ov", "label": "Gemma 4 E4B（平衡）", "min_ram": 16,
            "note": "記憶體 16～63 GB；速度與品質平衡"},
    "26b": {"repo": "OpenVINO/gemma-4-26b-a4b-it-int4-ov", "label": "Gemma 4 26B-A4B（品質最好）", "min_ram": 64,
            "note": "記憶體 64 GB 以上；約 15 GB，整理段落品質明顯較好"},
}


def ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 2**30
    except Exception:
        return 0.0


def recommend(ram: float | None = None) -> str:
    ram = ram_gb() if ram is None else ram
    if ram >= 60:
        return "26b"
    if ram >= 15:
        return "e4b"
    return "e2b"


def current() -> str:
    k = load_settings().get("llm_model") or ""
    if k in MODELS:
        return k
    for k2 in ("e4b", "e2b", "26b"):  # 沒指定時：已下載的優先，否則依記憶體建議
        if _ready(k2):
            return k2
    return recommend()


def _name(key: str) -> str:
    return MODELS[key]["repo"].split("/", 1)[1]


IDLE_SEC = 600

_lock = threading.RLock()
_pipe = None
_device = ""
_last = 0.0
_sdpa = False
_key = ""
_dl = {"state": "idle", "progress": 0.0, "msg": "", "error": "", "total_gb": None}


class LLMError(Exception):
    pass


def model_dir(key: str | None = None) -> Path:
    return models_root() / "llm" / _name(key or current())


def _ready(key: str) -> bool:
    d = model_dir(key)
    return d.is_dir() and any(d.glob("openvino_language_model.xml")) and (d / ".download_ok").exists()


def ready() -> bool:
    return _ready(current())


def status() -> dict:
    ram = ram_gb()
    return {"ready": ready(), "model": current(), "recommended": recommend(ram), "ram_gb": round(ram, 1),
            "model_dir": str(model_dir()), "loaded": _pipe is not None, "device": _device, "download": dict(_dl),
            "judge": bool(load_settings().get("llm_judge", True)),
            "models": [{"key": k, **v, "ready": _ready(k), "path": str(model_dir(k))} for k, v in MODELS.items()]}


# ------------------------------------------------------------------ 下載（背景執行緒）
def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


def start_download(key: str | None = None, log: Callable[[str], None] = print) -> dict:
    if _dl["state"] == "running":
        return dict(_dl)
    key = key if key in MODELS else current()
    REPO = MODELS[key]["repo"]
    _dl.update(state="running", progress=0.0, msg="準備下載…", error="", key=key)

    def run():
        try:
            from huggingface_hub import HfApi, snapshot_download
            d = model_dir(key)
            d.mkdir(parents=True, exist_ok=True)
            try:
                info = HfApi().model_info(REPO, files_metadata=True)
                total = sum((s.size or 0) for s in info.siblings)
            except Exception:
                total = 0
            _dl["total_gb"] = round(total / 1e9, 1) if total else None
            done = threading.Event()

            def watch():
                while not done.wait(2):
                    if total:
                        got = _dir_size(d)
                        _dl["progress"] = min(got / total, 0.99)
                        _dl["msg"] = f"下載中 {got / 1e9:.1f}／{total / 1e9:.1f} GB"
            threading.Thread(target=watch, daemon=True).start()
            log(f"下載本機 LLM：{REPO} → {d}")
            snapshot_download(REPO, local_dir=str(d))  # 中斷後再按一次會續傳
            done.set()
            if not any(d.glob("openvino_language_model.xml")):
                raise LLMError("下載完成，但找不到 openvino_language_model.xml，模型格式可能已變更")
            (d / ".download_ok").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            from .config import save_settings
            if not load_settings().get("llm_model"):
                save_settings({**load_settings(), "llm_model": key})
            _dl.update(state="done", progress=1.0, msg="下載完成")
            log("本機 LLM 下載完成")
        except Exception as e:
            _dl.update(state="error", error=str(e), msg="下載失敗（再按一次會從中斷處續傳）")
            log(f"本機 LLM 下載失敗：{e}")
    threading.Thread(target=run, daemon=True).start()
    return dict(_dl)


# ------------------------------------------------------------------ 模型
def _load(log: Callable[[str], None] = print):
    global _pipe, _device
    if _pipe is None:
        if not ready():
            raise LLMError("還沒有本機 LLM：請到「參數設定 → 本機 LLM」下載")
        from .ovcompat import problem as ov_problem
        bad = ov_problem(model_dir())
        if bad:
            raise LLMError(bad)
        import openvino as ov
        import openvino_genai as ov_genai
        from .config import ov_cache_dir
        pref = (load_settings().get("llm_device") or "auto").upper()
        dev = "GPU" if pref in ("AUTO", "GPU") and "GPU" in ov.Core().available_devices else "CPU"
        props = {"CACHE_DIR": str(ov_cache_dir())}
        if _sdpa:
            props["ATTENTION_BACKEND"] = "SDPA"
        t0 = time.time()
        try:
            _pipe = ov_genai.VLMPipeline(str(model_dir()), dev, **props)
        except Exception as e:
            if dev != "GPU":
                raise
            log(f"  顯示卡載入本機 LLM 失敗，改用 CPU：{e}")
            dev = "CPU"
            _pipe = ov_genai.VLMPipeline(str(model_dir()), dev, **props)
        global _key
        _key = current()
        _device = "Intel 顯示卡" if dev == "GPU" else "CPU"
        log(f"載入本機 LLM（{_device}，{time.time() - t0:.0f} 秒）")
        threading.Thread(target=_idle_unload, daemon=True).start()
    return _pipe


def _idle_unload():
    global _pipe
    while True:
        time.sleep(30)
        with _lock:
            if _pipe is None:
                return
            if time.time() - _last > IDLE_SEC:
                _pipe = None
                return


def _render(pipe, prompt: str) -> str:
    """用模型自己的對話範本組提示；取不到時用 Gemma 的格式"""
    try:
        return pipe.get_tokenizer().apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True)
    except Exception:
        return f"<start_of_turn>user\n{prompt}<end_of_turn>\n<start_of_turn>model\n"


def generate(prompt: str, max_new: int = 1024, log: Callable[[str], None] = print) -> str:
    import openvino_genai as ov_genai
    global _pipe, _sdpa, _last
    with _lock:
        if _pipe is not None and _key != current():  # 換了模型
            _pipe = None
        pipe = _load(log)
        cfg = ov_genai.GenerationConfig()
        cfg.max_new_tokens = max_new
        cfg.do_sample = False
        try:
            cfg.apply_chat_template = False
        except Exception:
            pass
        text = _render(pipe, prompt)
        try:
            out = pipe.generate(text, generation_config=cfg)
        except Exception as e:
            if "Chat mode" not in str(e) or _sdpa:
                raise
            log("  顯示卡的連續批次模式不支援，改用 SDPA 重新載入本機 LLM")
            _sdpa, _pipe = True, None
            pipe = _load(log)
            out = pipe.generate(text, generation_config=cfg)
        _last = time.time()
        s = out.texts[0] if hasattr(out, "texts") else str(out)
    s = re.sub(r"<[^>]{1,24}>", "", s)  # 結尾標記
    return s.strip()


# ------------------------------------------------------------------ 判讀詞庫候選
JUDGE_V = 3  # 判讀提示改版就加 1（已判讀的結果會重新判讀）
_JUDGE = """你是逐字稿校對助理。以下是語音辨識稿被人工校對修改的地方（原稿 → 校對後）。請逐項回答三個問題：

1. add：「校對後的寫法」值不值得加入專有名詞庫？
   值得：人名、地名、機關／單位／公司名稱、職稱、專業術語、產品或系統名稱、英文縮寫、特定行業用語，且原稿是聽錯的寫法（同音字、近音字、英文被聽成中文等）。
   不值得：一般用字修正（的／了、語助詞）、標點、口誤修正；校對後的寫法本身是一般常用詞（例：精進、協助、討論）也不值得。
   上下文中【原稿→校對後】標出修改的位置，請看前後文判斷完整的詞（例：中華【電力→電信】的完整詞是「中華電信」）。
2. common：「原稿的寫法」本身是不是日常會正常使用的詞語？（例：「船隻」「飲水」「高雄」是正常詞語 → true；「飲水人」「門少」「idea 工業」不是 → false）
3. term：建議加入詞庫的完整正確寫法（只改了詞的一部分時，填完整的詞）。

每一項輸出一行 JSON，不要其他文字：{{"k":編號,"add":true或false,"common":true或false,"term":"完整正確寫法","why":"10 字內理由"}}

{items}"""


def judge(cands: list[dict], log: Callable[[str], None] = print) -> dict[int, dict]:
    """cands：[{wrong, right, context}]；回傳 {索引: {add, term, flag, why}}（判讀不到的不列）。
    flag（只提醒）＝原稿寫法本身是常用詞（LLM 判斷），或只是補字（原稿寫法包含在正確寫法裡，規則判斷）"""
    out: dict[int, dict] = {}
    for s in range(0, len(cands), 15):
        part = cands[s:s + 15]
        items = "\n".join(f"{k + 1}. 原稿「{c['wrong']}」→ 校對後「{c['right']}」；上下文：{c.get('context', '')[:50]}"
                          for k, c in enumerate(part))
        res = generate(_JUDGE.format(items=items), 70 * len(part) + 64, log)
        for m in re.finditer(r"\{[^{}]*\}", res):
            try:
                o = json.loads(m.group(0))
                k = int(o.get("k", 0)) - 1
            except Exception:
                continue
            if 0 <= k < len(part):
                c = part[k]
                sub = bool(c["wrong"]) and c["wrong"] in c["right"]
                out[s + k] = {"add": bool(o.get("add")), "term": textout.to_tw(str(o.get("term") or c["right"]).strip()),
                              "flag": bool(o.get("common")) or sub, "why": str(o.get("why") or "")[:20]}
    return out


# ------------------------------------------------------------------ 測試（內建範例，不用任何使用者資料）
SAMPLE = [
    {"wrong": "飲水人", "right": "引水人", "context": "今天由【飲水人→引水人】登輪引領船舶進港"},
    {"wrong": "電力", "right": "電信", "context": "我們跟中華【電力→電信】合作"},
    {"wrong": "idea 工業", "right": "AI 產業", "context": "【idea 工業→AI 產業】的發展很快"},
    {"wrong": "經濟", "right": "精進", "context": "大家一起【經濟→精進】工作方法"},
    {"wrong": "船隻", "right": "船席", "context": "請確認【船隻→船席】調度"},
]


def test(log: Callable[[str], None] = print) -> dict:
    t0 = time.time()
    with _lock:
        _load(log)
    t1 = time.time()
    res = judge(SAMPLE, log)
    return {"device": _device, "model": MODELS[current()]["label"], "load_s": round(t1 - t0, 1), "judge_s": round(time.time() - t1, 1),
            "items": [{**c, "res": res.get(k)} for k, c in enumerate(SAMPLE)]}
