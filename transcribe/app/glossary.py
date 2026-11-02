"""專有名詞庫（階段 5，0.11.0）：glossary\\*.csv（UTF-8 BOM，可用 Excel 編輯）。

三層做法的前兩層：
1. 提示詞：辨識前把「提示優先＝1」（不夠再放 2）的詞組成 Initial Prompt（Whisper 上限約 224 token，取前約 110 字）
2. 誤寫替換：辨識後把「常見誤寫」換成「正確寫法」；「只提醒」欄列出的誤寫本身也是常用詞，不自動替換、只列出提醒
（第 3 層 LLM 校正之後再評估）

另提供：校對前後比對的正確率量測（逐句對齊，因為校對只改文字、不改分句）。
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import re
from datetime import datetime
from pathlib import Path

from . import ROOT

GDIR = ROOT / "glossary"
PROMPT_CHARS = 110


def names() -> list[str]:
    return sorted(p.stem for p in GDIR.glob("*.csv"))


def path(name: str) -> Path:
    p = (GDIR / f"{name}.csv").resolve()
    if p.parent != GDIR.resolve() or not p.exists():
        raise FileNotFoundError(f"找不到詞庫：{name}")
    return p


def load(name: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(path(name).read_text(encoding="utf-8-sig"))))
    out = []
    for r in rows:
        term = (r.get("正確寫法") or "").strip()
        if not term:
            continue
        mis = [x.strip() for x in (r.get("常見誤寫") or "").split("|") if x.strip()]
        flag = {x.strip() for x in (r.get("只提醒") or "").split("|") if x.strip()}
        try:
            pri = int(r.get("提示優先") or 3)
        except ValueError:
            pri = 3
        out.append({"term": term, "mis": mis, "flag": flag, "pri": pri, "cat": r.get("分類", "")})
    return out


def prompt(entries: list[dict]) -> str:
    """提示優先 1 的詞先放，字數還夠再放 2"""
    words, n = [], 0
    for pri in (1, 2):
        for e in entries:
            if e["pri"] == pri and e["term"] not in words:
                if n + len(e["term"]) + 1 > PROMPT_CHARS:
                    return "、".join(words)
                words.append(e["term"])
                n += len(e["term"]) + 1
    return "、".join(words)


def _rules(entries: list[dict]) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """回傳 (自動替換規則［長的先］, 只提醒的誤寫 → 可能的正確寫法)。
    同一個誤寫對到兩個不同的正確寫法時（例如 游輪→油輪／郵輪），也改為只提醒"""
    m: dict[str, set[str]] = {}
    flag: dict[str, set[str]] = {}
    for e in entries:
        for w in e["mis"]:
            (flag if w in e["flag"] else m).setdefault(w, set()).add(e["term"])
    rules = []
    for w, terms in m.items():
        if len(terms) == 1:
            rules.append((w, next(iter(terms))))
        else:
            short = min(terms, key=len)  # 例如 航港居→航港局／交通部航港局：短的包含在長的裡，取短的
            if all(short in t for t in terms):
                rules.append((w, short))
            else:
                flag.setdefault(w, set()).update(terms)
    rules.sort(key=lambda x: -len(x[0]))
    return rules, {w: sorted(t) for w, t in flag.items()}


def apply(segs: list[dict], entries: list[dict]) -> tuple[list[dict], dict]:
    """逐句替換誤寫；回傳 (新的 segs, 報告 {replaced:[{i,from,to,count}], flagged:[{i,word,maybe}]})"""
    rules, flag = _rules(entries)
    replaced, flagged = [], []
    out = []
    for i, s in enumerate(segs):
        t = s["text"]
        for w, right in rules:  # 長的誤寫先換，避免短的先把長詞拆壞
            if w in t:
                c = t.count(w)
                t = t.replace(w, right)
                replaced.append({"i": i, "from": w, "to": right, "count": c})
        for w, maybe in flag.items():
            if w in t:
                flagged.append({"i": i, "word": w, "maybe": maybe})
        out.append({**s, "text": t})
    return out, {"replaced": replaced, "flagged": flagged}


def report_file(base: Path) -> Path:
    return base.parent / (base.name + ".glossary.json")


def save_report(base: Path, name: str, rep: dict, prompt_text: str = "") -> dict:
    data = {"glossary": name, "prompt": prompt_text, "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}", **rep}
    report_file(base).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_report(base: Path) -> dict | None:
    try:
        return json.loads(report_file(base).read_text(encoding="utf-8"))
    except Exception:
        return None


def add_miswrite(name: str, term: str, wrong: str) -> bool:
    """把量測找到的誤聽寫法加進該詞的「常見誤寫」（已存在就不重複）"""
    p = path(name)
    rows = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
    fields = list(rows[0].keys()) if rows else ["正確寫法", "常見誤寫", "只提醒", "分類", "英文／全稱", "說明", "提示優先"]
    wrong, term = wrong.strip(), term.strip()
    if not wrong or not term or wrong == term:
        return False
    hit = False
    for r in rows:
        if r.get("正確寫法", "").strip() == term:
            mis = [x for x in (r.get("常見誤寫") or "").split("|") if x]
            if wrong not in mis:
                mis.append(wrong)
                r["常見誤寫"] = "|".join(mis)
            hit = True
    if not hit:
        return False
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    p.write_text(out.getvalue(), encoding="utf-8-sig")
    return True


# ------------------------------------------------------------------ 從校對結果找出可加入詞庫的修改（0.11.2）
_PUNCT = re.compile(r"^[\s，。、！？；：,.!?;:「」『』（）()\-—…~～\"']*$")


def _likely(wrong: str, right: str, kind: str) -> dict:
    """（規則版）這處修改像不像專有名詞：詞庫已有的詞、含英文／數字、中文同音字替換 → 像；
    單純補字／刪字、只改一個字 → 比較像一般用字修正。之後可改由本機 LLM 判讀"""
    hz = lambda x: len(re.findall(r"[\u3400-\u9fff]", x))
    if kind in ("mis", "known"):
        return {"likely": True, "why": "詞庫裡的詞"}
    if not wrong or wrong in right or right in wrong:
        return {"likely": False, "why": "補字／刪字"}
    if re.search(r"[A-Za-z]{2,}", right):
        return {"likely": True, "why": "英文名稱／縮寫"}
    if hz(right) >= 2 and hz(wrong) >= 2 and abs(len(right) - len(wrong)) <= 1:
        return {"likely": True, "why": "可能是聽錯的同音詞"}
    return {"likely": False, "why": "一般用字修正"}


def edits(ref_segs: list[dict], hyp_segs: list[dict], entries: list[dict]) -> list[dict]:
    """逐句比對辨識原稿（hyp）與校對版（ref），列出每一處修改：{i, wrong, right, context, kind, term}
    kind：mis＝加到既有詞的常見誤寫／new＝新詞／known＝詞庫已有這個誤寫"""
    terms = {e["term"]: e for e in entries}
    out, seen = [], set()
    for i, (r, h) in enumerate(zip(ref_segs, hyp_segs)):
        a, b = h["text"], r["text"]
        if a == b:
            continue
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        ops = [o for o in sm.get_opcodes() if o[0] != "equal"]
        # 相鄰（間隔 ≤1 字）的修改合併成一處
        merged = []
        for o in ops:
            if merged and o[1] - merged[-1][2] <= 1 and o[3] - merged[-1][4] <= 1:
                m = merged[-1]
                merged[-1] = ("replace", m[1], o[2], m[3], o[4])
            else:
                merged.append(o)
        for _, i1, i2, j1, j2 in merged:
            if _PUNCT.match(a[i1:i2]) and _PUNCT.match(b[j1:j2]):
                continue  # 只改了標點或空白
            # 改到的地方屬於詞庫的詞：擴成整個詞（取最長的那個）
            hits = [(t, m.start(), m.end()) for t in terms for m in re.finditer(re.escape(t), b)
                    if m.start() < max(j2, j1 + 1) and m.end() > j1]
            if hits:
                t, rs, re_ = max(hits, key=lambda x: len(x[0]))
                hs, he = i1 - (j1 - rs), i2 + (re_ - j2)  # 前後相同的部分，在原稿對應同樣長度
                hs, he = max(0, hs), min(len(a), max(he, hs))
                wrong, right = a[hs:he], t
                jl, jr = rs, re_
            else:
                # 不在詞庫：英文擴到整個字；中文至少兩個字
                units = lambda x: len(re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]", x))
                def grow():
                    nonlocal i1, i2, j1, j2
                    changed = False
                    while i1 > 0 and j1 > 0 and a[i1 - 1] == b[j1 - 1] and a[i1 - 1].isalnum() and a[i1 - 1].isascii() \
                            and (b[j1:j1 + 1].isascii() and b[j1:j1 + 1].isalnum() or a[i1:i1 + 1].isascii() and a[i1:i1 + 1].isalnum()):
                        i1, j1, changed = i1 - 1, j1 - 1, True
                    while i2 < len(a) and j2 < len(b) and a[i2] == b[j2] and a[i2].isalnum() and a[i2].isascii() \
                            and (b[j2 - 1:j2].isascii() and b[j2 - 1:j2].isalnum() or a[i2 - 1:i2].isascii() and a[i2 - 1:i2].isalnum()):
                        i2, j2, changed = i2 + 1, j2 + 1, True
                    return changed
                grow()
                han = lambda x: len(re.findall(r"[\u3400-\u9fff]", x))
                is_han = lambda c: bool(re.match(r"[\u3400-\u9fff]", c or ""))
                need = lambda: (units(a[i1:i2]) < 2 or units(b[j1:j2]) < 2
                                or (han(b[j1:j2]) or han(a[i1:i2])) and (han(b[j1:j2]) < 2 or han(a[i1:i2]) < 2))
                n = 0
                while need() and n < 4:
                    n += 1
                    L = i1 > 0 and j1 > 0 and a[i1 - 1] == b[j1 - 1]
                    R = i2 < len(a) and j2 < len(b) and a[i2] == b[j2]
                    if L and is_han(a[i1 - 1]):          # 優先往前帶一個中文字（例：港陸→港內）
                        i1, j1 = i1 - 1, j1 - 1
                    elif R and is_han(a[i2]):            # 再往後帶一個中文字（例：idea 工→idea 工業）
                        i2, j2 = i2 + 1, j2 + 1
                    elif R:
                        i2, j2 = i2 + 1, j2 + 1
                    elif L:
                        i1, j1 = i1 - 1, j1 - 1
                    else:
                        break
                    grow()
                wrong, right = a[i1:i2].strip(), b[j1:j2].strip()
                jl, jr = j1, j2
            if not right or wrong == right:
                continue
            key = (wrong, right)
            if key in seen:
                continue
            seen.add(key)
            e = terms.get(right)
            kind = ("known" if wrong in e["mis"] else "mis") if e else "new"
            # 誤寫被正確寫法包含（只是補字，例：高雄→高雄港），自動替換會把正常用法也改掉：預設只提醒
            risky = bool(wrong) and wrong in right
            pre, post = b[max(0, jl - 12):jl], b[jr:jr + 12]  # 前後文（校對後的句子），供顯示與擴大範圍
            out.append({"i": i, "wrong": wrong, "right": right, "pre": pre, "post": post,
                        "context": f"{pre}【{wrong}→{right}】{post}", "kind": kind,
                        "term": right if e else "", "risky": risky, **_likely(wrong, right, kind)})
    return out


def add_entry(name: str, term: str, wrong: str, flag: bool = False) -> str:
    """加入詞庫：term 已存在→把 wrong 加進常見誤寫（flag＝只提醒）；不存在→新增一列（分類「待分類」、提示優先 2）。
    回傳 'mis'／'new'／'exists'"""
    p = path(name)
    rows = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
    fields = list(rows[0].keys()) if rows else ["正確寫法", "常見誤寫", "只提醒", "分類", "英文／全稱", "說明", "提示優先"]
    if "只提醒" not in fields:
        fields.insert(fields.index("常見誤寫") + 1, "只提醒")
    term, wrong = term.strip(), wrong.strip()
    if not term:
        raise ValueError("正確寫法不可空白")
    res = "exists"
    for r in rows:
        if r.get("正確寫法", "").strip() == term:
            mis = [x for x in (r.get("常見誤寫") or "").split("|") if x]
            fl = [x for x in (r.get("只提醒") or "").split("|") if x]
            if wrong and wrong != term and wrong not in mis:
                mis.append(wrong)
                res = "mis"
            if flag and wrong and wrong not in fl:
                fl.append(wrong)
            r["常見誤寫"], r["只提醒"] = "|".join(mis), "|".join(fl)
            break
    else:
        rows.append({k: "" for k in fields} | {"正確寫法": term, "常見誤寫": wrong if wrong != term else "",
                                               "只提醒": wrong if flag and wrong != term else "", "分類": "待分類",
                                               "說明": "由校對結果加入", "提示優先": "2"})
        res = "new"
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, lineterminator="\r\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    p.write_text(out.getvalue(), encoding="utf-8-sig")
    return res


# ------------------------------------------------------------------ 正確率量測
def _dist(a: str, b: str) -> int:
    """字元編輯距離（算字錯率用）"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _aligned(ref: str, hyp: str, term: str) -> list[str]:
    """ref 裡 term 出現的位置，在 hyp 對應到什麼寫法（找出模型把專有名詞聽成什麼）"""
    out = []
    sm = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    for m in re.finditer(re.escape(term), ref):
        a0, a1 = m.start(), m.end()
        b0 = b1 = None
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if i2 <= a0 or i1 >= a1:
                continue
            s = j1 + (max(a0, i1) - i1 if tag == "equal" else 0)
            e = j1 + (min(a1, i2) - i1 if tag == "equal" else j2 - j1)
            b0 = s if b0 is None else min(b0, s)
            b1 = e if b1 is None else max(b1, e)
        got = hyp[b0:b1] if b0 is not None else ""
        if got != term:
            out.append(got)
    return out


