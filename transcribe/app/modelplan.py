"""這台電腦的模型配置方案（0.14.0）。

專案需求清單 models.json（開發時維護）＋ 這台的功能需求（使用者勾選）＋ 電腦環境（等級、記憶體）＋ 效能實測
→ 每個功能用哪個模型、哪個裝置；只準備方案裡的模型，其他只告知、不刪除。
結果寫入 env-reports\\<電腦名稱>.models.json（每台一份）；電腦別名與功能選擇存在 data\\settings.json。

命令列（setup.cmd 使用，需在專案根目錄執行）：
    python -m app.modelplan configure [--ask]   第一次（或 --ask）詢問電腦別名與要用的功能
    python -m app.modelplan prepare [--yes]     準備缺少的模型（共用來源複製或 Hugging Face 下載）
    python -m app.modelplan report              顯示並寫出這台的配置方案
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import ROOT, version
from .config import computer_name, env_report, load_settings, models_root, recommended, save_settings

MANIFEST = ROOT / "models.json"
FEATURES = ("transcribe", "tidy", "translate", "diarize")
OPTIONAL = ("tidy", "translate", "diarize")
DEV_LABEL = {"GPU": "Intel 顯示卡", "CPU": "CPU", "AUTO": "自動（Intel 顯示卡優先，失敗改 CPU）",
             "OV-GPU": "OpenVINO＋Intel 顯示卡", "OV-CPU": "OpenVINO＋CPU", "TORCH": "PyTorch（CPU）"}


# ------------------------------------------------------------------ 讀設定
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _models() -> dict[str, dict]:
    return {m["id"]: m for m in manifest()["models"]}


def alias() -> str:
    return (load_settings().get("computer_alias") or "").strip()


def display_name() -> str:
    a, c = alias(), computer_name()
    return f"{a}（{c}）" if a else c


def model_path(m: dict) -> Path:
    return models_root().joinpath(*m["path"].split("/"))


def is_ready(m: dict, root: Path | None = None) -> bool:
    d = (root or models_root()).joinpath(*m["path"].split("/"))
    return d.is_dir() and all((d / c).exists() for c in m.get("check", []))


def chosen_features() -> tuple[list[str], bool]:
    """回傳 (功能清單, 是否已由使用者選過)。沒選過時：轉逐字稿＋模型已就緒的功能（維持舊版行為）"""
    v = load_settings().get("features")
    if isinstance(v, list):
        return ["transcribe"] + [f for f in OPTIONAL if f in v], True
    ms = _models().values()
    have = {m["feature"] for m in ms if m.get("enabled", True) and is_ready(m)}
    return ["transcribe"] + [f for f in OPTIONAL if f in have], False


def save_choice(features: list[str] | None = None, alias_text: str | None = None) -> None:
    data = load_settings()
    if features is not None:
        data["features"] = [f for f in OPTIONAL if f in features]
    if alias_text is not None:
        a = " ".join(alias_text.split())[:30]
        if a:
            data["computer_alias"] = a
        else:
            data.pop("computer_alias", None)
    save_settings(data)


def _ram() -> float:
    v = env_report().get("RamGB")
    if v:
        return float(v)
    try:
        import psutil
        return psutil.virtual_memory().total / 2**30
    except Exception:
        return 0.0


def source_root() -> Path | None:
    """共用模型來源（網芳／隨身碟）：網頁設定 models_source → 環境變數 MODELS_SOURCE"""
    v = (load_settings().get("models_source") or os.environ.get("MODELS_SOURCE") or "").strip().strip('"')
    if v and Path(v).is_dir() and Path(v).resolve() != models_root().resolve():
        return Path(v)
    return None


# ------------------------------------------------------------------ 決定方案
def _llm_choice(ms: dict, ram: float) -> tuple[dict, str]:
    llm_ms = [m for m in ms.values() if m.get("choice") == "llm"]
    key = load_settings().get("llm_model") or ""
    for m in llm_ms:
        if m["llm_key"] == key:
            return m, "在「參數設定 → 本機 LLM」選定"
    for k in ("e4b", "e2b", "26b"):  # 與 llm.current() 相同：沒指定時已下載的優先
        for m in llm_ms:
            if m["llm_key"] == k and is_ready(m):
                return m, "這台已經有這個模型"
    for m in llm_ms:
        lo, hi = m["ram_gb"]
        if lo <= ram < hi:
            return m, f"依記憶體 {ram:.0f} GB 建議"
    return llm_ms[0], "預設"


def compute() -> dict:
    ms = _models()
    rep = env_report()
    tier = rep.get("Tier") or "D"
    ram = _ram()
    feats, picked = chosen_features()
    st = load_settings()
    plan: dict[str, dict] = {}

    if "transcribe" in feats:
        rec = recommended()
        dev = (rec.get("device") or "auto").upper()
        why = "依效能實測" if rec.get("source") == "benchmark" else "還沒做效能實測，先用預設（到「本機效能」實測可找出最快配置）"
        if tier in ("A", "B"):
            why += "；NVIDIA 顯示卡要等 faster-whisper 引擎完成才用得到"
        main = f"{rec.get('model') or 'breeze'}-ov"
        plan["transcribe"] = {"model": main, "also": ["turbo-ov" if main == "breeze-ov" else "breeze-ov"],
                              "device": dev, "why": why}
    if "tidy" in feats:
        m, why = _llm_choice(ms, ram)
        plan["tidy"] = {"model": m["id"], "device": "AUTO", "why": why}
    if "translate" in feats:
        plan["translate"] = {"model": "translategemma", "device": (st.get("translate_device") or "auto").upper(), "why": ""}
    if "diarize" in feats:
        d = (st.get("diarize_device") or "").lower()
        dv = {"gpu": "OV-GPU", "cpu": "OV-CPU", "torch": "TORCH"}.get(d, "OV-CPU")
        plan["diarize"] = {"model": "pyannote", "device": dv,
                           "why": "依說話者辨識診斷" if d else "還沒做診斷，先用 OpenVINO＋CPU"}

    needed: list[str] = []
    for p in plan.values():
        for mid in [p["model"], *p.get("also", [])]:
            if mid not in needed:
                needed.append(mid)

    try:
        from .ovcompat import problem as ov_problem
    except Exception:  # 沒有 openvino 時不檢查
        ov_problem = None

    status: dict[str, dict] = {}
    for mid, m in ms.items():
        ready = is_ready(m)
        need = mid in needed
        if not ready and not need:
            continue
        item = {"label": m["label"], "path": str(model_path(m)), "gb": m.get("gb"),
                "state": ("ready" if ready else "missing") if need else "not_needed"}
        if need and not ready:
            item["how"] = m["how"]
            src = source_root()
            item["source"] = "copy" if src and is_ready(m, src) else ("download" if m["from"] == "hf" else "manual")
        if need and ready and m.get("openvino") and ov_problem:
            try:
                bad = ov_problem(model_path(m))
            except Exception:
                bad = ""
            if bad:
                item["state"], item["problem"] = "incompatible", bad
        status[mid] = item
    for p in plan.values():
        p["label"] = ms[p["model"]]["label"]
        p["device_label"] = DEV_LABEL.get(p["device"], p["device"])
        p["state"] = status.get(p["model"], {}).get("state", "missing")

    return {
        "computer": computer_name(), "alias": alias(), "display": display_name(),
        "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "app_version": version(),
        "tier": tier, "ram_gb": round(ram, 1), "models_root": str(models_root()),
        "source": str(source_root() or ""),
        "features": feats, "features_set": picked, "plan": plan, "models": status,
        "missing": [k for k, v in status.items() if v["state"] == "missing"],
        "unused": unused_folders(),
    }


def unused_folders() -> list[dict]:
    """模型位置裡，本專案清單沒有提到的資料夾（只告知，可能是其他專案在用）"""
    root = models_root()
    if not root.is_dir():
        return []
    known = [tuple(m["path"].split("/")) for m in manifest()["models"]]
    out = []

    def covered(parts: tuple) -> bool:
        return any(k[:len(parts)] == parts for k in known)

    def size(d: Path) -> float:
        try:
            return round(sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 2**30, 2)
        except Exception:
            return 0.0

    def walk(d: Path, parts: tuple, depth: int):
        for c in sorted(d.iterdir()):
            if not c.is_dir():
                continue
            p = parts + (c.name,)
            if c.name == ".hf-cache":
                out.append({"path": str(c), "gb": size(c), "kind": "cache",
                            "note": "Hugging Face 下載快取，模型都下載好後通常用不到"})
            elif not covered(p):
                out.append({"path": str(c), "gb": size(c), "kind": "unknown",
                            "note": "本專案用不到（可能是其他專案的模型，確認後再自行刪除）"})
            elif depth < 3 and p not in known:
                walk(c, p, depth + 1)
    try:
        walk(root, (), 1)
    except Exception:
        pass
    return out


def report_file() -> Path:
    d = ROOT / "env-reports"
    d.mkdir(exist_ok=True)
    return d / f"{computer_name()}.models.json"


def write(plan: dict | None = None) -> dict:
    plan = plan or compute()
    report_file().write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


# ------------------------------------------------------------------ 準備模型（複製或下載）
def _dir_bytes(d: Path) -> int:
    try:
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    except Exception:
        return 0


def fetch(mid: str, progress: Callable[[float, str], None] = lambda p, s: None) -> str:
    """準備一個模型：共用來源有就複製，否則可匿名下載的就從 Hugging Face 下載。回傳結果說明；做不到時拋出例外"""
    ms = _models()
    m = ms[mid]
    dst = model_path(m)
    if is_ready(m):
        return "已就緒"
    src = source_root()
    if src and is_ready(m, src):
        s = src.joinpath(*m["path"].split("/"))
        if m.get("openvino"):
            from .ovcompat import problem as ov_problem
            bad = ov_problem(s)
            if bad:
                raise RuntimeError(bad)
        total = _dir_bytes(s) or 1
        tmp = dst.with_name(dst.name + ".copying")  # 複製到暫存名，完成才改名，避免半套模型被當成就緒
        tmp.parent.mkdir(parents=True, exist_ok=True)
        done = threading.Event()

        def watch():
            while not done.wait(1.5):
                got = _dir_bytes(tmp)
                progress(min(got / total, 0.99), f"複製 {m['label']} {got / 2**30:.1f}／{total / 2**30:.1f} GB")
        threading.Thread(target=watch, daemon=True).start()
        try:
            shutil.copytree(s, tmp, dirs_exist_ok=True)
        finally:
            done.set()
        if dst.exists():  # 已有不完整的資料夾：不刪除，改名保留
            dst.rename(dst.with_name(f"{dst.name}.old-{time.strftime('%Y%m%d%H%M%S')}"))
        tmp.rename(dst)
        progress(1.0, f"{m['label']} 複製完成")
        return f"從共用來源 {src} 複製"
    if m["from"] == "hf":
        from huggingface_hub import snapshot_download
        total = int((m.get("gb") or 0) * 2**30)
        dst.mkdir(parents=True, exist_ok=True)
        done = threading.Event()

        def watch():
            while not done.wait(2):
                got = _dir_bytes(dst)
                progress(min(got / total, 0.99) if total else 0.0,
                         f"下載 {m['label']} {got / 2**30:.1f}" + (f"／{total / 2**30:.1f} GB" if total else " GB"))
        threading.Thread(target=watch, daemon=True).start()
        try:
            snapshot_download(m["repo"], local_dir=str(dst))  # 中斷後再執行會續傳
        finally:
            done.set()
        if "openvino_language_model.xml" in m.get("check", []) and not (dst / "openvino_language_model.xml").exists():
            raise RuntimeError("下載完成，但找不到 openvino_language_model.xml，模型格式可能已變更")
        if ".download_ok" in m.get("check", []):
            (dst / ".download_ok").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        if m.get("choice") == "llm" and not load_settings().get("llm_model"):
            save_settings({**load_settings(), "llm_model": m["llm_key"]})
        progress(1.0, f"{m['label']} 下載完成")
        return "從 Hugging Face 下載"
    raise RuntimeError(m["how"])


# 網頁用：背景準備缺少的模型（一次一個，依序處理）
prep = {"state": "idle", "model": "", "progress": 0.0, "msg": "", "done": [], "errors": {}}
_prep_lock = threading.Lock()


def start_prepare(ids: list[str] | None = None, log: Callable[[str], None] = print) -> dict:
    with _prep_lock:
        if prep["state"] == "running":
            return dict(prep)
        plan = compute()
        todo = [i for i in (ids or plan["missing"]) if plan["models"].get(i, {}).get("source") in ("copy", "download")]
        if not todo:
            return dict(prep)
        prep.update(state="running", model="", progress=0.0, msg="準備中…", done=[], errors={})

    def run():
        for mid in todo:
            prep.update(model=mid, progress=0.0)
            try:
                how = fetch(mid, lambda p, s: prep.update(progress=p, msg=s))
                prep["done"].append(mid)
                log(f"模型就緒：{mid}（{how}）")
            except Exception as e:
                prep["errors"][mid] = str(e)
                log(f"模型準備失敗：{mid}：{e}")
        prep.update(state="error" if prep["errors"] else "done", model="", progress=1.0,
                    msg="有模型沒有準備好" if prep["errors"] else "模型都準備好了")
        try:
            write()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
    return dict(prep)


# ------------------------------------------------------------------ 命令列（setup.cmd）
C = {"ok": "\033[32m", "warn": "\033[33m", "err": "\033[31m", "dim": "\033[90m", "hd": "\033[36m", "end": "\033[0m"}


def _p(text: str = "", kind: str = "") -> None:
    print(f"{C.get(kind, '')}{text}{C['end'] if kind else ''}", flush=True)


def _yes(q: str, default: bool = False) -> bool:
    try:
        a = input(f"{q} ({'Y/n' if default else 'y/N'}) ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def cli_configure(ask: bool) -> None:
    _p("--- 這台電腦的名稱與功能 ---", "hd")
    feats, picked = chosen_features()
    if picked and not ask:  # 已經選過：只顯示，不再詢問
        _p(f"電腦：{display_name()}｜功能：{'、'.join(manifest()['features'][f]['label'] for f in feats)}", "ok")
        _p("（要修改請到網頁「參數設定 → 這台電腦」）", "dim")
        return
    if ask or not alias():
        _p(f"電腦名稱是 {computer_name()}。可以取一個好記的別名（例：辦公室筆電、會議室桌機），直接按 Enter 略過。")
        try:
            a = input("別名：").strip()
        except EOFError:
            a = ""
        if a:
            save_choice(alias_text=a)
            _p(f"[OK] 別名：{alias()}", "ok")
    info = manifest()["features"]
    _p("要在這台電腦使用哪些功能？只會準備勾選功能需要的模型（之後可在網頁「參數設定」修改）。")
    _p(f"  轉逐字稿：必選", "dim")
    chosen = []
    for f in OPTIONAL:
        ms = [m for m in manifest()["models"] if m["feature"] == f]
        gb = max((m.get("gb") or 0) for m in ms)
        size = f"，模型約 {gb:g} GB" if gb else ""
        if _yes(f"  {info[f]['label']}（{info[f]['note']}{size}）？", default=f in feats):
            chosen.append(f)
    save_choice(features=chosen)
    _p(f"[OK] 功能：轉逐字稿{''.join('、' + info[f]['label'] for f in chosen)}", "ok")


def cli_prepare(yes: bool) -> int:
    plan = compute()
    miss = plan["missing"]
    if not miss:
        _p("[OK] 這台需要的模型都已就緒", "ok")
        return 0
    auto = [m for m in miss if plan["models"][m].get("source") in ("copy", "download")]
    manual = [m for m in miss if m not in auto]
    for m in manual:
        _p(f"[缺] {plan['models'][m]['label']}：{plan['models'][m]['how']}", "warn")
    if auto:
        gb = sum(plan["models"][m].get("gb") or 0 for m in auto)
        names = "、".join(f"{plan['models'][m]['label']}（{'複製' if plan['models'][m]['source'] == 'copy' else '下載'}）" for m in auto)
        _p(f"缺少：{names}" + (f"，共約 {gb:g} GB" if gb else ""))
        if yes or _yes("現在準備？（也可之後在網頁「參數設定 → 這台電腦」按「準備模型」）"):
            for m in auto:
                last = [0.0]

                def prog(p, s):
                    if p - last[0] >= 0.05 or p >= 1:
                        last[0] = p
                        print(f"\r  {s}（{p * 100:.0f}%）   ", end="", flush=True)
                try:
                    how = fetch(m, prog)
                    print()
                    _p(f"[OK] {plan['models'][m]['label']}：{how}", "ok")
                except Exception as e:
                    print()
                    _p(f"[注意] {plan['models'][m]['label']} 沒有準備好：{e}（再執行一次會續傳）", "warn")
        else:
            _p("略過。", "dim")
    return len(compute()["missing"])


def cli_report() -> int:
    plan = write()
    info = manifest()["features"]
    _p("")
    _p(f"--- 這台的模型配置方案：{plan['display']}（等級 {plan['tier']}，記憶體 {plan['ram_gb']:g} GB） ---", "hd")
    if not plan["features_set"]:
        _p("（還沒選功能，暫時依已有的模型判斷；下次執行 setup.cmd 或在網頁「參數設定」選擇）", "dim")
    sym = {"ready": ("[就緒]", "ok"), "missing": ("[缺少]", "warn"), "incompatible": ("[版本不符]", "err")}
    for f, p in plan["plan"].items():
        t, k = sym.get(p["state"], ("[?]", ""))
        _p(f"{t} {info[f]['label']}：{p['label']} × {p['device_label']}" + (f"　{p['why']}" if p["why"] else ""), k)
    for mid, it in plan["models"].items():
        if it["state"] == "incompatible":
            _p(f"  {it['problem']}", "err")
    nn = [it for it in plan["models"].values() if it["state"] == "not_needed"]
    if nn:
        _p("")
        _p("這台沒選用、但已經有的模型（保留不影響使用；需要空間時可自行刪除）：", "dim")
        for it in nn:
            _p(f"  {it['label']}  {it['path']}", "dim")
    if plan["unused"]:
        _p("")
        _p("模型位置裡本專案用不到的資料夾（本程式不會刪除，請確認沒有其他專案在用再自行刪除）：", "warn")
        for u in plan["unused"]:
            _p(f"  {u['path']}（{u['gb']:g} GB）：{u['note']}", "warn")
    _p(f"方案已存到 env-reports\\{report_file().name}", "dim")
    return len(plan["missing"])


def main(argv: list[str]) -> int:
    if os.name == "nt":
        os.system("")  # 讓 Windows 主控台顯示顏色
    cmd = argv[0] if argv else "report"
    if cmd == "configure":
        cli_configure("--ask" in argv)
        return 0
    if cmd == "prepare":
        return cli_prepare("--yes" in argv)
    return cli_report()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
