"""單一任務流程：影音檔 → 16kHz → 辨識 → 正體／切句 → SRT／TXT／JSON →（需要時）另存 MP3。

每個任務一個資料夾：<MEDIA_ROOT>\\uploads\\<日期時間>_<檔名>\\，內含 mp3、srt、txt、json、job.json（紀錄耗時與設定）。
長檔自動切段辨識：依本機長檔測試的建議（例如 1 小時內不切、超過 1 小時每 30 分鐘切）；未測試時超過 15 分鐘每 10 分鐘切。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import audio, chunked, engine_ov, textout, version
from .config import MODELS, env_report, ov_model_dir, recommended, uploads_dir
from .naming import safe_name

DEFAULT_CHUNK_MIN = 10  # 尚未做長檔測試時的預設切段長度（分鐘）


def _safe(name: str, max_len: int = 80) -> str:
    return safe_name(name, max_len)


# 已是壓縮音訊、瀏覽器也能直接播放的格式：不另存 MP3
KEEP_AS_IS = {".mp3", ".m4a", ".aac", ".ogg", ".opus"}


def run(src: Path, model: str = "breeze", device: str = "auto", language: str = "auto",
        initial_prompt: str = "", out_dir: Path | None = None, chunk_min: float | None = None,
        log: Callable[[str], None] = print,
        progress: Callable[[float, str], None] | None = None, save_mp3: bool | None = None,
        glossary_name: str = "") -> dict:
    """progress(比例 0～1, 階段說明)：給網頁介面顯示進度條
    save_mp3：None＝依格式決定（影片、wav 等另存 MP3）；False＝不另存（網址下載的 mp4 本身就能播放）"""
    src = Path(src)
    gl_entries = []
    if glossary_name:
        from . import glossary
        gl_entries = glossary.load(glossary_name)
        if not initial_prompt:
            initial_prompt = glossary.prompt(gl_entries)

    def step(f: float, msg: str):
        if progress:
            progress(f, msg)
    t_all = time.perf_counter()

    # 1. 檢查檔案
    info = audio.probe(src)
    if not info["has_audio"]:
        raise audio.AudioError("這個檔案沒有聲音軌，無法轉逐字稿")
    log(f"檔案：{src.name}（長度 {textout.fmt_dur(info['duration'])}）")

    stem = _safe(src.stem)
    job = out_dir or (uploads_dir() / f"{datetime.now():%Y%m%d_%H%M%S}_{stem}")
    job.mkdir(parents=True, exist_ok=True)
    base = job / stem

    # 3＋4. 載入模型＋辨識（長檔自動切段：記憶體固定、每段存檔可續跑）
    over_min = None
    if chunk_min is None:
        rec = recommended()
        chunk_min = float(rec.get("chunk_min") or DEFAULT_CHUNK_MIN)
        over_min = rec.get("chunk_over_min")
    # 超過門檻才切段：本機長檔測試有建議就用（例如超過 60 分鐘才切），否則為切段長度的 1.5 倍
    threshold = (over_min or chunk_min * 1.5) * 60
    use_chunk = chunk_min > 0 and info["duration"] > threshold
    mdir = ov_model_dir(model)
    step(0.06, "載入辨識模型")
    pipe, dev, t_load = engine_ov.load(mdir, device, log)
    mode = f"切段（每 {chunk_min:g} 分鐘）" if use_chunk else "不切段"
    log(f"開始辨識（{MODELS.get(model, (model, model))[1]}，{dev}，{mode}）")
    t0 = time.perf_counter()
    last = [-1]

    def prog(f: float, _msg: str):
        step(0.1 + f * 0.88, f"辨識中（{textout.fmt_dur(f * info['duration'])}／{textout.fmt_dur(info['duration'])}）")
        pct = int(f * 100)
        if pct // 10 != last[0]:
            last[0] = pct // 10
            log(f"  辨識中 {pct}%（{textout.fmt_dur(f * info['duration'])}／{textout.fmt_dur(info['duration'])}）")

    cuts: list[float] = []
    if use_chunk:
        ckpt = job / "checkpoint.json"
        raw, cuts = chunked.transcribe_chunked(src, pipe, info["duration"], chunk_min, language, initial_prompt,
                                               progress=prog, checkpoint=ckpt)
    else:
        raw = chunked.transcribe_whole(src, pipe, language, initial_prompt, prog)
    t_asr = time.perf_counter() - t0
    if not raw:
        raise engine_ov.EngineError("沒有辨識出任何語音（可能只有音樂或沒有人說話）")

    # 5. 後處理＋輸出
    step(0.99, "整理字幕")
    segs = textout.postprocess(raw)
    gl_rep = None
    if glossary_name:  # 專有名詞庫：常見誤寫 → 正確寫法（常用詞類的只提醒）
        from . import glossary
        sd, gl_rep = glossary.apply([{"start": a, "end": b, "text": t} for a, b, t in segs], gl_entries)
        segs = [(x["start"], x["end"], x["text"]) for x in sd]
        glossary.save_report(base, glossary_name, gl_rep, initial_prompt)
        log(f"專有名詞庫「{glossary_name}」：替換 {sum(r['count'] for r in gl_rep['replaced'])} 處，"
            f"提醒 {len(gl_rep['flagged'])} 處")
    files = textout.write_outputs(segs, base)
    if use_chunk and (job / "checkpoint.json").exists():
        (job / "checkpoint.json").unlink()  # 完成後不再需要續跑紀錄

    # 6. 另存 MP3（保存用，放在辨識之後，不拖慢出字幕）
    #    辨識一律直接讀原始檔；原始檔已是壓縮音訊（mp3、m4a…）就不另存，避免多一份、二次壓縮
    mp3, t_mp3 = None, 0.0
    if save_mp3 is False:
        log("網址下載的影片可直接播放，不另存 MP3（辨識直接讀影片的聲音軌，沒有暫存聲音檔）")
    elif src.suffix.lower() in KEEP_AS_IS:
        log(f"原始檔已是 {src.suffix.lower()[1:]}，直接保留，不另存 MP3")
    else:
        step(0.995, "另存 MP3")
        t0 = time.perf_counter()
        try:
            mp3 = audio.to_mp3(src, textout.with_ext(base, ".mp3"))
            t_mp3 = time.perf_counter() - t0
            log(f"另存 MP3 完成（{t_mp3:.1f} 秒）")
        except audio.AudioError as e:  # 字幕已完成，MP3 失敗不影響結果
            log(f"另存 MP3 失敗（字幕不受影響）：{e}")
    t_total = time.perf_counter() - t_all

    rep = env_report()
    result = {
        "app_version": version(),
        "computer": rep.get("Computer", ""),
        "source": str(src),
        "duration_sec": round(info["duration"], 1),
        "model": model,
        "model_dir": str(mdir),
        "device": dev,
        "language": language,
        "chunk_min": chunk_min if use_chunk else 0,
        "cuts": cuts,
        "initial_prompt": initial_prompt,
        "glossary": glossary_name,
        "seconds": {"mp3": round(t_mp3, 1), "load_model": round(t_load, 1),
                    "asr": round(t_asr, 1), "total": round(t_total, 1)},
        # RTF：辨識耗時 ÷ 音檔長度；0.1 代表 10 分鐘音檔約 1 分鐘辨識完
        "rtf": round(t_asr / info["duration"], 3) if info["duration"] else None,
        "segments": len(segs),
        "chars": sum(len(t) for _, _, t in segs),
        "job_dir": str(job),
        "files": {k: str(v) for k, v in {"mp3": mp3, **files}.items() if v},
        "finished_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
    }
    (job / "job.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    step(1.0, "完成")
    log(f"完成：辨識 {t_asr:.0f} 秒（RTF {result['rtf']}），共 {len(segs)} 句 → {job}")
    return result
