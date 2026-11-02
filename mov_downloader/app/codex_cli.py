"""本機 Codex CLI（OpenAI codex）當作重點摘要的 AI。

用 `codex exec` 非互動模式：提示詞從 stdin 送入，最後回覆寫到暫存檔讀回。
走的是使用者自己登入的 Codex（ChatGPT 帳號或 API key），不經過 Azure，因此沒有 Azure 的內容篩選。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class CodexError(RuntimeError):
    pass


def find_codex(path_setting: str = "") -> str | None:
    """找 codex 執行檔：設定指定的路徑 → PATH（Windows 會找到 codex.cmd）→ npm 全域預設位置"""
    if path_setting:
        p = Path(path_setting.strip().strip('"'))
        if p.is_file():
            return str(p)
        for name in ("codex.cmd", "codex.exe", "codex"):
            if (p / name).is_file():
                return str(p / name)
    w = shutil.which("codex")
    if w:
        return w
    appdata = os.environ.get("APPDATA")
    if appdata:
        for name in ("codex.cmd", "codex.exe"):
            c = Path(appdata) / "npm" / name
            if c.is_file():
                return str(c)
    return None


def _run(args: list[str], stdin: str | None, timeout: int, cwd: str | None = None) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0     # 不要跳出黑色視窗
    return subprocess.run(args, input=stdin.encode("utf-8") if stdin is not None else None,
                          capture_output=True, timeout=timeout, cwd=cwd, creationflags=flags)


def _text(b: bytes) -> str:
    for enc in ("utf-8", "cp950", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def version(exe: str) -> str:
    r = _run([exe, "--version"], None, 30)
    return (_text(r.stdout) or _text(r.stderr)).strip().splitlines()[0] if (r.stdout or r.stderr) else ""


def exec_prompt(prompt: str, exe: str, model: str = "", timeout: int = 600) -> str:
    """執行 codex exec，回傳最後的回覆文字"""
    with tempfile.TemporaryDirectory(prefix="movdl_codex_") as work:
        out = Path(work) / "last.txt"
        opts = ["--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "--color", "never"]
        for _ in range(4):
            args = [exe, "exec", *opts, "-o", str(out)]
            if model:
                args += ["-m", model]
            args.append("-")                      # 提示詞從 stdin 讀（避免命令列長度與跳脫問題）
            try:
                r = _run(args, prompt, timeout, cwd=work)
            except subprocess.TimeoutExpired:
                raise CodexError(f"Codex 超過 {timeout // 60} 分鐘沒有回應") from None
            err = _text(r.stderr)
            # 舊版 codex 不認得某些參數 → 拿掉再試
            bad = re.search(r"unexpected argument '(--[\w-]+)'", err)
            if r.returncode != 0 and bad and bad.group(1) in opts:
                i = opts.index(bad.group(1))
                del opts[i:i + (1 if bad.group(1) in ("--skip-git-repo-check", "--ephemeral") else 2)]
                continue
            break
        text = out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else ""
        if not text and r.returncode == 0:
            text = _text(r.stdout).strip()
        if r.returncode != 0 and not text:
            msg = (err or _text(r.stdout)).strip()
            if re.search(r"log ?in|auth|401|unauthori", msg, re.I):
                raise CodexError("Codex 尚未登入：請在命令提示字元執行 codex login 登入後再試")
            raise CodexError(f"Codex 執行失敗（代碼 {r.returncode}）：{msg[-400:]}")
        return text


def messages_to_prompt(messages: list[dict]) -> str:
    sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
    user_parts = [m["content"] for m in messages if m.get("role") != "system"]
    return ("【任務說明】\n" + "\n\n".join(sys_parts) + "\n\n" if sys_parts else "") + \
        "【注意】這是純文字整理工作：不要執行任何指令、不要讀寫任何檔案，直接依說明輸出結果。\n\n" + \
        "【資料】\n" + "\n\n".join(user_parts)


def chat(messages: list[dict], exe: str, model: str = "", timeout: int = 600) -> str:
    return exec_prompt(messages_to_prompt(messages), exe, model, timeout)


def diagnose(path_setting: str = "", model: str = "") -> dict:
    """設定頁「測試 Codex 連線」：找執行檔 → 版本 → 實際問一次"""
    steps = []

    def add(name, status, detail="", ms=None):
        steps.append({"name": name, "status": status, "detail": detail, "ms": ms})

    exe = find_codex(path_setting)
    if not exe:
        add("找到 Codex CLI", "fail", "找不到 codex。請先安裝：npm install -g @openai/codex，安裝後執行 codex login 登入；"
            "或在下方「Codex 路徑」填入 codex.cmd 的完整路徑")
        return {"ok": False, "steps": steps}
    add("找到 Codex CLI", "ok", exe)
    t0 = time.time()
    try:
        v = version(exe)
        add("版本", "ok", v or "（未回報版本）", round((time.time() - t0) * 1000))
    except Exception as e:
        add("版本", "warn", str(e)[:200])
    t0 = time.time()
    try:
        out = exec_prompt('只輸出這段 JSON，不要加任何其他文字：{"ok": true, "reply": "連線正常"}', exe, model, timeout=180)
        ms = round((time.time() - t0) * 1000)
        ok = '"ok"' in out
        add("實際呼叫 codex exec", "ok" if ok else "warn",
            ("回應：" + out[:200]) + ("" if ok else "（有回應但格式不同，摘要時仍可運作）"), ms)
        return {"ok": True, "steps": steps}
    except Exception as e:
        add("實際呼叫 codex exec", "fail", str(e)[:400], round((time.time() - t0) * 1000))
        return {"ok": False, "steps": steps}
