"""長檔切段辨識：每次只解碼一段（例如 10 分鐘），記憶體用量固定，且每段完成就存檔（中斷可續跑）。

切點處理：每段多解碼 TAIL 秒，只保留「開始時間在本段範圍內」的句子；
下一段從最後保留那句的結束時間接著開始，所以句子不會被切斷，也不會重複。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import audio, engine_ov

Segment = tuple[float, float, str]
TAIL = 30.0  # 每段多解碼的秒數，讓跨切點的句子能完整辨識


def transcribe_whole(src: Path, pipe, language: str = "auto", initial_prompt: str = "",
                     progress: Callable[[float, str], None] | None = None) -> list[Segment]:
    """不切段：整個檔案一次解碼進記憶體"""
    pcm = audio.load_16k(src)
    return engine_ov.transcribe(pipe, pcm, language, initial_prompt, progress)


def transcribe_chunked(src: Path, pipe, duration: float, chunk_min: float, language: str = "auto",
                       initial_prompt: str = "", limit_sec: float | None = None,
                       progress: Callable[[float, str], None] | None = None,
                       checkpoint: Path | None = None) -> tuple[list[Segment], list[float]]:
    """回傳 (句子, 實際切點秒數)。checkpoint 檔存在時從上次完成處續跑。"""
    total = min(duration, limit_sec) if limit_sec else duration
    L = chunk_min * 60
    segs: list[Segment] = []
    cuts: list[float] = []
    pos = 0.0
    if checkpoint and checkpoint.exists():
        try:
            st = json.loads(checkpoint.read_text(encoding="utf-8"))
            segs = [tuple(s) for s in st["segments"]]
            pos, cuts = float(st["pos"]), list(st.get("cuts", []))
        except Exception:
            segs, pos, cuts = [], 0.0, []

    while pos < total - 0.5:
        last = pos + L >= total - 1
        length = (total - pos) if last else (L + TAIL)
        pcm = audio.load_16k(src, pos, length)
        if len(pcm) < audio.SR * 0.5:
            break

        def prog(f: float, _m: str, _pos=pos, _len=length):
            if progress:
                progress(min((_pos + f * min(_len, L)) / total, 1.0), "")

        part = engine_ov.transcribe(pipe, pcm, language, initial_prompt, prog)
        if last:
            keep = part
            nxt = total
        else:
            keep = [s for s in part if s[0] < L]
            if not keep:  # 這段沒人說話 → 直接跳到下一段
                nxt = pos + L
            else:
                end = keep[-1][1]
                nxt = pos + (end if end > L * 0.5 else L)
        for a, b, t in keep:
            if segs and t == segs[-1][2]:
                continue
            segs.append((pos + a, pos + b, t))
        if not last:
            cuts.append(round(nxt, 2))
        pos = nxt
        if checkpoint:
            checkpoint.write_text(json.dumps({"pos": pos, "cuts": cuts, "segments": segs}, ensure_ascii=False),
                                  encoding="utf-8")
    return segs, cuts


def quality_stats(segs: list[Segment], cuts: list[float] | None = None) -> dict:
    """簡易品質指標：重複句（模型幻覺的常見徵兆）、最大空白、切點附近空白"""
    texts = [t for _, _, t in segs]
    from collections import Counter
    c = Counter(t for t in texts if len(t) >= 4)
    repeats = sum(n - 1 for n in c.values() if n > 1)
    gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
    near_cut = []
    for x in cuts or []:
        g = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1) if abs(segs[i][1] - x) < 20]
        near_cut.append(round(max(g), 1) if g else 0.0)
    return {"segments": len(segs), "chars": sum(len(t) for t in texts), "repeats": repeats,
            "max_gap": round(max(gaps), 1) if gaps else 0.0,
            "max_gap_at_cut": max(near_cut) if near_cut else 0.0}
