"""檔案寫入工具（1.24.0，參考 transcribe 專案的做法）。

設定、佇列、影片資訊等 JSON 檔如果直接覆寫，寫到一半程式被關掉（關視窗、當機、斷電），
檔案就只剩一半、下次讀不出來。改為：先寫到同資料夾的暫存檔 → 寫完 → 一次換上去，
任何時候磁碟上的檔案不是舊的完整版、就是新的完整版。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows：防毒軟體或其他程式剛好開著目標檔時，替換會暫時失敗，稍等重試
        for i in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 9:
                    raise
                time.sleep(0.05 * (i + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path | str, data, indent: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))