def measure(ref_segs: list[dict], hyp_segs: list[dict], entries: list[dict], t0: float, t1: float) -> dict:
    """校對後（ref）與辨識原稿（hyp）逐句比對，只看 t0～t1 秒內的句子"""
    idx = [i for i, s in enumerate(ref_segs) if t0 <= s["start"] < t1 and i < len(hyp_segs)]
    ref = [ref_segs[i]["text"] for i in idx]
    hyp = [hyp_segs[i]["text"] for i in idx]
    rc = sum(len(x) for x in ref)
    cer = sum(_dist(a, b) for a, b in zip(ref, hyp)) / rc if rc else 0.0
    terms = []
    total = ok = false_pos = 0
    for e in entries:
        T = e["term"]
        n_ref = sum(x.count(T) for x in ref)
        n_hyp = sum(x.count(T) for x in hyp)
        if not n_ref and not n_hyp:
            continue
        good = sum(min(a.count(T), b.count(T)) for a, b in zip(ref, hyp))
        wrong = []
        for a, b in zip(ref, hyp):
            if a.count(T) > b.count(T):
                wrong += [w for w in _aligned(a, b, T) if w]
        fp = max(0, n_hyp - good)
        total += n_ref
        ok += good
        false_pos += fp
        terms.append({"term": T, "ref": n_ref, "ok": good, "fp": fp, "wrong": sorted(set(wrong)),
                      "known": [w for w in set(wrong) if w in e["mis"]]})
    terms.sort(key=lambda x: (x["ok"] - x["ref"], -x["ref"]))
    return {"lines": len(idx), "chars": rc, "cer": round(cer, 4), "terms_total": total, "terms_ok": ok,
            "term_acc": round(ok / total, 4) if total else None, "false_pos": false_pos, "terms": terms,
            "edited_lines": sum(1 for a, b in zip(ref, hyp) if a != b)}
