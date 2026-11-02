"""字幕處理：SRT 解析/輸出、粵語→台灣正體、簡體→台灣正體、英文翻譯。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .fsutil import atomic_write_text

try:
    import opencc  # opencc-python-reimplemented
    _HK2S = opencc.OpenCC("hk2s")
    # 1.24.0：改用 s2tw（只轉字形）。s2twp 會連詞彙一起換，把原本正確的詞也改錯
    # （連接詞→連線詞、對象→物件、發佈→釋出；transcribe 專案 2026-10-31 實測）。
    _S2TW = opencc.OpenCC("s2tw")
except Exception:  # pragma: no cover
    _HK2S = _S2TW = None

# 大陸用語 → 臺灣用語：只收錄「幾乎不會換錯」的詞（像 文件、程序、項目、菜單 這類一般詞也常用的不收）
_TW_TERMS = [
    ("視頻", "影片"), ("軟件", "軟體"), ("硬件", "硬體"), ("網絡", "網路"), ("互聯網", "網際網路"),
    ("鼠標", "滑鼠"), ("屏幕", "螢幕"), ("硬盤", "硬碟"), ("U盤", "隨身碟"), ("內存", "記憶體"),
    ("默認", "預設"), ("博客", "部落格"), ("短信", "簡訊"), ("激活", "啟用"), ("打印機", "印表機"),
    ("打印", "列印"), ("鏈接", "連結"), ("服務器", "伺服器"), ("數據庫", "資料庫"),
    ("攝像頭", "鏡頭"), ("筆記本電腦", "筆電"), ("台式機", "桌機"), ("移動端", "行動裝置"),
    ("社交媒體", "社群媒體"), ("點贊", "按讚"), ("帖子", "貼文"),
]


def _tw_terms(text: str) -> str:
    for a, b in _TW_TERMS:
        text = text.replace(a, b)
    return text


class TranslationFailed(Exception):
    pass


@dataclass
class Cue:
    start: str
    end: str
    text: str


_TIME = r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
_TIME_RE = re.compile(_TIME + r"\s*-->\s*" + _TIME)
_TAG_RE = re.compile(r"</?[^>]+>|\{\\[^}]*\}")


def _norm_time(t: str) -> str:
    t = t.replace(".", ",")
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms.ljust(3, '0')[:3]}"


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.split("\n")]
        for i, line in enumerate(lines):
            m = _TIME_RE.search(line)
            if m:
                body = "\n".join(lines[i + 1:]).strip()
                body = _TAG_RE.sub("", body)
                if body:
                    cues.append(Cue(_norm_time(m.group(1)), _norm_time(m.group(2)), body))
                break
    return clean_rolling(cues)


def _ms(t: str) -> int:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _fmt_ms(v: int) -> str:
    v = max(0, v)
    return f"{v // 3600000:02d}:{v // 60000 % 60:02d}:{v // 1000 % 60:02d},{v % 1000:03d}"


def clean_rolling(cues: list[Cue]) -> list[Cue]:
    """整理 YouTube 自動字幕的「滾動式」格式：

    原始檔每一格常是「上一句＋新的一句」兩行、且時間互相重疊，
    播放時會同時出現兩排、甚至上下跳動。這裡只保留每格新增的那一行，並修正重疊時間。
    """
    out: list[Cue] = []
    for c in cues:
        lines = [l.strip() for l in c.text.split("\n") if l.strip()]
        if out:
            prev = out[-1].text.split("\n")
            while lines and lines[0] in prev:
                lines.pop(0)
        if not lines:
            continue
        start, end = _ms(c.start), _ms(c.end)
        if end - start < 60:           # 10 毫秒這類過場格
            continue
        if out:
            if lines == out[-1].text.split("\n"):
                out[-1].end = c.end
                continue
            if _ms(out[-1].end) > start:
                out[-1].end = c.start
        out.append(Cue(c.start, c.end, "\n".join(lines)))
    return out


def dump_srt(cues: list[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{c.start} --> {c.end}\n{c.text}\n")
    return "\n".join(out)


def srt_to_vtt(text: str) -> str:
    cues = parse_srt(text)
    out = ["WEBVTT", ""]
    for c in cues:
        out.append(f"{c.start.replace(',', '.')} --> {c.end.replace(',', '.')}")
        out.append(c.text)
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 語言判斷
_CANTO_MARKERS = re.compile(r"[嘅咗喺唔冇佢哋嘢睇啲咁嚟搵攞俾畀嗰乜諗瞓啱噉囉喎嘞]")
_CJK = re.compile(r"[一-鿿]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_LETTER = re.compile(r"[^\W\d_]", re.U)


def base_lang(code: str) -> str:
    """'ar-orig' → 'ar'，'en-US' → 'en'，'zh-Hant' 保留"""
    code = re.sub(r"-orig$", "", code or "", flags=re.I)
    if code.lower().startswith(("zh", "yue")):
        return code
    return code.split("-")[0].split("_")[0] or "orig"


def is_chinese(lang: str) -> bool:
    return lang in ("yue", "zh-Hant", "zh-Hans")


def detect_lang(cues: list[Cue], hint: str = "") -> str:
    """依字幕「內容」判斷語言（不只看標籤）。

    回傳 'yue' | 'zh-Hant' | 'zh-Hans' 表示中文；其他情況回傳外語代碼（如 en、ar、ja）。
    """
    h = (hint or "").lower()
    sample = "".join(c.text for c in cues[:400])
    letters = len(_LETTER.findall(sample)) or 1
    cjk = len(_CJK.findall(sample))
    if len(_KANA.findall(sample)) > letters * 0.05:
        return "ja"
    if len(_HANGUL.findall(sample)) > letters * 0.2:
        return "ko"
    if cjk < letters * 0.3:           # 中文字比例太低 → 外語
        b = base_lang(hint)
        return b if b and not b.lower().startswith(("zh", "yue")) else "und"
    canto = len(_CANTO_MARKERS.findall(sample))
    if h.startswith("yue") or h == "zh-hk" or canto / max(cjk, 1) > 0.015:
        return "yue"
    if _S2TW is not None:
        simp = _HK2S.convert(sample)
        if sum(a != b for a, b in zip(simp, sample)) > cjk * 0.02:
            return "zh-Hant"
        return "zh-Hans"
    return "zh-Hant" if "hant" in h or "tw" in h else "zh-Hans"


# ---------------------------------------------------------------- 粵語口語 → 書面語
# 由長到短比對；只收錄誤判風險低的常見口語詞
_CANTO_MAP = [
    ("喺度", "在這裡"), ("佢話", "他說"), ("我話", "我說"), ("你話", "你說"), ("話俾", "告訴"),
    ("幾好", "蠻好"), ("睇緊", "正在看"), ("做緊", "正在做"), ("講緊", "正在說"), ("講嘢", "說話"),
    ("點解", "為什麼"), ("乜嘢", "什麼"), ("咩嘢", "什麼"), ("邊個", "誰"), ("邊度", "哪裡"),
    ("邊啲", "哪些"), ("而家", "現在"), ("宜家", "現在"), ("琴日", "昨天"), ("聽日", "明天"),
    ("今日", "今天"), ("尋日", "昨天"), ("呢個", "這個"), ("嗰個", "那個"), ("呢啲", "這些"),
    ("嗰啲", "那些"), ("呢度", "這裡"), ("嗰度", "那裡"), ("呢排", "最近"), ("佢哋", "他們"),
    ("我哋", "我們"), ("你哋", "你們"), ("唔係", "不是"), ("唔好", "不要"), ("唔該", "謝謝"),
    ("唔知", "不知道"), ("唔使", "不用"), ("唔會", "不會"), ("冇問題", "沒問題"), ("冇嘢", "沒事"),
    ("鍾意", "喜歡"), ("好似", "好像"), ("點樣", "怎麼樣"), ("幾多", "多少"), ("識得", "懂得"),
    ("嘢食", "食物"), ("做嘢", "做事"), ("返工", "上班"), ("放工", "下班"), ("返屋企", "回家"),
    ("屋企", "家裡"), ("傾偈", "聊天"), ("咁樣", "這樣"), ("噉樣", "這樣"), ("冇", "沒有"),
    ("嘅", "的"), ("咗", "了"), ("喺", "在"), ("佢", "他"), ("唔", "不"),
    ("啲", "些"), ("嘢", "東西"), ("睇", "看"), ("嚟", "來"), ("搵", "找"), ("攞", "拿"),
    ("俾", "給"), ("畀", "給"), ("嗰", "那"), ("諗", "想"), ("瞓", "睡"),
    ("啱", "對"), ("咁", "這麼"), ("噉", "這樣"), ("乜", "什麼"), ("嘞", "了"),
]
# 需避開一般詞彙的單字（例如「關係」「呢喃」）
_CANTO_REGEX = [
    (re.compile(r"(?<![關聯维維])係(?![數统統])"), "是"),
    (re.compile(r"呢(?=[個度啲排間位隻件次])"), "這"),
]


def canto_to_written(text: str) -> str:
    for a, b in _CANTO_MAP:
        if len(a) > 1:
            text = text.replace(a, b)
    for rx, b in _CANTO_REGEX:
        text = rx.sub(b, text)
    for a, b in _CANTO_MAP:
        if len(a) == 1:
            text = text.replace(a, b)
    return text


def to_taiwan(text: str, source: str) -> str:
    """source: 'yue' | 'zh-Hant' | 'zh-Hans'"""
    if _S2TW is None:
        return text
    if source == "yue":
        text = canto_to_written(text)
    if source in ("yue", "zh-Hant"):
        text = _HK2S.convert(text)
    return _tw_terms(_S2TW.convert(text))


# ---------------------------------------------------------------- 英文翻譯
def untranslated_lines(src: list[Cue], zh: list[Cue]) -> list[int] | None:
    """比對原文與中文字幕，回傳還沒翻好的行號；兩份字幕對不上時回傳 None"""
    from . import translator
    if len(src) != len(zh) or any(a.start != b.start for a, b in zip(src, zh)):
        return None
    return [i for i, (a, b) in enumerate(zip(src, zh)) if not translator.is_translated(a.text, b.text)]


def translate_cues(cues: list[Cue], target: str = "zh-TW", progress=None,
                   engine: str = "google", context: str = "",
                   existing: list[Cue] | None = None) -> list[Cue]:
    """existing：既有的中文字幕；有的話只補翻還沒翻好的行"""
    from . import translator

    texts = [c.text.replace("\n", " ") for c in cues]
    only = untranslated_lines(cues, existing) if existing is not None else None
    def hms(t: str) -> str:          # 00:12:30,500 → 12:30
        sec = _ms(t) // 1000
        h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
        return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"
    times = [hms(c.start) for c in cues]
    results, _ = translator.translate(texts, engine=engine, context=context, progress=progress, only=only,
                                      labels=times)
    if only is not None:   # 已翻好的行沿用既有譯文
        keep = set(only)
        results = [results[k] if k in keep else existing[k].text for k in range(len(texts))]
    out_cues = []
    for c, t in zip(cues, results):
        t = _tw_terms(_S2TW.convert(t)) if _S2TW else t
        out_cues.append(Cue(c.start, c.end, t.strip()))
    return out_cues


# ---------------------------------------------------------------- 主流程
def process_subtitle(path: Path, base: Path, lang_hint: str, en_mode: str,
                     canto_written: bool = True, progress=None,
                     engine: str = "google", context: str = "", fill_only: bool = False) -> list[Path]:
    """處理下載回來的 SRT，輸出最終檔案並回傳檔案列表。

    base: 影片檔去除副檔名的路徑（例如 download/標題）
    en_mode: 外語字幕處理 keep | translate | bilingual（適用所有非中文字幕）
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    cues = parse_srt(raw)
    if not cues:
        return []
    lang = detect_lang(cues, lang_hint)
    outputs: list[Path] = []

    def write(suffix: str, data: list[Cue]) -> Path:
        p = Path(f"{base}.{suffix}.srt")
        atomic_write_text(p, dump_srt(data))
        outputs.append(p)
        return p

    if lang in ("yue", "zh-Hant", "zh-Hans"):
        src = lang
        if lang == "yue" and not canto_written:
            src = "zh-Hant"
        conv = [Cue(c.start, c.end, to_taiwan(c.text, src)) for c in cues]
        write("zh-TW", conv)
    else:
        # 外語（英文、阿拉伯文、日文…）：保留原文，並依設定翻譯成正體中文
        write(lang, cues)
        if en_mode in ("translate", "bilingual"):
            try:
                existing = None
                zp = Path(f"{base}.zh-TW.srt")
                if fill_only and zp.exists():
                    existing = parse_srt(zp.read_text(encoding="utf-8", errors="replace"))
                zh = translate_cues(cues, progress=progress, engine=engine, context=context, existing=existing)
            except Exception as e:
                from . import errlog
                errlog.error("翻譯", f"翻譯失敗（引擎 {engine}）：{base.name}", e)
                if path.resolve() != outputs[0].resolve():
                    path.unlink(missing_ok=True)
                msg = str(e) if "AI 翻譯" in str(e) else (
                    f"{'AI' if engine == 'llm' else 'Google'} 翻譯發生錯誤：{type(e).__name__} {str(e)[:200]}"
                    "（詳細內容在 data\\error.log）")
                raise TranslationFailed(f"{msg}；已保留原文字幕（{lang}）")
            from . import translator as _tr
            bad = [i for i, (a, b) in enumerate(zip(cues, zh)) if not _tr.is_translated(a.text, b.text)]
            if len(bad) > len(cues) * 0.8:
                if path.resolve() != outputs[0].resolve():
                    path.unlink(missing_ok=True)
                why = ("AI 回應幾乎都沒有中文，詳見 data\\error.log" if engine == "llm"
                       else "可能無法連線 Google 翻譯")
                raise TranslationFailed(f"字幕翻譯失敗（{why}），已保留原文字幕（{lang}）")
            write("zh-TW", zh)
            if bad and progress:
                try:
                    progress(1.0, f"{len(bad)} 句未翻譯成功")
                except TypeError:
                    progress(1.0)
            if en_mode == "bilingual":
                bi = [Cue(a.start, a.end, f"{b.text}\n{a.text}") for a, b in zip(cues, zh)]
                write(f"zh-TW-{lang}", bi)

    try:
        if path.resolve() not in [o.resolve() for o in outputs]:
            path.unlink()
    except Exception:
        pass
    return outputs
