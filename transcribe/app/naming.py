"""檔名與路徑：讓上傳檔、網址下載的檔名在 Windows 上一定能用（0.7.1）。

- 不合法字元 \\ / : * ? " < > | 換成全形（：／＊？…），保留可讀性
- 去掉控制字元、表情符號等擴充字元（部分 Windows 工具與命令列無法處理）、零寬字元
- 去掉結尾的空白與句點；避開 CON、NUL、COM1 等保留名稱
- 依媒體檔位置的長度自動縮短，整條路徑控制在 240 字元內（Windows 傳統上限 260）
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_FULLWIDTH = str.maketrans({"\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？", '"': "＂",
                            "<": "＜", ">": "＞", "|": "｜"})
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_PATH = 240


def safe_name(name: str, max_len: int = 80, fallback: str = "audio") -> str:
    s = unicodedata.normalize("NFC", str(name or ""))
    s = s.translate(_FULLWIDTH)
    s = re.sub(r"\s+", " ", s)  # Tab、換行 → 空格
    s = "".join(ch for ch in s
                if ord(ch) <= 0xFFFF                        # 表情符號等擴充字元
                and unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")  # 控制、零寬、代理、私用字元
                and not 0xFE00 <= ord(ch) <= 0xFE0F)        # 變體選擇符
    s = re.sub(r"\s+", " ", s).strip()
    s = s[:max_len].rstrip(" .")
    s = s.lstrip(" .")
    if not s:
        s = fallback
    if s.split(".")[0].strip().upper() in _RESERVED:
        s = "_" + s
    return s


def clean_title(title: str) -> str:
    """畫面顯示用的標題：去掉控制字元與零寬字元、合併空白（表情符號保留）"""
    s = unicodedata.normalize("NFC", str(title or ""))
    s = re.sub(r"\s+", " ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf", "Co"))
    return s.strip()


def name_budget(base: Path, prefix_len: int = 16, ext_len: int = 5) -> int:
    """在 base\\<前綴_標題>\\<標題>.<副檔名> 的結構下，標題最多可用幾個字（20～80）"""
    room = MAX_PATH - len(str(base)) - prefix_len - ext_len - 2
    return max(20, min(80, room // 2))
