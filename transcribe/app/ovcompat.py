"""OpenVINO 版本相容檢查（0.13.7）。

模型若是用較新版 OpenVINO 轉出，舊版執行環境載入時會在底層當掉（Windows access violation），
Python 攔不到、整個程式直接結束，網頁只看到「Failed to fetch」。
所以載入前先比對：模型的 Runtime_version（寫在 .xml 結尾的 rt_info）比執行環境新，就改成一般錯誤並說明怎麼處理。
2026-11-02 HQO302003 實測：Python 3.9 只能裝到 OpenVINO 2025.3，載入 2026.4 轉出的 Breeze 模型即當掉。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_RT = re.compile(rb'Runtime_version value="(\d+)\.(\d+)')
FIX = ("請到 https://www.python.org/downloads/ 安裝 Python 3.11（勾選「Add python.exe to PATH」），"
       "再重新執行 setup.cmd：會自動改用新版 Python 重建執行環境（舊的 .venv 改名保留，不會刪除）。")


@lru_cache(maxsize=1)
def runtime() -> tuple[int, int] | None:
    try:
        import openvino as ov
        m = re.match(r"(\d+)\.(\d+)", ov.__version__)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None


def model_version(model_dir: Path) -> tuple[int, int] | None:
    """模型資料夾內各 .xml 記錄的轉換版本，取最新的（rt_info 在檔尾，只讀最後 64KB）"""
    best = None
    for f in Path(model_dir).glob("openvino_*.xml"):
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 65536))
                m = _RT.search(fh.read())
        except OSError:
            continue
        if m:
            v = (int(m.group(1)), int(m.group(2)))
            best = v if best is None or v > best else best
    return best


def problem(model_dir: Path) -> str:
    """模型比執行環境新 → 回傳說明文字；相容或無法判斷 → 空字串"""
    rt, mv = runtime(), model_version(model_dir)
    if rt and mv and mv > rt:
        return (f"這台電腦的 OpenVINO 是 {rt[0]}.{rt[1]} 版，但模型 {Path(model_dir).name} 是用 {mv[0]}.{mv[1]} 版轉出的，"
                f"直接載入會讓程式當掉。{FIX}")
    return ""
