"""說話者辨識（0.8.0）：pyannote speaker-diarization-community-1（法國 pyannote，開源，CC-BY-4.0）。

- 模型：<MODELS_ROOT>\\diarization\\speaker-diarization-community-1（管理者下載，一般使用者不需 Hugging Face 帳號）
- 元件 pyannote.audio＋PyTorch 第一次使用時才自動安裝到專案 .venv（Windows 的 PyPI 版本是 CPU 版，約 1～2 GB）
- 只分析聲音：已完成的任務可直接補做，不必重新轉文字
- 結果存在 <檔名>.speakers.json；SRT／TXT 依說話者重新產生，改名後也會重新產生
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import re
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import ROOT, audio, textout
from .config import load_settings, models_root, ov_cache_dir

MODEL_NAME = "speaker-diarization-community-1"
PACKAGES = ["pyannote.audio>=4.0"]
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_pipe = None
_device = "CPU"  # 計算聲紋實際使用的裝置（載入模型時決定）


class DiarizeError(Exception):
    pass


class DiarizeCancelled(Exception):
    pass


def model_dir() -> Path:
    return models_root() / "diarization" / MODEL_NAME


def model_ready() -> bool:
    return (model_dir() / "config.yaml").exists()


def _has(mod: str) -> bool:
    """模組是否已安裝。find_spec("a.b") 在上層套件 a 不存在時會直接拋出錯誤，所以要逐層檢查並攔截"""
    try:
        parts = mod.split(".")
        return all(importlib.util.find_spec(".".join(parts[:i + 1])) is not None for i in range(len(parts)))
    except Exception:
        return False


def installed() -> bool:
    importlib.invalidate_caches()
    return _has("torch") and _has("pyannote.audio")


def status() -> dict:
    return {"installed": installed(), "model": model_ready(), "model_dir": str(model_dir())}


# ------------------------------------------------------------------ 第一次使用時安裝
def install(log: Callable[[str], None] = print, progress: Callable[[float, str], None] | None = None,
            cancelled: Callable[[], bool] = lambda: False) -> None:
    """安裝 pyannote.audio（連同 PyTorch CPU 版）到專案 .venv。
    先固定已載入的 numpy 版本，避免執行中的程式被換掉；裝不起來才放寬並提示重新啟動。"""
    if installed():
        return
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        raise DiarizeError("找不到專案的 .venv，請先執行 setup.cmd")
    import numpy
    np_ver = numpy.__version__
    cons = Path(sys.prefix) / "diarize-constraints.txt"
    cons.write_text(f"numpy=={np_ver}\n", encoding="utf-8")
    log("第一次使用說話者辨識：自動安裝 pyannote.audio 與 PyTorch（CPU 版，約 1～2 GB，需要幾分鐘）")

    def pip(extra: list[str]) -> int:
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off",
               *extra, *PACKAGES]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                encoding="utf-8", errors="replace", creationflags=_NOWIN)
        n = 0
        for line in proc.stdout:
            line = line.strip()
            if cancelled():
                proc.kill()
                raise DiarizeCancelled()
            if line.startswith("Collecting"):
                n += 1
                progress and progress(min(0.01 + n * 0.001, 0.05), "安裝說話者辨識元件：確認需要的套件（第一次使用）")
            elif line.startswith("Downloading"):
                n += 1
                parts = line.split()
                name = parts[1].split("-")[0] if len(parts) > 1 else ""
                m = re.search(r"\(([^)]+)\)", line)
                size = f"（{m.group(1)}）" if m else ""
                if "metadata" not in line:
                    progress and progress(min(0.05 + n * 0.001, 0.1), f"安裝說話者辨識元件：下載 {name}{size}")
                    log("  " + line[:160])
            elif line.startswith("Installing"):
                progress and progress(0.1, "安裝說話者辨識元件：寫入檔案（第一次使用，約 5～15 分鐘，請勿關閉 start.cmd 視窗）")
                log("  寫入檔案中（PyTorch 檔案很多，Windows 防毒掃描時會比較久）")
            elif line.startswith("Successfully installed"):
                log("  安裝完成")
        return proc.wait()

    t0 = time.time()
    code = pip(["-c", str(cons)])
    restart = False
    if code != 0:
        log("  固定 numpy 版本安裝失敗，改為讓 pip 自行決定版本")
        code = pip([])
        restart = True
    if code != 0:
        raise DiarizeError("說話者辨識元件安裝失敗，請確認網路連線後再試一次（公司網路可能封鎖 PyPI）。")
    log(f"說話者辨識元件安裝完成（{(time.time() - t0) / 60:.1f} 分鐘）")
    if restart:
        import importlib.metadata as md
        if md.version("numpy") != np_ver:
            raise DiarizeError("說話者辨識元件已安裝完成，但需要重新啟動程式：請關閉 start.cmd 視窗再重新開啟，"
                               "然後在任務頁按「分辨說話者」。")


# ------------------------------------------------------------------ 辨識
def _load():
    global _pipe
    if _pipe is None:
        if not model_ready():
            raise DiarizeError(f"找不到說話者辨識模型：{model_dir()}。請向管理者索取 D:\\models\\diarization 資料夾。")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 只用本機模型，不連 Hugging Face
        import warnings
        warnings.filterwarnings("ignore", module="pyannote")
        warnings.filterwarnings("ignore", message=".*torchcodec.*")
        from pyannote.audio import Pipeline
        _pipe = Pipeline.from_pretrained(str(model_dir()))
        if _pipe is None:
            raise DiarizeError("說話者辨識模型載入失敗（模型檔案可能不完整）。")
        global _device
        _device = _accelerate(_pipe)
    return _pipe


# ------------------------------------------------------------------ Intel 顯示卡加速（OpenVINO）
class _Accel:
    """「計算聲紋」的執行方式：torch（PyTorch CPU）／cpu（OpenVINO CPU）／gpu（OpenVINO Intel 顯示卡）。
    只替換 WeSpeaker ResNet 最耗時的卷積部分（forward_frames，約佔 95% 時間），統計彙整維持 PyTorch。
    OpenVINO 模型轉換一次後存在 .cache\\diarize-ov\\；顯示卡固定輸入大小（動態大小在 Intel 顯示卡上很慢），
    不足一批時補齊再丟掉。轉換後先與 PyTorch 比對（相似度 ≥ 0.995）才採用；執行出錯自動退回 PyTorch。"""

    BATCH = 32  # 與模型設定 embedding_batch_size 相同

    def __init__(self, pipe):
        self.resnet = pipe._embedding.model_.resnet
        self.orig = self.resnet.forward
        self.mode = "torch"
        self._core = self._xml = None
        self._compiled: dict = {}

    def _prepare(self):
        import openvino as ov
        import torch
        if self._xml:
            return
        self._core = ov.Core()
        resnet = self.resnet

        class _Frames(torch.nn.Module):
            def __init__(self, r):
                super().__init__()
                self.r = r

            def forward(self, fbank):
                return self.r.forward_frames(fbank)

        wfile = model_dir() / "embedding" / "pytorch_model.bin"
        key = f"{wfile.stat().st_size}_{int(wfile.stat().st_mtime)}" if wfile.exists() else "x"
        cache = ROOT / ".cache" / "diarize-ov"
        cache.mkdir(parents=True, exist_ok=True)
        xml = cache / f"wespeaker_frames_{key}.xml"
        if not xml.exists():
            with torch.inference_mode():
                ovm = ov.convert_model(_Frames(resnet).eval(), example_input=torch.randn(2, 998, 80))
            ovm.reshape({0: ov.PartialShape([-1, -1, 80])})
            ov.save_model(ovm, str(xml), compress_to_fp16=False)
        self._xml = xml

    def devices(self) -> list[str]:
        try:
            import openvino as ov
            return list(ov.Core().available_devices)
        except Exception:
            return []

    def _compiled_for(self, dev: str, shape: tuple | None):
        k = (dev, shape)
        if k not in self._compiled:
            m = self._core.read_model(str(self._xml))
            if shape:
                m.reshape({0: list(shape)})
            from .config import ov_cache_dir as _c
            self._compiled[k] = self._core.compile_model(m, dev, {"CACHE_DIR": str(_c())})
        return self._compiled[k]

    def _run(self, dev: str, fbank):
        import numpy as np
        import torch
        x = fbank.detach().to("cpu", torch.float32).contiguous().numpy()
        n = x.shape[0]
        if dev == "GPU":  # 固定大小：批次補到 32
            if n < self.BATCH:
                x = np.concatenate([x, np.zeros((self.BATCH - n, *x.shape[1:]), np.float32)])
            c = self._compiled_for("GPU", tuple(x.shape))
        else:
            c = self._compiled_for("CPU", None)
        y = np.asarray(c(x)[c.output(0)])[:n]
        return torch.from_numpy(y.copy())

    def use(self, mode: str) -> str:
        """切換執行方式；無法使用時退回 torch。回傳實際採用的方式"""
        import torch
        self.resnet.forward = self.orig
        self.mode = "torch"
        if mode == "torch":
            return "torch"
        dev = "GPU" if mode == "gpu" else "CPU"
        try:
            if dev not in self.devices():
                return "torch"
            self._prepare()
            x = torch.randn(4, 998, 80)
            with torch.inference_mode():
                ref = self.resnet.forward_frames(x.clone()).flatten(1)
            got = self._run(dev, x).flatten(1)
            cos = float(torch.nn.functional.cosine_similarity(ref, got).min())
            if cos < 0.995:
                print(f"[說話者辨識] OpenVINO {dev} 結果與 PyTorch 不一致（相似度 {cos:.4f}），維持 PyTorch")
                return "torch"
        except Exception as e:
            print(f"[說話者辨識] 無法使用 OpenVINO {dev}，維持 PyTorch：{e}")
            return "torch"
        state = {"ok": True}
        resnet, orig = self.resnet, self.orig

        def fwd(fbank, weights=None):
            if state["ok"]:
                try:
                    return resnet.forward_embedding(self._run(dev, fbank), weights=weights)
                except Exception as e:  # 執行中出錯：之後改回 PyTorch，不中斷任務
                    state["ok"] = False
                    print(f"[說話者辨識] OpenVINO {dev} 執行失敗，改用 PyTorch：{e}")
            return orig(fbank, weights=weights)

        resnet.forward = fwd
        self.mode = mode
        return mode


_accel: _Accel | None = None
LABEL = {"torch": "CPU（PyTorch）", "cpu": "CPU（OpenVINO）", "gpu": "Intel 顯示卡（OpenVINO）"}


def _accelerate(pipe) -> str:
    """依設定 diarize_device（auto／gpu／cpu／torch）選擇；auto＝用診斷測出的最快方式，沒測過時用 OpenVINO CPU"""
    global _accel
    _accel = _Accel(pipe)
    pref = (load_settings().get("diarize_device") or "auto").lower()
    if pref == "auto":
        pref = "cpu"  # 測試環境 OpenVINO CPU 比 PyTorch 快約 7.5 倍；顯示卡要經診斷確認較快才用
    got = _accel.use(pref)
    if got != pref and pref == "gpu":
        got = _accel.use("cpu")
    return LABEL[got]


# pyannote 各步驟在進度條上的區間與說明
_STEPS = {"segmentation": (0.0, 0.25, "分析聲音片段"), "speaker_counting": (0.25, 0.3, "估計說話人數"),
          "embeddings": (0.3, 0.92, "計算聲紋"), "discrete_diarization": (0.92, 1.0, "歸類說話者")}


def diarize(src: Path, num_speakers: int | None = None, progress: Callable[[float, str], None] | None = None,
            cancelled: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print) -> list[list]:
    """回傳說話時段 [[開始秒, 結束秒, 'SPEAKER_00'], …]（不重疊版本）"""
    import torch
    t0 = time.time()
    progress and progress(0.0, "分辨說話者：載入模型")
    pipe = _load()
    threads = apply_threads()
    wav = audio.load_16k(src)  # 直接從原始檔解碼，與辨識相同
    where = f"聲紋計算用 {_device}，CPU {threads} 執行緒"
    log(f"分辨說話者（{'自動判斷人數' if not num_speakers else f'{num_speakers} 人'}，{where}）")
    marks: list[tuple[str, float]] = []  # 各步驟開始時間，用來記錄耗時

    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        if cancelled():
            raise DiarizeCancelled()
        if not marks or marks[-1][0] != step_name:
            marks.append((step_name, time.time()))
        lo, hi, label = _STEPS.get(step_name, (0.0, 1.0, "處理中"))
        f = lo + (hi - lo) * (completed / total if total and completed is not None else 1.0)
        detail = f" {completed}／{total}" if total and completed is not None and step_name == "embeddings" else ""
        progress and progress(f, f"分辨說話者：{label}{detail}（{f * 100:.0f}%）")

    kw = {"num_speakers": num_speakers} if num_speakers else {}
    out = pipe({"waveform": torch.from_numpy(wav.copy()).unsqueeze(0), "sample_rate": audio.SR}, hook=hook, **kw)
    ann = getattr(out, "exclusive_speaker_diarization", None) or getattr(out, "speaker_diarization", None) or out
    turns = [[round(t.start, 2), round(t.end, 2), str(lab)] for t, _, lab in ann.itertracks(yield_label=True)]
    end = time.time()
    steps = [f"{_STEPS.get(n, (0, 0, n))[2]} {((marks[i + 1][1] if i + 1 < len(marks) else end) - t):.0f} 秒"
             for i, (n, t) in enumerate(marks)]
    log(f"說話者辨識完成：{len({x[2] for x in turns})} 位說話者（{end - t0:.0f} 秒；{'、'.join(steps)}）")
    return turns


# ------------------------------------------------------------------ 診斷：比較執行方式（結果存成設定）
def bench_devices(src: Path, seconds: int = 60, log: Callable[[str], None] = print) -> list[dict]:
    """取前 seconds 秒，分別用 PyTorch CPU、OpenVINO CPU、OpenVINO 顯示卡計算，回傳 [{mode, sec, rtf}]"""
    import torch
    global _device
    pipe = _load()
    wav = audio.load_16k(src, 0, seconds)
    dur = len(wav) / audio.SR
    data = {"waveform": torch.from_numpy(wav.copy()).unsqueeze(0), "sample_rate": audio.SR}
    log(f"診斷：取前 {dur:.0f} 秒比較聲紋計算方式（OpenVINO 裝置：{'、'.join(_accel.devices()) or '無'}）")
    out = []
    for mode in ("gpu", "cpu", "torch"):
        got = _accel.use(mode)
        if got != mode:
            log(f"  {LABEL[mode]}：無法使用")
            continue
        pipe({"waveform": data["waveform"][:, :audio.SR * 12], "sample_rate": audio.SR})  # 暖機（含顯示卡編譯）
        t0 = time.time()
        pipe(data)
        sec = time.time() - t0
        out.append({"mode": mode, "sec": round(sec, 1), "rtf": round(sec / dur, 3)})
        log(f"  {LABEL[mode]}：{sec:.1f} 秒（錄音長度的 {sec / dur:.2f} 倍）")
    best = min(out, key=lambda x: x["sec"])
    _device = LABEL[_accel.use(best["mode"])]
    log(f"最快：{LABEL[best['mode']]}（{best['sec']} 秒），已採用")
    return out


# ------------------------------------------------------------------ CPU 執行緒（混合架構 CPU 用太多執行緒反而變慢）
def apply_threads() -> int:
    """依設定（data\\settings.json 的 diarize_threads）設定 PyTorch 執行緒數；沒設定用 PyTorch 預設"""
    import torch
    n = load_settings().get("diarize_threads")
    if isinstance(n, int) and n > 0:
        torch.set_num_threads(n)
    return torch.get_num_threads()


def bench_threads(src: Path, seconds: int = 90, log: Callable[[str], None] = print,
                  cancelled: Callable[[], bool] = lambda: False) -> list[dict]:
    """取前 seconds 秒，用不同執行緒數各跑一次，回傳 [{threads, sec, rtf}]（診斷用）"""
    import torch
    pipe = _load()
    wav = audio.load_16k(src, 0, seconds)
    dur = len(wav) / audio.SR
    data = {"waveform": torch.from_numpy(wav.copy()).unsqueeze(0), "sample_rate": audio.SR}
    default = torch.get_num_threads()
    cpu = os.cpu_count() or default
    cands = sorted({default, cpu, max(1, cpu * 3 // 4), max(1, cpu // 2), max(1, cpu // 4), 4}, reverse=True)
    log(f"診斷：取前 {dur:.0f} 秒，測試執行緒數 {cands}（PyTorch 預設 {default}，邏輯處理器 {cpu}）")
    torch.set_num_threads(default)
    pipe({"waveform": data["waveform"][:, :audio.SR * 15], "sample_rate": audio.SR})  # 暖機
    out = []
    for n in cands:
        if cancelled():
            raise DiarizeCancelled()
        torch.set_num_threads(n)
        t0 = time.time()
        pipe(data)
        sec = time.time() - t0
        out.append({"threads": n, "sec": round(sec, 1), "rtf": round(sec / dur, 3)})
        log(f"  {n:>2} 執行緒：{sec:.1f} 秒（錄音長度的 {sec / dur:.2f} 倍）")
    torch.set_num_threads(default)
    best = min(out, key=lambda x: x["sec"])
    log(f"最快：{best['threads']} 執行緒（{best['sec']} 秒）")
    return out


def assign(segs: list[dict], turns: list[list]) -> tuple[list[int], list[str]]:
    """每一句對應到重疊時間最長的說話者；回傳 (每句說話者編號, 編號對應的原始標籤)，編號依出場順序"""
    order: list[str] = []
    out: list[int] = []
    for s in segs:
        a, b = s["start"], max(s["end"], s["start"] + 0.1)
        best, lab = 0.0, None
        for ta, tb, tl in turns:
            if tb <= a or ta >= b:
                continue
            ov = min(b, tb) - max(a, ta)
            if ov > best:
                best, lab = ov, tl
        if lab is None:  # 沒有重疊：取 2 秒內最近的時段，否則沿用上一句
            near = min(turns, key=lambda t: min(abs(t[0] - b), abs(t[1] - a)), default=None)
            if near and min(abs(near[0] - b), abs(near[1] - a)) <= 2:
                lab = near[2]
        if lab is None:
            out.append(out[-1] if out else -1)
            continue
        if lab not in order:
            order.append(lab)
        out.append(order.index(lab))
    if out and out[0] == -1 and order:  # 開頭沒對到的句子歸給第一位
        out = [0 if x == -1 else x for x in out]
    return out, order


# ------------------------------------------------------------------ 結果與輸出
def speakers_file(base: Path) -> Path:
    return textout.with_ext(base, ".speakers.json")


def load(base: Path) -> dict | None:
    try:
        return json.loads(speakers_file(base).read_text(encoding="utf-8"))
    except Exception:
        return None


def save_and_write(base: Path, segs: list[dict], turns: list[list], num_speakers: int | None, seconds: float,
                   names: list[str] | None = None) -> dict:
    seg_spk, labels = assign(segs, turns)
    old = load(base) or {}
    names = names or old.get("names") or []
    names = [names[i] if i < len(names) and names[i] else f"說話者 {i + 1}" for i in range(len(labels))]
    data = {"version": 1, "model": MODEL_NAME, "requested": num_speakers, "count": len(labels), "labels": labels,
            "names": names, "seg": seg_spk, "turns": turns, "seconds": round(seconds, 1),
            "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    speakers_file(base).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_files(base, segs, data)
    return data


def rename(base: Path, segs: list[dict], names: list[str]) -> dict:
    data = load(base)
    if not data:
        raise DiarizeError("這個任務還沒有分辨說話者")
    clean = [(n or "").strip()[:30] for n in names]
    data["names"] = [clean[i] if i < len(clean) and clean[i] else f"說話者 {i + 1}" for i in range(data["count"])]
    speakers_file(base).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_files(base, segs, data)
    return data


def write_files(base: Path, segs: list[dict], data: dict) -> None:
    """SRT 每句前加 [姓名]；TXT 同一人連續的話歸成一段（段首標姓名與時間，每句一行，段落間空一行）"""
    names, seg = data["names"], data["seg"]
    who = lambda i: names[seg[i]] if i < len(seg) and 0 <= seg[i] < len(names) else ""
    srt = "\n".join(f"{i}\n{textout._ts(s['start'])} --> {textout._ts(max(s['end'], s['start'] + 0.3))}\n"
                    f"{('[' + who(i - 1) + '] ') if who(i - 1) else ''}{s['text']}\n"
                    for i, s in enumerate(segs, 1))
    paras, cur, buf, t0 = [], None, [], 0.0
    for i, s in enumerate(segs):
        w = who(i)
        if w != cur and buf:
            paras.append(f"{cur}（{textout.fmt_dur(t0)}）\n" + "\n".join(buf) if cur else "\n".join(buf))
            buf = []
        if not buf:
            cur, t0 = w, s["start"]
        buf.append(s["text"])
    if buf:
        paras.append(f"{cur}（{textout.fmt_dur(t0)}）\n" + "\n".join(buf) if cur else "\n".join(buf))
    textout.with_ext(base, ".srt").write_text(srt, encoding="utf-8-sig")
    if not textout.tidied(base):  # 已整理過：.txt 是整理稿，不蓋回
        textout.with_ext(base, ".txt").write_text("\n\n".join(paras) + "\n", encoding="utf-8-sig")
