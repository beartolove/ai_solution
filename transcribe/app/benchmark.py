"""本機效能實測（每台電腦的健檢）：找出這台的最佳配置、適合的檔案格式與長檔切段長度。

第 1 部分　快速健檢（必跑，約 5–10 分鐘），用 3 分鐘標準樣本：
  1. 檔案格式：mp3／m4a／wav／mp4（影片）的轉檔與解碼速度，確認都能處理
  2. 辨識配置：Breeze-ASR-25、large-v3-turbo × 顯示卡、CPU 的載入時間與速度（RTF）
第 2 部分　長檔測試（詢問後才跑，約 6–10 分鐘），取最長檔案的前 30 分鐘、用最佳配置：
  3. 不切段 vs 每 10 分鐘切段：耗時、記憶體高峰、字數差異、重複句、切點空白 → 建議切段方式
     （記憶體不足或切段明顯較好 → 每 10 分鐘切；否則 1 小時內不切、超過 1 小時每 30 分鐘切）

結果：env-reports\\<電腦名稱>.benchmark.json（轉逐字稿時自動採用）與 .benchmark.md（給人看）。
從網頁「本機效能」頁執行（0.6.8 起不再提供 benchmark.cmd）。
標準樣本：samples\\benchmark_sample.mp3；沒有時從 samples\\ 裡最短的測試錄音自動擷取（母機建立一次，隨專案發佈）。
長檔測試：取 samples\\ 裡最長（20 分鐘以上）的測試錄音。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import ROOT, audio, chunked, engine_ov, textout, version
from .config import MODELS, benchmark_dir, benchmark_file, env_report, find_ffmpeg, ov_model_dir

SAMPLE_SEC = 180
LONG_SEC = 1800
LONG_MODES = [0, 10]  # 0＝不切段；用 10 分鐘切段量測「每個切點的額外成本」與切點品質
LOW_RAM_GB = 12       # 記憶體低於此值的電腦一律切段，避免長檔把記憶體吃光
SAMPLE = ROOT / "samples" / "benchmark_sample.mp3"
EXTS = audio.AUDIO_EXT | audio.VIDEO_EXT
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------ 小工具
def _ff(args: list[str]) -> None:
    r = subprocess.run([find_ffmpeg(), "-v", "error", "-y", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, creationflags=_NOWIN)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[-300:])


class PeakMem:
    """在背景每 0.5 秒記錄本程式的記憶體用量，取最高值（需要 psutil；沒有就略過）"""

    def __init__(self):
        try:
            import psutil
            self.proc = psutil.Process(os.getpid())
        except Exception:
            self.proc = None
        self.peak = 0
        self._stop = threading.Event()

    def __enter__(self):
        if self.proc:
            self.peak = self.proc.memory_info().rss
            threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                self.peak = max(self.peak, self.proc.memory_info().rss)
            except Exception:
                pass
            time.sleep(0.5)

    def __exit__(self, *a):
        self._stop.set()

    @property
    def mb(self):
        return round(self.peak / 1e6) if self.proc else None


def _media_files() -> dict[Path, float]:
    out = {}
    for p in (ROOT / "samples").glob("*"):  # 測試錄音（0.6.8 起由 files\\ 併入 samples\\）
        if p.suffix.lower() in EXTS and not p.name.startswith("benchmark_"):  # 排除擷取出來的標準樣本
            try:
                out[p] = audio.probe(p)["duration"]
            except Exception:
                pass
    return out


# ------------------------------------------------------------------ 第 1 部分
def ensure_sample(user_file: Path | None) -> Path:
    """準備 3 分鐘標準樣本（16kHz 單聲道 mp3）"""
    if not user_file and SAMPLE.exists():
        return SAMPLE
    if user_file:
        src = user_file
    else:
        media = _media_files()
        if not media:
            raise FileNotFoundError("找不到測試樣本：請在專案的 samples 資料夾放一段有人說話的錄音（3 分鐘以上），再重新實測。")
        src = min(media, key=media.get)
    dur = audio.probe(src)["duration"]
    start = 60 if dur > SAMPLE_SEC + 120 else 0  # 長檔跳過開頭 1 分鐘（常是片頭音樂）
    out = SAMPLE if not user_file else ROOT / "samples" / f"benchmark_{user_file.stem[:40]}.mp3"
    out.parent.mkdir(exist_ok=True)
    _ff(["-ss", str(start), "-t", str(SAMPLE_SEC), "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "64k", str(out)])
    print(f"已建立標準樣本：{out.name}（取自 {src.name}，第 {start} 秒起 {SAMPLE_SEC // 60} 分鐘）")
    if not user_file:
        print("  ※ 這個樣本會隨專案發佈給其他電腦；若內容不宜外流，請換成公開內容的 3 分鐘錄音。")
    return out


def test_formats(sample: Path, work: Path) -> list[dict]:
    """把樣本轉成各格式，量測「轉 MP3」與「解碼 16kHz」的時間，換算成每小時音檔需要的秒數"""
    variants = {
        "mp3": ["-i", str(sample), "-c:a", "libmp3lame", "-b:a", "128k"],
        "m4a": ["-i", str(sample), "-c:a", "aac", "-b:a", "128k"],
        "wav": ["-i", str(sample), "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2"],
        "mp4": ["-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=30:d={SAMPLE_SEC}", "-i", str(sample),
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "128k"],
    }
    rows = []
    dur = audio.probe(sample)["duration"] or SAMPLE_SEC
    for ext, args in variants.items():
        f = work / f"fmt_test.{ext}"
        row = {"format": ext}
        try:
            _ff([*args, str(f)])
            t0 = time.perf_counter()
            if ext != "mp3":
                audio.to_mp3(f, work / f"fmt_out_{ext}.mp3")
            t_mp3 = time.perf_counter() - t0
            t0 = time.perf_counter()
            pcm = audio.load_16k(f)
            t_dec = time.perf_counter() - t0
            row.update(ok=len(pcm) > 0, size_mb=round(f.stat().st_size / 1e6, 1),
                       mp3_sec_per_hour=round(t_mp3 * 3600 / dur, 1),
                       decode_sec_per_hour=round(t_dec * 3600 / dur, 1))
        except Exception as e:
            row.update(ok=False, error=str(e)[:200])
        rows.append(row)
        print(f"  {ext:<4} {'OK' if row.get('ok') else '失敗'}｜轉 MP3 {row.get('mp3_sec_per_hour', '-')} 秒／每小時音檔"
              f"｜解碼 {row.get('decode_sec_per_hour', '-')} 秒／每小時音檔")
    return rows


def test_engines(sample: Path, work: Path) -> list[dict]:
    pcm = audio.load_16k(sample)
    dur = len(pcm) / audio.SR
    devs = engine_ov.devices()
    targets = (["GPU"] if any(d.startswith("GPU") for d in devs) else []) + ["CPU"]
    print(f"  可用裝置：{'、'.join(devs) or '（無法偵測，僅測 CPU）'}")
    rows = []
    for model in MODELS:
        for dev in targets:
            row = {"model": model, "device": dev}
            print(f"\n  ▶ {MODELS[model][1]} × {dev}")
            try:
                with PeakMem() as pm:
                    pipe, used, t_load = engine_ov.load(ov_model_dir(model), dev, lambda m: print("    " + m))
                    t0 = time.perf_counter()
                    segs = engine_ov.transcribe(pipe, pcm)
                    t_asr = time.perf_counter() - t0
                segs = textout.postprocess(segs)
                text = "".join(t for _, _, t in segs)
                (work / f"text_{model}_{dev}.txt").write_text("\n".join(t for _, _, t in segs), encoding="utf-8-sig")
                rtf = t_asr / dur if dur else 0
                row.update(ok=True, load_sec=round(t_load, 1), asr_sec=round(t_asr, 1), rtf=round(rtf, 3),
                           min_per_hour=round(rtf * 60, 1), peak_mb=pm.mb, chars=len(text), preview=text[:50])
                print(f"    載入 {t_load:.0f} 秒｜辨識 {t_asr:.0f} 秒｜RTF {rtf:.3f}（1 小時音檔約 {rtf * 60:.0f} 分鐘）"
                      + (f"｜記憶體高峰 {pm.mb} MB" if pm.mb else ""))
            except Exception as e:
                row.update(ok=False, error=str(e)[:200])
                print(f"    [失敗] {e}")
            finally:
                engine_ov.unload()
            rows.append(row)
    return rows


def recommend(engines: list[dict]) -> dict:
    ok = [r for r in engines if r.get("ok")]
    if not ok:
        return {}
    breeze = sorted((r for r in ok if r["model"] == "breeze"), key=lambda r: r["rtf"])
    turbo = sorted((r for r in ok if r["model"] == "turbo"), key=lambda r: r["rtf"])
    best = breeze[0] if breeze and breeze[0]["rtf"] <= 1.0 else (turbo[0] if turbo else breeze[0])
    why = ("Breeze-ASR-25 對臺灣華語與中英夾雜最準，且這台跑得夠快" if best["model"] == "breeze"
           else "Breeze-ASR-25 在這台比即時還慢，改用 large-v3-turbo")
    rec = {"model": best["model"], "device": best["device"], "rtf": best["rtf"],
           "min_per_hour": best["min_per_hour"], "reason": why}
    if best["model"] == "breeze" and turbo and turbo[0]["rtf"] < best["rtf"] * 0.6:
        rec["draft"] = {"model": "turbo", "device": turbo[0]["device"], "min_per_hour": turbo[0]["min_per_hour"]}
    return rec


# ------------------------------------------------------------------ 第 2 部分
def test_long(src: Path, model: str, device: str, work: Path) -> dict:
    """取長檔前 30 分鐘，比較不切段與每 5／10 分鐘切段"""
    dur = audio.probe(src)["duration"]
    test_sec = min(dur, LONG_SEC)
    clip = work / "long_clip.m4a"  # 先剪出測試片段，讓「不切段」與「切段」處理相同內容
    _ff(["-t", str(test_sec), "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "96k", str(clip)])
    print(f"  測試片段：{src.name} 前 {textout.fmt_dur(test_sec)}｜配置：{model} × {device}")
    pipe, used, _ = engine_ov.load(ov_model_dir(model), device, lambda m: print("    " + m))
    modes = []
    for cm in LONG_MODES:
        label = "不切段" if cm == 0 else f"每 {cm} 分鐘切段"
        print(f"\n  ▶ {label}")
        row = {"chunk_min": cm, "label": label}
        try:
            last = [-1]

            def prog(f: float, _m: str):
                if int(f * 10) != last[0]:
                    last[0] = int(f * 10)
                    print(f"    {int(f * 100)}%")

            with PeakMem() as pm:
                t0 = time.perf_counter()
                if cm == 0:
                    segs, cuts = chunked.transcribe_whole(clip, pipe, progress=prog), []
                else:
                    segs, cuts = chunked.transcribe_chunked(clip, pipe, test_sec, cm, progress=prog)
                sec = time.perf_counter() - t0
            st = chunked.quality_stats(segs, cuts)
            (work / f"long_{cm or 'whole'}.txt").write_text(
                "\n".join(f"[{textout.fmt_dur(a)}] {t}" for a, _, t in textout.postprocess(segs)), encoding="utf-8-sig")
            row.update(ok=True, sec=round(sec, 1), rtf=round(sec / test_sec, 3), peak_mb=pm.mb, cuts=len(cuts), **st)
            print(f"    耗時 {sec:.0f} 秒｜RTF {sec / test_sec:.3f}｜{st['segments']} 句｜重複句 {st['repeats']}"
                  f"｜切點最大空白 {st['max_gap_at_cut']} 秒" + (f"｜記憶體高峰 {pm.mb} MB" if pm.mb else ""))
        except Exception as e:
            row.update(ok=False, error=str(e)[:200])
            print(f"    [失敗] {e}")
        modes.append(row)
    engine_ov.unload()

    ok = {m["chunk_min"]: m for m in modes if m.get("ok")}
    whole, chunk = ok.get(0), ok.get(10)
    for m in modes:  # 與「不切段」比較字數差異，檢查切點有沒有漏字或多字
        if m.get("ok") and whole and whole["chars"]:
            m["chars_diff_pct"] = round((m["chars"] - whole["chars"]) / whole["chars"] * 100, 1)

    ram = float(env_report().get("RamGB") or 0)
    chunk_better = bool(whole and chunk and (
        chunk["repeats"] + 2 <= whole["repeats"] or (whole["max_gap"] > 30 and chunk["max_gap"] <= 10)))
    per_cut = None
    if whole and chunk and chunk.get("cuts"):
        per_cut = round(max(chunk["sec"] - whole["sec"], 0) / chunk["cuts"], 1)

    if not whole or (ram and ram < LOW_RAM_GB) or chunk_better:
        # 記憶體不足、不切段失敗、或切段明顯減少錯誤 → 每 10 分鐘切段
        chunk_min, over_min = 10, 15
        why = ("不切段測試失敗" if not whole else f"記憶體只有 {ram:g} GB" if ram and ram < LOW_RAM_GB
               else "切段明顯減少重複句或空白")
        note = f"{why}，長檔每 10 分鐘切一段"
    else:
        # 效能與品質差不多 → 1 小時內不切段（最快）；超過 1 小時每 30 分鐘切段，保留中斷續跑
        chunk_min, over_min = 30, 60
        note = "1 小時內不切段（最快）；超過 1 小時每 30 分鐘切一段，中斷可續跑"
        if per_cut is not None and whole["sec"]:
            # 2 小時錄音切成 4 段＝3 個切點的額外時間占比
            extra = per_cut * 3 / (whole["rtf"] * 7200) * 100 if whole["rtf"] else 0
            note += (f"（每個切點約多 {per_cut:g} 秒，2 小時錄音約多 {extra:.0f}%）" if extra >= 1
                     else "（實測切段幾乎不增加時間）")
    return {"source": src.name, "test_sec": round(test_sec), "model": model, "device": device, "modes": modes,
            "chunk_min": chunk_min, "chunk_over_min": over_min, "per_cut_sec": per_cut,
            "rtf_long": whole["rtf"] if whole else (chunk["rtf"] if chunk else None), "note": note}


# ------------------------------------------------------------------ 報告

def rating(rtf: float) -> str:
    """把 RTF 換成白話評等"""
    if rtf < 0.1:
        return "非常快（1 小時錄音不到 6 分鐘）"
    if rtf < 0.3:
        return "快（1 小時錄音約 6–18 分鐘）"
    if rtf < 0.7:
        return "普通（1 小時錄音約 18–42 分鐘，可邊做別的事）"
    if rtf < 1.0:
        return "偏慢（接近即時，長檔建議下班或午休時跑）"
    return "比即時還慢（建議改用其他電腦處理長檔）"


def estimate_minutes(rec: dict, engines: list[dict], formats: list[dict], hours: float, fmt: str,
                     rtf: float | None = None) -> float | None:
    """預估從拖進檔案到拿到逐字稿的總時間（分鐘）＝轉 MP3＋解碼＋載入模型＋辨識。
    rtf 有給（長檔實測值）就用它，比 3 分鐘樣本換算的更貼近實際。"""
    e = next((x for x in engines if x.get("ok") and x["model"] == rec.get("model") and x["device"] == rec.get("device")), None)
    f = next((x for x in formats if x.get("ok") and x["format"] == fmt), None)
    if not e or not f:
        return None
    pre = (f["mp3_sec_per_hour"] + f["decode_sec_per_hour"]) * hours
    return round((pre + e["load_sec"] + (rtf or e["rtf"]) * 3600 * hours) / 60, 1)

def write_report(res: dict) -> Path:
    rep = env_report()
    L = [f"# 本機效能實測：{res['computer']}", "",
         f"- 實測時間：{res['tested_at']}｜程式版本：{res['app_version']}",
         f"- CPU：{rep.get('CPU', '')}｜記憶體：{rep.get('RamGB', '')} GB",
         f"- 顯示卡：{'、'.join(g.get('Name', '') for g in (rep.get('Gpus') or []))}",
         f"- 標準樣本：{res['sample']}（{SAMPLE_SEC // 60} 分鐘）", ""]
    r = res.get("recommended") or {}
    lt = res.get("long_test") or {}
    # 長檔實測的速度（同一配置）比 3 分鐘樣本更準
    rtf_long = lt.get("rtf_long") if lt and r and lt.get("model") == r.get("model") and lt.get("device") == r.get("device") else None
    rtf_use = rtf_long or (r.get("rtf") if r else None)
    L += ["## ✅ 這台電腦的建議配置", ""]
    if r:
        L += [f"- **辨識：{MODELS[r['model']][1]} × {r['device']}**，1 小時音檔約 **{rtf_use * 60:.0f} 分鐘**（RTF {rtf_use}"
              + ("，長檔實測）" if rtf_long else "，3 分鐘樣本換算，長檔通常更快）"),
              f"  - 理由：{r['reason']}"]
        if r.get("draft"):
            d = r["draft"]
            L.append(f"  - 趕時間的快速草稿：large-v3-turbo × {d['device']}，1 小時約 {d['min_per_hour']:.0f} 分鐘")
    else:
        L.append("- ❌ 沒有可用的辨識配置，請把畫面截圖回報。")
    if lt:
        L.append(f"- **長檔：{lt.get('note', '')}**")
    else:
        L.append("- 長檔：尚未測試，暫用預設「超過 15 分鐘的檔案每 10 分鐘切一段」。")
    fm = [f for f in res["formats"] if f.get("ok")]
    if fm:
        fast = min(fm, key=lambda f: f["mp3_sec_per_hour"] + f["decode_sec_per_hour"])
        L.append(f"- **檔案格式**：{'、'.join(f['format'] for f in fm)} 都能處理；前處理最快的是 {fast['format']}。"
                 "有原始錄音檔時，優先用錄音檔而不是影片。")
    L += ["- 轉逐字稿時會自動採用以上配置。", ""]

    if r:
        L += ["## ⏱ 效能評估", "",
              f"- **效能評等：{rating(rtf_use)}**",
              f"- RTF（辨識耗時 ÷ 音檔長度）＝ {rtf_use}（{'長檔實測' if rtf_long else '3 分鐘樣本'}）；數字越小越快，1.0 代表跟播放時間一樣長。", "",
              "### 預估處理時間（從拖進檔案到拿到逐字稿，分鐘）", "",
              "| 錄音長度 | m4a 錄音 | mp3 | mp4 影片 |", "|---|---|---|---|"]
        for h, lab in ((0.5, "30 分鐘"), (1, "1 小時"), (2, "2 小時")):
            cells = [estimate_minutes(r, res["engines"], res["formats"], h, f, rtf_long) for f in ("m4a", "mp3", "mp4")]
            L.append(f"| {lab} | " + " | ".join(f"約 {c:.0f}" if c is not None else "-" for c in cells) + " |")
        L += ["", ("> 預估值依長檔實測速度換算" if rtf_long else "> 預估值依 3 分鐘樣本換算（尚未做長檔測試，通常偏保守）")
              + "，實際會因內容（說話密度、靜音多寡）略有差異；第一次載入模型會多 1～3 分鐘。",
              "> 筆電速度會隨電源與溫度變動（同一台實測曾差到 1.6 倍）：處理長檔時請插電，並將電源模式設為「最佳效能」。", ""]

    L += ["## 辨識配置比較（3 分鐘樣本）", "",
          "| 模型 | 裝置 | 載入(秒) | 辨識(秒) | RTF | 1 小時約需(分) | 記憶體高峰(MB) | 開頭文字 |",
          "|---|---|---|---|---|---|---|---|"]
    for e in res["engines"]:
        if e.get("ok"):
            L.append(f"| {e['model']} | {e['device']} | {e['load_sec']} | {e['asr_sec']} | {e['rtf']} | "
                     f"{e['min_per_hour']} | {e.get('peak_mb') or '-'} | {e['preview']} |")
        else:
            L.append(f"| {e['model']} | {e['device']} | ❌ {e.get('error', '')[:80]} ||||||")
    L += ["", "> 第一次在顯示卡載入模型需要編譯，較久；之後有快取會快很多。各配置辨識出的全文在 "
          f"`{res['work_dir']}`，可比對品質。", ""]

    if lt:
        L += [f"## 長檔切段比較（{lt['source']} 前 {textout.fmt_dur(lt['test_sec'])}，{lt['model']} × {lt['device']}）", "",
              "| 方式 | 耗時(秒) | RTF | 記憶體高峰(MB) | 句數 | 字數 | 字數差異 | 重複句 | 最大空白(秒) | 切點最大空白(秒) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for m in lt["modes"]:
            if m.get("ok"):
                diff = m.get("chars_diff_pct")
                L.append(f"| {m['label']} | {m['sec']} | {m['rtf']} | {m.get('peak_mb') or '-'} | {m['segments']} | "
                         f"{m.get('chars', '-')} | {'基準' if m['chunk_min'] == 0 else (f'{diff:+.1f}%' if diff is not None else '-')} | "
                         f"{m['repeats']} | {m['max_gap']} | {m['max_gap_at_cut'] if m['chunk_min'] else '-'} |")
            else:
                L.append(f"| {m['label']} | ❌ {m.get('error', '')[:80]} |||||||||")
        if lt.get("per_cut_sec"):
            L.append(f"\n每個切點約多花 {lt['per_cut_sec']} 秒（切點前後各多辨識一小段，確保句子不被切斷）。")
        elif lt.get("per_cut_sec") is not None:
            L.append("\n切段沒有增加辨識時間（差異在量測誤差內）。")
        L += ["", "> 字數差異在 ±2% 內屬正常（斷句位置不同）；差異過大代表切點附近可能漏字或重複。",
              "> 重複句多通常代表模型「幻覺」；切點空白過大代表切點附近可能漏字。",
              "> 切段的好處：記憶體用量固定（低階電腦也能跑長檔），每段完成就存檔，中斷後可從斷點續跑。", ""]

    L += ["## 檔案格式（前處理時間）", "", "| 格式 | 可處理 | 轉 MP3（秒／每小時音檔） | 解碼（秒／每小時音檔） |", "|---|---|---|---|"]
    for f in res["formats"]:
        L.append(f"| {f['format']} | {'✅' if f.get('ok') else '❌ ' + f.get('error', '')[:60]} | "
                 f"{f.get('mp3_sec_per_hour', '-')} | {f.get('decode_sec_per_hour', '-')} |")
    L += ["", "> 各格式都會先轉成 16kHz 單聲道再辨識，格式只影響前處理時間，辨識速度與格式無關。", ""]
    md = benchmark_file().with_suffix(".md")
    md.write_text("\n".join(L), encoding="utf-8")
    return md


def _save(res: dict) -> Path:
    benchmark_file().write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return write_report(res)


# ------------------------------------------------------------------ 主程式
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run_all(argv, None)


def run_all(argv: list[str], long_choice: bool | None) -> int:
    """long_choice：True／False＝直接決定是否做長檔測試（網頁介面用）；None＝執行時詢問（命令列用）"""
    print(f"=== 本機效能實測 v{version()} ===")
    print("第 1 部分：快速健檢（約 5–10 分鐘）｜第 2 部分：長檔切段測試（詢問後才跑，約 6–10 分鐘）\n")
    try:
        sample = ensure_sample(Path(argv[0]) if argv else None)
        work = benchmark_dir() / f"benchmark_{datetime.now():%Y%m%d_%H%M%S}"
        work.mkdir(parents=True, exist_ok=True)

        print("\n[1-1] 檔案格式")
        formats = test_formats(sample, work)
        print("\n[1-2] 辨識配置")
        engines = test_engines(sample, work)
        res = {"computer": env_report().get("Computer") or benchmark_file().name.split(".")[0],
               "tested_at": f"{datetime.now():%Y-%m-%d %H:%M}", "app_version": version(),
               "sample": sample.name, "formats": formats, "engines": engines,
               "recommended": recommend(engines), "work_dir": str(work)}
        try:  # 保留上次長檔測試結果（只重跑快速健檢時不會被清掉）
            old = json.loads(benchmark_file().read_text(encoding="utf-8"))
            if old.get("long_test"):
                res["long_test"] = old["long_test"]
        except Exception:
            pass
        md = _save(res)
        print(f"\n第 1 部分完成，報告：{md}")

        rec = res["recommended"]
        if rec:
            long_src = Path(argv[1]) if len(argv) > 1 else None
            if not long_src:
                media = _media_files()
                longs = {p: d for p, d in media.items() if d >= 20 * 60}
                long_src = max(longs, key=longs.get) if longs else None
            if long_src:
                if long_choice is None:
                    ans = input(f"\n要進行長檔切段測試嗎？（取「{long_src.name}」前 30 分鐘，約 6–10 分鐘）(Y/N)：")
                    do_long = ans.strip().lower().startswith("y")
                else:
                    do_long = long_choice
                if do_long:
                    print("\n[2] 長檔切段測試")
                    res["long_test"] = test_long(long_src, rec["model"], rec["device"], work)
                    res["tested_at"] = f"{datetime.now():%Y-%m-%d %H:%M}"
                    md = _save(res)
            else:
                print("\n（samples\\ 裡沒有 20 分鐘以上的測試錄音，略過長檔測試；放一段長錄音到 samples\\ 再重新實測即可。）")

        print("\n" + md.read_text(encoding="utf-8"))
        print(f"報告：{md}")
        return 0
    except Exception as e:
        print(f"[錯誤] {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
