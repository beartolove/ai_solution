"""整理逐字稿（階段 8，0.13.0）：用本機 LLM 把「一句一行的字幕」整理成有標點、分段、小標題的純文字。

- 兩步驟（0.13.1）：① 每約 450 字請模型加標點，檢查沒改字、真的有加標點，不合格重試一次、仍不行該小段改用保守整理；
  ② 把加好標點的句子編號，只請模型回答「哪幾句開新段落、哪裡加小標題」，文字由程式組回去，模型改不到內容
  （0.13.0 一次請模型完成全部，E4B 遇到長文會原樣照抄、沒加標點也沒分段）
- 有分辨說話者時：換說話者就換段，段首標示「姓名：」
- 顯示進度、可取消
- 整理稿直接取代 <檔名>.txt（SRT、時間軸不動；之後校對字幕不會再把 .txt 蓋回一句一行）；整理資訊存 <檔名>.tidy.json
"""
from __future__ import annotations

import difflib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import diarize, llm, textout

PUNCT_CHARS = 450   # 加標點每小批字數（太長小模型會只照抄）
STRUCT_SENTS = 60   # 分段時每次給模型的句數
_PUNCT = re.compile(r"[\s，。、？！：；「」『』（）《》〈〉…—\-,.?!:;\"'()\[\]【】#■＃]+")


class TidyCancelled(Exception):
    pass


def out_file(base: Path) -> Path:
    """整理稿直接取代原本的純文字檔（0.13.6 使用者要求）；一句一行的內容仍可從 SRT／時間軸產生"""
    return textout.with_ext(base, ".txt")


def _legacy(base: Path) -> Path:
    return textout.with_ext(base, ".整理.txt")  # 0.13.0～0.13.5 另存的整理稿


def info_file(base: Path) -> Path:
    return textout.with_ext(base, ".tidy.json")


