"""統一錯誤紀錄：所有錯誤與警告都寫進 data/error.log，並標示類別。

格式（一筆一段，多行內容以兩個空白縮排）：
    2026-10-09 11:20:00 [錯誤][翻譯] 訊息
      Traceback ...
"""
from __future__ import annotations

import re
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "error.log"
MAX_BYTES = 5 * 1024 * 1024          # 超過 5 MB 改名為 error.log.1，重新開始
CATEGORIES = ["下載", "訂閱掃描", "Cookie", "翻譯", "AI 連線", "語音辨識", "重點摘要", "影片資訊", "本機 AI", "系統", "伺服器"]
_lock = threading.Lock()
_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(錯誤|警告)\]\[([^\]]+)\] ?(.*)$")


def write(category: str, msg: str, level: str = "錯誤", exc: BaseException | None = None) -> None:
    """寫入一筆紀錄；exc 有值時附上完整錯誤追蹤。寫入失敗不影響程式。"""
    try:
        text = str(msg).strip()
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
            text += "\n" + tb
        lines = text.splitlines() or [""]
        body = lines[0] + "".join("\n  " + ln for ln in lines[1:])
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}][{category}] {body}\n"
        with _lock:
            LOG.parent.mkdir(exist_ok=True)
            if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
                old = LOG.with_name("error.log.1")
                old.unlink(missing_ok=True)
                LOG.rename(old)
            with LOG.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def error(category: str, msg: str, exc: BaseException | None = None) -> None:
    write(category, msg, "錯誤", exc)


def warn(category: str, msg: str, exc: BaseException | None = None) -> None:
    write(category, msg, "警告", exc)


def read(limit: int = 300, category: str = "", level: str = "") -> list[dict]:
    """讀取最新的紀錄（新的在前）。"""
    if not LOG.exists():
        return []
    with _lock:
        raw = LOG.read_text(encoding="utf-8", errors="replace")
    items: list[dict] = []
    for ln in raw.splitlines():
        m = _HEAD.match(ln)
        if m:
            items.append({"time": m.group(1), "level": m.group(2), "category": m.group(3), "message": m.group(4)})
        elif items:
            items[-1]["message"] += "\n" + ln[2:] if ln.startswith("  ") else "\n" + ln
    if category:
        items = [x for x in items if x["category"] == category]
    if level:
        items = [x for x in items if x["level"] == level]
    return items[::-1][:limit]


def clear() -> None:
    with _lock:
        LOG.unlink(missing_ok=True)


def migrate_old() -> None:
    """把舊版的 data/translate.log 併入 error.log（標為翻譯類警告），之後刪除舊檔。"""
    old = ROOT / "data" / "translate.log"
    if not old.exists():
        return
    try:
        out = []
        for ln in old.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$", ln)
            if m:
                out.append(f"{m.group(1)} [警告][翻譯] {m.group(2)}")
            elif ln.strip():
                out.append("  " + ln)
        with _lock:
            LOG.parent.mkdir(exist_ok=True)
            prev = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
            LOG.write_text(("\n".join(out) + "\n" if out else "") + prev, encoding="utf-8")
        old.unlink()
    except Exception:
        pass
