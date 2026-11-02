"""命令列：轉一個或多個影音檔。

  .venv\\Scripts\\python -m app.cli 檔案1 [檔案2 ...] [--model breeze|turbo] [--device auto|GPU|CPU] [--lang auto|zh|en]
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from . import pipeline, version
from .config import recommended


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="影音檔轉逐字稿（SRT／TXT）")
    ap.add_argument("files", nargs="+", help="影片或聲音檔")
    ap.add_argument("--model", default="", choices=["", "breeze", "turbo"],
                    help="不指定時採用本機實測建議（網頁「本機效能」），未實測則用 breeze")
    ap.add_argument("--device", default="", help="auto（先顯示卡後 CPU）、GPU、CPU；不指定時採用本機實測建議")
    ap.add_argument("--lang", default="auto", help="auto、zh、en…")
    ap.add_argument("--prompt", default="", help="Initial Prompt（專有名詞提示，選填）")
    a = ap.parse_args(argv)

    rec = recommended()
    model = a.model or rec["model"]
    device = a.device or rec["device"]
    print(f"=== 影音轉逐字稿 v{version()} ===")
    src = "本機實測建議" if rec["source"] == "benchmark" and not (a.model or a.device) else "預設"
    print(f"配置：{model}／{device}（{src}）")
    if rec["source"] != "benchmark":
        print("提示：這台電腦尚未做效能實測，開啟 start.cmd 到「本機效能」頁可找出最佳配置（約 5–10 分鐘）。")
    ok = 0
    for f in a.files:
        print()
        try:
            r = pipeline.run(Path(f), model, device, a.lang, a.prompt)
            ok += 1
            if os.name == "nt":
                os.startfile(r["job_dir"])  # 完成後開啟資料夾
        except Exception as e:
            print(f"[錯誤] {f}：{e}")
            if os.environ.get("TRANSCRIBE_DEBUG"):
                traceback.print_exc()
    print(f"\n完成 {ok}／{len(a.files)} 個檔案。")
    return 0 if ok == len(a.files) else 1


if __name__ == "__main__":
    sys.exit(main())