def load(base: Path) -> dict | None:
    lg = _legacy(base)
    if lg.exists():  # 舊版另存的整理稿：搬成正式的純文字檔
        out_file(base).write_bytes(lg.read_bytes())
        lg.unlink()
    f = out_file(base)
    if not info_file(base).exists() or not f.exists():
        return None
    try:
        meta = json.loads(info_file(base).read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    return {"text": f.read_text(encoding="utf-8-sig"), **meta}


def _lines(segs: list[dict], sp: dict | None) -> list[dict]:
    out = []
    for i, s in enumerate(segs):
        who = ""
        if sp and i < len(sp.get("seg", [])) and sp["seg"][i] is not None and sp["seg"][i] >= 0:
            who = sp["names"][sp["seg"][i]]
        out.append({"t": s["text"].strip(), "who": who, "start": s["start"], "end": s["end"]})
    return [x for x in out if x["t"]]


def _turns(lines: list[dict]) -> list[dict]:
    """依說話者合併成「發言」（沒分辨說話者時整份是同一段發言）"""
    out = []
    for x in lines:
        if out and out[-1]["who"] == x["who"]:
            out[-1]["lines"].append(x)
        else:
            out.append({"who": x["who"], "lines": [x]})
    return out


def _pieces(lines: list[dict], size: int = PUNCT_CHARS) -> list[list[dict]]:
    """加標點用的小批：約 size 字，盡量在停頓處切開（小模型一次處理太長會只照抄、不加標點）"""
    out, cur, n = [], [], 0
    for x in lines:
        if cur and (n >= size * 1.4 or (n >= size and x["start"] - cur[-1]["end"] >= 0.8)):
            out.append(cur)
            cur, n = [], 0
        cur.append(x)
        n += len(x["t"])
    if cur:
        out.append(cur)
    return out


_P_PUNCT = """請為下面這段語音辨識的逐字稿加上全形標點符號（，。？！、：；「」），讓它通順好讀。
規則：
- 只加標點，不可改字、刪字、增加內容或調換順序；口語、贅字都照原樣保留。
- 原文是一行一句字幕，請把它們接成連續的文字，在適當的地方斷句（。？！）。
- 中文用正體字；英文單字照原樣。
- 只輸出加好標點的文字，不要任何說明。

逐字稿：
{text}"""

_P_STRUCT = """下面是一段已經加好標點的逐字稿，每一句前面有編號。請幫它分段並加小標題：
- 同一個小話題的句子放在同一段，每段大約 3～8 句。
- 話題明顯轉換的地方給一個小標題（10 個字以內、概括該段主題），不要每段都有；整段話題沒變就不給。{head}
- 只輸出一行 JSON，不要其他文字：{{"p":[每一段第一句的編號],"h":{{"句子編號":"小標題"}}}}

{text}"""


def _marks(s: str) -> int:
    return len(re.findall(r"[，。？！、：；]", s))


def _punct(piece: list[dict], log) -> tuple[str, bool]:
    """一小批加標點；檢查沒有改字（相似度）且真的有加標點（每 30 字至少 1 個），否則重試，仍不行回傳保守版"""
    src = "\n".join(x["t"] for x in piece)
    raw = "".join(x["t"] for x in piece)
    for k in range(2):
        out = textout.to_tw(llm.generate(_P_PUNCT.format(text=src), int(len(raw) * 1.5) + 120, log)).strip()
        out = re.sub(r"\s*\n\s*", "", out)
        sim, mk = _same(raw, out), _marks(out)
        if sim >= 0.95 and mk >= max(1, len(raw) // 30):
            return out, True
        log(f"  加標點結果不合格（相似度 {sim:.2f}、標點 {mk} 個／{len(raw)} 字），{'重試' if k == 0 else '這一小段改用保守整理'}")
    return _plain(piece).replace("\n\n", ""), False


def _sentences(text: str) -> list[str]:
    """依句號、問號、驚嘆號斷句；超過 120 字的長句再依逗號切成約 60 字（分段時才有位置可切）"""
    out = []
    for p in re.findall(r"[^。？！]+[。？！」]*|[。？！]+", text):
        p = p.strip()
        while len(p) > 120:
            cut = [m.end() for m in re.finditer(r"[，；]", p) if 40 <= m.end() <= 90]
            if not cut:
                break
            out.append(p[:cut[-1]])
            p = p[cut[-1]:]
        if p:
            out.append(p)
    return out


def _structure(sents: list[str], prev_head: str, log) -> tuple[list[int], dict[int, str]]:
    """請模型只回傳「哪幾句開新段落、哪裡加小標題」，文字由程式組回去（模型不可能改到內容）"""
    head = f"\n- 前一段的小標題是「{prev_head}」，開頭話題沒變就不要再給小標題。" if prev_head else ""
    text = "\n".join(f"{k + 1}. {s}" for k, s in enumerate(sents))
    try:
        res = llm.generate(_P_STRUCT.format(head=head, text=text), 400, log)
        m = re.search(r"\{.*\}", res, re.S)
        o = json.loads(m.group(0)) if m else {}
        starts = sorted({int(x) - 1 for x in o.get("p", []) if str(x).isdigit() and 0 < int(x) <= len(sents)} | {0})
        heads = {int(k) - 1: textout.to_tw(str(v)).strip()[:20] for k, v in (o.get("h") or {}).items()
                 if str(k).isdigit() and 0 < int(k) <= len(sents) and str(v).strip()}
        for h in heads:  # 有標題的句子一定是段落開頭
            if h not in starts:
                starts = sorted(set(starts) | {h})
        if len(starts) > 1 or len(sents) <= 8:
            return starts, heads
    except Exception as e:
        log(f"  分段失敗，改為每 5 句一段：{e}")
    return list(range(0, len(sents), 5)), {}


def run(base: Path, segs: list[dict], title: str = "", progress: Callable[[float, str], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print) -> dict:
    """兩步驟：① 小批加標點（檢查沒改字）② 只請模型決定分段位置與小標題，文字由程式組回"""
    sp = diarize.load(base)
    lines = _lines(segs, sp)
    if not lines:
        raise llm.LLMError("逐字稿是空的，沒有可整理的內容")
    turns = _turns(lines)
    pieces = [(t, p) for t in turns for p in _pieces(t["lines"])]
    total = sum(len(x["t"]) for x in lines)
    log(f"整理逐字稿：{total} 字、加標點 {len(pieces)} 小段（{llm.MODELS[llm.current()]['label']}）")
    t0 = time.time()
    done, warns = 0, []
    texts: dict[int, list[str]] = {}
    for k, (t, p) in enumerate(pieces):
        if cancelled():
            raise TidyCancelled()
        left = ""
        if done:
            sec = (time.time() - t0) / done * (total - done) * 1.1
            left = f"（約剩 {sec / 60:.0f} 分鐘）" if sec >= 60 else f"（約剩 {sec:.0f} 秒）"
        progress and progress(done / total * 0.9, f"整理逐字稿：加標點 {k + 1}／{len(pieces)}{left}")
        out, ok = _punct(p, log)
        if not ok:
            warns.append(f"{_ts(p[0]['start'])} 起的一小段：模型加標點時改動了內容或沒加標點，改用保守整理（只加逗號句號）")
        texts.setdefault(id(t), []).append(out)
        done += sum(len(x["t"]) for x in p)
    # ② 分段與小標題：每段發言的句子，每 60 句問一次
    progress and progress(0.9, "整理逐字稿：分段與小標題")
    paras, prev_head = [], ""
    for t in turns:
        if cancelled():
            raise TidyCancelled()
        sents = _sentences("".join(texts.get(id(t), [])))
        who = f"{t['who']}：" if t["who"] else ""
        first = True
        for w in range(0, len(sents), STRUCT_SENTS):
            win = sents[w:w + STRUCT_SENTS]
            starts, heads = _structure(win, prev_head, log) if len(win) > 3 else ([0], {})
            bounds = starts + [len(win)]
            for a, b in zip(bounds, bounds[1:]):
                if a >= b:
                    continue
                if a in heads:
                    paras.append(f"■ {heads[a]}")
                    prev_head = heads[a]
                paras.append((who if first else "") + "".join(win[a:b]))
                first = False
    secs = time.time() - t0
    body = "\n\n".join(paras)
    head = f"{title}\n（{datetime.now():%Y-%m-%d} 由本機 LLM 整理標點、分段與小標題；內容以原逐字稿為準）\n\n" if title else ""
    out_file(base).write_text(head + body + "\n", encoding="utf-8-sig")
    meta = {"created": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "seconds": round(secs, 1), "chunks": len(pieces),
            "chars": total, "model": llm.MODELS[llm.current()]["label"], "device": llm.status().get("device", ""),
            "warnings": warns, "method": 2}
    info_file(base).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    if _legacy(base).exists():
        _legacy(base).unlink()
    log(f"整理完成：{len(pieces)} 小段、{secs:.0f} 秒" + (f"；{len(warns)} 小段改用保守整理" if warns else ""))
    return meta


def _core(s: str) -> str:
    s = "\n".join(ln for ln in s.splitlines() if not ln.lstrip().startswith(("##", "■")))
    return _PUNCT.sub("", textout.to_tw(s))


def _same(src: str, out: str) -> float:
    a, b = _core(src), _core(out)
    if not a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _plain(chunk: list[dict]) -> str:
    """保守整理：依停頓（≥1.2 秒）分句，句與句之間加逗號、句尾加句號，文字一字不改"""
    paras, cur = [], []
    for k, x in enumerate(chunk):
        if cur and (chunk[k - 1]["end"] and x["start"] - chunk[k - 1]["end"] >= 1.2 or len(cur) >= 6):
            paras.append(cur)
            cur = []
        cur.append(x["t"].rstrip("，。、 "))
    if cur:
        paras.append(cur)
    return "\n\n".join("，".join(c) + "。" for c in paras)


def _ts(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
