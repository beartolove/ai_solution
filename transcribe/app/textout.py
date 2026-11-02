"""文字後處理與輸出：OpenCC 轉臺灣正體、長句切分、SRT／TXT／JSON。"""
from __future__ import annotations

import json
import re
from pathlib import Path

Segment = tuple[float, float, str]
_PUNCT = "，。！？；、,.!?;"
_cc = None


def to_tw(text: str) -> str:
    """簡體字 → 臺灣正體字（s2tw：只轉字形，不換詞）。

    不用 s2twp：它會把已經正確的正體詞也換成「臺灣慣用詞」，實測造成錯誤，
    例如 連接詞→連線詞、溝通對象→溝通物件、發佈→釋出（2026-10-31 實測發現）。
    專有名詞的正確寫法改由專有名詞庫（glossary）處理。
    """
    global _cc
    if _cc is None:
        from opencc import OpenCC
        _cc = OpenCC("s2tw")
    return _cc.convert(text)


def split_long(start: float, end: float, text: str, max_chars: int = 26) -> list[Segment]:
    """一句太長時依標點切成數段，依字數比例分配時間，字幕才不會塞滿畫面。"""
    text = text.strip()
    cjk = len(re.findall(r"[㐀-鿿]", text))
    is_cjk = cjk > len(text) * 0.3
    limit = max_chars if is_cjk else max_chars * 3
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
    for p in parts:  # 沒標點的長句再硬切
        while len(p) > limit * 1.4:
            cut = limit if is_cjk else (p.rfind(" ", 0, limit) if p.rfind(" ", 0, limit) > 0 else limit)
            out.append(p[:cut])
            p = p[cut:]
        out.append(p)
    out = [p.strip() for p in out if p.strip()]
    total = sum(len(p) for p in out) or 1
    res, t = [], start
    for p in out:
        d = (end - start) * len(p) / total
        res.append((t, t + d, p))
        t += d
    return res


def postprocess(segs: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for a, b, t in segs:
        t = to_tw(t)
        out.extend(split_long(a, b, t))
    return [s for s in out if s[2]]


def _ts(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def with_ext(base: Path, ext: str) -> Path:
    """base 加上副檔名（不用 with_suffix：檔名中間有句點時，例如「v1.2 會議」，會被誤切）"""
    return base.parent / (base.name + ext)


def tidied(base: Path) -> bool:
    """已整理過（有 <base>.tidy.json）：<base>.txt 是整理稿，字幕更新時不可再蓋回一句一行"""
    return with_ext(base, ".tidy.json").exists()


def write_outputs(segs: list[Segment], base: Path) -> dict[str, Path]:
    """產生 <base>.srt / .txt / .json，回傳各檔路徑（已整理過的不動 .txt）"""
    srt = "\n".join(f"{i}\n{_ts(a)} --> {_ts(max(b, a + 0.3))}\n{t}\n" for i, (a, b, t) in enumerate(segs, 1))
    txt = "\n".join(t for _, _, t in segs) + "\n"
    files = {
        "srt": with_ext(base, ".srt"),
        "txt": with_ext(base, ".txt"),
        "json": with_ext(base, ".json"),
    }
    files["srt"].write_text(srt, encoding="utf-8-sig")  # 加 BOM，Windows 播放器較不會亂碼
    if not tidied(base):
        files["txt"].write_text(txt, encoding="utf-8-sig")
    files["json"].write_text(json.dumps([{"start": round(a, 2), "end": round(b, 2), "text": t} for a, b, t in segs],
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    return files
