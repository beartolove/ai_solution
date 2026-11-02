"""網頁介面後端（FastAPI）。只在本機 127.0.0.1 提供服務，由 start.cmd 啟動。

一次只執行一個工作（轉檔或效能實測），避免同時佔用顯示卡；其餘排隊。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ROOT, audio, benchmark, diarize, downloader, glossary, llm, modelplan, pipeline, textout, tidy, translate, version
from .naming import name_budget
from .config import (DEFAULT_MEDIA_ROOT, DEFAULT_MODELS_ROOT, models_root, models_root_source, task_bases, MODELS, benchmark_dir, benchmark_file, count_tasks, downloads_dir,
                     env_report, legacy_jobs_dir, load_settings, media_root, media_root_source, ov_model_dir,
                     recommended, save_settings, uploads_dir)

PORT = int(os.environ.get("TRANSCRIBE_PORT") or 8766)
WEB = ROOT / "web"
EXTS = audio.AUDIO_EXT | audio.VIDEO_EXT

app = FastAPI(title="影音轉逐字稿")
_tasks: "queue.Queue[tuple[str, str]]" = queue.Queue()
_jobs: dict[str, dict] = {}          # 執行中／排隊中的任務狀態（完成後以 job.json 為準）
_bench = {"state": "idle", "log": [], "started": None, "finished": None, "error": ""}
_lock = threading.Lock()


# ------------------------------------------------------------------ 背景工作
def _worker():
    while True:
        kind, key = _tasks.get()
        try:
            if kind == "job":
                _run_job(key)
            elif kind == "bench":
                _run_bench(key == "long")
            elif kind == "diag":
                _run_diag(key)
        except Exception:
            traceback.print_exc()
        finally:
            _tasks.task_done()


class JobCancelled(Exception):
    pass


def _run_job(jid: str):
    j = _jobs.get(jid)
    if not j or j.get("state") != "queued":  # 排隊中就被取消或刪除（後續處理取消後會回到「完成」）
        return
    j.update(state="running", started=time.time(), stage="準備中", progress=0.0, diar_error="")

    def log(msg: str):
        j["log"].append(f"{datetime.now():%H:%M:%S} {msg}")

    if j.get("diarize_only"):  # 已完成的任務補做說話者辨識：逐字稿不動
        try:
            _diarize_for_job(j, log, 0.0)
        except (diarize.DiarizeCancelled, JobCancelled):
            log("已取消分辨說話者（逐字稿不受影響）")
        except Exception as e:
            log(f"[分辨說話者失敗] {e}")
            j["diar_error"] = str(e)
        j.update(state="done", progress=1.0, stage="完成", cancel=False)
        return
    if j.get("translate_only"):  # 已完成的任務翻譯成中文：逐字稿不動
        try:
            _translate_for_job(j, log)
        except (translate.TranslateCancelled, JobCancelled):
            log("已取消翻譯（逐字稿不受影響）")
        except Exception as e:
            log(f"[翻譯失敗] {e}")
            j["trans_error"] = str(e)
        j.update(state="done", progress=1.0, stage="完成", cancel=False)
        return

    if j.get("tidy_only"):  # 已完成的任務整理逐字稿：原本的 SRT／TXT 不動
        try:
            _tidy_for_job(j, log, 0.0)
        except (tidy.TidyCancelled, JobCancelled):
            log("已取消整理（逐字稿不受影響）")
        except Exception as e:
            log(f"[整理失敗] {e}")
            j["tidy_error"] = str(e)
        j.update(state="done", progress=1.0, stage="完成", cancel=False)
        return

    want_tidy = bool(j.get("tidy"))
    end = 0.85 if want_tidy else 1.0  # 有勾整理逐字稿：最後 15% 給整理
    share = (0.7 if j.get("diarize") else 1.0) * end  # 有勾分辨說話者：轉文字佔進度條 70%

    def prog(f: float, msg: str):
        if j.get("cancel"):  # 每辨識完一段（約 30 秒音訊）檢查一次
            raise JobCancelled()
        j["progress"], j["stage"] = round(f * share, 3), msg

    try:
        if j.get("url") and not j.get("src"):
            _download_for_job(j, log)
        r = pipeline.run(Path(j["src"]), j["model"], j["device"], j["lang"], out_dir=Path(j["dir"]),
                         log=log, progress=prog, save_mp3=False if j.get("url") else None,
                         glossary_name=j.get("glossary") or "")
        j["result"] = r
        if j.get("diarize"):
            try:  # 說話者辨識失敗或取消時，逐字稿照樣完成
                _diarize_for_job(j, log, share, end)
            except (diarize.DiarizeCancelled, JobCancelled):
                log("已取消分辨說話者（逐字稿已完成，不受影響）")
            except Exception as e:
                log(f"[分辨說話者失敗] {e}")
                j["diar_error"] = str(e)
        if want_tidy and not j.get("cancel"):
            try:  # 整理失敗或取消時，逐字稿照樣完成
                if not llm.ready():
                    raise llm.LLMError("還沒有本機 LLM：請到「參數設定 → 本機 LLM」下載")
                _tidy_for_job(j, log, end)
            except (tidy.TidyCancelled, JobCancelled):
                log("已取消整理（逐字稿已完成，不受影響）")
            except Exception as e:
                log(f"[整理失敗] {e}")
                j["tidy_error"] = str(e)
        j.update(state="done", progress=1.0, stage="完成", cancel=False)
    except (JobCancelled, downloader.DownloadCancelled):
        log("已取消")
        j.update(state="cancelled", stage="已取消")
        (Path(j["dir"]) / "cancelled.txt").write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} 使用者取消\n", encoding="utf-8")
    except downloader.DownloadError as e:
        log(f"[下載失敗] {e}")
        if e.detail:
            log(f"  yt-dlp：{e.detail}")
        j.update(state="error", stage="下載失敗", error=str(e))
        (Path(j["dir"]) / "error.txt").write_text(f"{e}\n\n種類：{e.kind}\n{e.detail}\n", encoding="utf-8")
    except Exception as e:
        log(f"[錯誤] {e}")
        j.update(state="error", stage="失敗", error=str(e))
        (Path(j["dir"]) / "error.txt").write_text(str(e) + "\n\n" + traceback.format_exc(), encoding="utf-8")


def _diarize_for_job(j: dict, log, start: float, end: float = 1.0):
    """安裝元件（第一次）→ 分辨說話者 → 依說話者重新產生 SRT／TXT"""
    d = Path(j["dir"])
    lo, span = start, end - start

    def prog(f: float, msg: str):
        j["progress"], j["stage"] = round(lo + f * span, 3), msg

    cancelled = lambda: bool(j.get("cancel"))
    srt = _main_srt(d)
    seg_file = srt.with_suffix(".json") if srt else None
    if not seg_file or not seg_file.exists():
        raise diarize.DiarizeError("找不到逐字稿的時間軸檔（.json），無法分辨說話者")
    base = srt.with_suffix("")
    segs = json.loads(seg_file.read_text(encoding="utf-8"))
    if not diarize.installed():
        diarize.install(log, lambda f, m: prog(f, m), cancelled)
    src, _ = _media_file(d)
    if not src:
        raise diarize.DiarizeError("找不到錄音或影片檔")
    n = (j.get("diarize") or {}).get("n")
    t0 = time.time()
    turns = diarize.diarize(src, n, lambda f, m: prog(0.2 + f * 0.8, m), cancelled, log)
    data = diarize.save_and_write(base, segs, turns, n, time.time() - t0)
    log(f"已依說話者更新字幕：{'、'.join(data['names'])}")
    try:  # 在任務紀錄記下說話者辨識的耗時
        jf = d / "job.json"
        r = json.loads(jf.read_text(encoding="utf-8"))
        r["diarization"] = {"count": data["count"], "seconds": data["seconds"], "requested": n}
        jf.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        j["result"] = r
    except Exception:
        pass


def _tidy_for_job(j: dict, log, start: float):
    """用本機 LLM 把逐字稿整理成有標點、分段、小標題的純文字（另存 <檔名>.整理.txt）"""
    d = Path(j["dir"])
    srt = _main_srt(d)
    seg_file = textout_with(srt, "json") if srt else None
    if not seg_file or not seg_file.exists():
        raise llm.LLMError("找不到逐字稿的時間軸檔（.json），無法整理")
    segs = json.loads(seg_file.read_text(encoding="utf-8"))

    def prog(f: float, msg: str):
        if j.get("cancel"):
            raise tidy.TidyCancelled()
        j["progress"], j["stage"] = round(start + f * (1.0 - start), 3), msg

    tidy.run(srt.with_suffix(""), segs, j.get("name") or "", prog, lambda: bool(j.get("cancel")), log)


def _translate_for_job(j: dict, log):
    """逐字稿翻成正體中文（雙語對照用），結果另存，不改原本的字幕"""
    d = Path(j["dir"])
    srt = _main_srt(d)
    seg_file = textout_with(srt, "json") if srt else None
    if not seg_file or not seg_file.exists():
        raise translate.TranslateError("找不到逐字稿的時間軸檔（.json），無法翻譯")
    segs = json.loads(seg_file.read_text(encoding="utf-8"))
    texts = [s["text"] for s in segs]
    src = (j.get("translate") or {}).get("src") or "auto"
    if src == "auto":
        src = translate.detect(texts)
    if src == "zh":
        raise translate.TranslateError("原文已是中文，不需要翻譯")
    log(f"翻譯成中文（原文：{translate.LANGS.get(src, src)}，共 {len(texts)} 句）")

    def prog(f: float, msg: str):
        if j.get("cancel"):
            raise translate.TranslateCancelled()
        j["progress"], j["stage"] = round(f, 3), msg

    t0 = time.time()
    lines = translate.translate(texts, src, prog, lambda: bool(j.get("cancel")), log)
    translate.save(srt.with_suffix(""), segs, lines, src, time.time() - t0)


def _parse_diarize(v: str) -> dict | None:
    """表單值：空白＝不分辨；auto＝自動判斷人數；數字＝指定人數"""
    v = (v or "").strip().lower()
    if not v or v in ("0", "off", "false"):
        return None
    return {"n": int(v)} if v.isdigit() and 1 <= int(v) <= 20 else {"n": None}


def _download_for_job(j: dict, log):
    """網址任務：直接下載到 downloads\\<任務>\\（暫存名 media.*）→ 改成正體標題 → 交給辨識"""
    d = Path(j["dir"])

    def prog(f: float, msg: str):
        j["progress"], j["stage"] = round(f * 0.1, 3), msg  # 下載佔進度條前 10%，辨識沿用原本的比例（下載中的文字另有百分比）

    try:
        got = downloader.download_auto(j["url"], d, progress=prog, cancelled=lambda: bool(j.get("cancel")), log=log,
                                       use_cookies=bool(j.get("use_cookies")))
        dest = d / (j["file_stem"] + got.suffix.lower())  # 正體標題、Windows 安全檔名
        got.replace(dest)
        j["src"] = str(dest)
    finally:
        # 只清掉程式自己產生的未完成下載片段（media.*.part 等），其他檔案不動
        for p in d.glob("media.*"):
            if p.suffix in (".part", ".ytdl") or ".part-" in p.name or p.name.endswith(".temp"):
                p.unlink(missing_ok=True)


_diag = {"state": "idle", "log": [], "result": None}


def _run_diag(key: str):
    """診斷：說話者辨識的速度。key＝「devices|任務」比較執行方式（最快的存成設定）；「threads|任務」比較 CPU 執行緒數"""
    what, jid = key.split("|", 1)
    _diag.update(state="running", log=[], result=None)
    log = lambda m: _diag["log"].append(f"{datetime.now():%H:%M:%S} {m}")
    try:
        src, _ = _media_file(_job_dir(jid))
        if what == "devices":
            res = diarize.bench_devices(src, 60, log)
            data = load_settings()
            data["diarize_device"] = min(res, key=lambda x: x["sec"])["mode"]
            save_settings(data)
            _plan_refresh()
            _diag["result"] = res
        else:
            _diag["result"] = diarize.bench_threads(src, 60, log)
        _diag["state"] = "done"
    except Exception as e:
        log(f"[錯誤] {e}")
        _diag["state"] = "error"


@app.post("/api/diag/diarize")
def diag_diarize(jid: str = Form(...), what: str = Form("devices")):
    if not diarize.installed():
        raise HTTPException(409, "還沒安裝說話者辨識元件")
    if _diag["state"] == "running":
        raise HTTPException(409, "診斷進行中")
    _job_dir(jid)
    _diag.update(state="queued", log=[], result=None)
    _tasks.put(("diag", f"{'threads' if what == 'threads' else 'devices'}|{jid}"))
    return {"ok": True}


@app.get("/api/diag/diarize")
def diag_status():
    return _diag | {"threads_setting": load_settings().get("diarize_threads")}


@app.post("/api/diag/diarize/apply")
def diag_apply(threads: int = Form(...)):
    """套用說話者辨識的 CPU 執行緒數（0＝回到 PyTorch 預設）"""
    data = load_settings()
    if threads > 0:
        data["diarize_threads"] = threads
    else:
        data.pop("diarize_threads", None)
    save_settings(data)
    return {"ok": True, "diarize_threads": data.get("diarize_threads")}


class _LogWriter(io.TextIOBase):
    """把效能實測的 print 輸出接到網頁的紀錄區"""

    def __init__(self, sink: list):
        self.sink, self.buf = sink, ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.sink.append(line.rstrip())
        return len(s)


def _run_bench(long: bool):
    _bench.update(state="running", log=[], started=time.time(), finished=None, error="")
    try:
        with contextlib.redirect_stdout(_LogWriter(_bench["log"])):
            code = benchmark.run_all([], long)
        _bench.update(state="done" if code == 0 else "error", finished=time.time())
        _plan_refresh()  # 實測結果改變了最佳配置，更新這台的模型配置方案
        if code != 0:
            _bench["error"] = "實測沒有完成，請看下方紀錄。"
    except Exception as e:
        _bench.update(state="error", finished=time.time(), error=str(e))


# ------------------------------------------------------------------ 資料整理
def _url_info(d: Path) -> dict:
    try:
        return json.loads((d / "source_url.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _job_from_disk(d: Path) -> dict | None:
    u = _url_info(d)
    j = _job_from_disk_raw(d)
    if j and u:
        j["url"] = u.get("url", "")
        j["name"] = u.get("title") or j["name"]  # 網址任務一律顯示影片標題
    return j


def _job_from_disk_raw(d: Path) -> dict | None:
    f = d / "job.json"
    if f.exists():
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            return {"id": d.name, "name": re.split(r"[\\/]", str(r["source"]))[-1], "state": "done", "progress": 1.0, "stage": "完成",
                    "created": r.get("finished_at", ""), "result": r}
        except Exception:
            return None
    if (d / "cancelled.txt").exists():
        return {"id": d.name, "name": d.name, "state": "cancelled", "stage": "已取消", "created": ""}
    if (d / "error.txt").exists():
        return {"id": d.name, "name": d.name, "state": "error", "stage": "失敗",
                "error": (d / "error.txt").read_text(encoding="utf-8").split("\n")[0], "created": ""}
    if any(d.glob("*")) and not d.name.startswith("benchmark_"):
        return {"id": d.name, "name": d.name, "state": "interrupted", "stage": "未完成（程式曾關閉）", "created": ""}
    return None


_BG_KINDS = (("tidy_only", "tidy"), ("diarize_only", "diarize"), ("translate_only", "translate"))


def _bg(j: dict) -> dict | None:
    """已完成任務的後續處理（整理、分辨說話者、翻譯）：任務本身仍算完成，只另外帶進度"""
    if j.get("state") not in ("queued", "running"):
        return None
    for flag, kind in _BG_KINDS:
        if j.get(flag):
            return {"kind": kind, "state": j["state"], "progress": j.get("progress", 0.0), "stage": j.get("stage", "")}
    return None


def _public(j: dict) -> dict:
    keys = ("id", "name", "state", "progress", "stage", "created", "error", "model", "device", "url", "diar_error",
            "trans_error", "tidy_error")
    out = {k: j.get(k) for k in keys if k in j}
    m = re.match(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", j.get("id") or "")
    if m:  # 建立時間一律取自任務資料夾名稱（舊任務也有）
        out["created"] = f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]}"
    out["origin"] = "url" if j.get("url") else "upload"
    if j.get("state") == "running" and j.get("started"):
        out["elapsed"] = round(time.time() - j["started"])  # 已經過的秒數，讓使用者知道仍在進行
    bg = _bg(j)
    if bg:  # 列表與任務頁照樣顯示為完成，另外附上進度（畫面不切換）
        out["state"], out["bg"] = "done", bg
        out.pop("progress", None)
        out.pop("elapsed", None)
    if j.get("diarize_only"):
        out["kind"] = "diarize"
    if j.get("translate_only"):
        out["kind"] = "translate"
    if j.get("tidy_only"):
        out["kind"] = "tidy"
    r = j.get("result")
    if r:
        out["summary"] = {"duration": r["duration_sec"], "asr_sec": r["seconds"]["asr"], "total_sec": r["seconds"]["total"],
                          "rtf": r["rtf"], "model": r["model"], "device": r["device"], "segments": r["segments"],
                          "chars": r["chars"], "chunk_min": r.get("chunk_min", 0)}
    return out


_EXTRA_SUFFIX = (".zh-TW.srt", ".bilingual.srt", ".zh-TW.json", ".speakers.json", ".asr.json", ".review.json", ".glossary.json",
                 ".整理.txt", ".tidy.json")


def _main_srt(d: Path) -> Path | None:
    """任務的主要字幕（排除翻譯、雙語等附加字幕）"""
    return next((f for f in sorted(d.glob("*.srt")) if not f.name.endswith(_EXTRA_SUFFIX)), None)


def _main_file(d: Path, ext: str) -> Path | None:
    srt = _main_srt(d)
    if srt and textout_with(srt, ext).exists():
        return textout_with(srt, ext)
    return next((f for f in sorted(d.glob(f"*.{ext}")) if not f.name.endswith(_EXTRA_SUFFIX)), None)


def textout_with(srt: Path, ext: str) -> Path:
    return srt.with_name(srt.name[:-4] + "." + ext)


def _job_dir(jid: str) -> Path:
    """任務資料夾只允許在 uploads\\ 或 downloads\\ 底下的一層"""
    for base in task_bases():
        base = base.resolve()
        d = (base / jid).resolve()
        if d.parent != base:
            raise HTTPException(400, "任務代號不正確")
        if d.is_dir():
            return d
    raise HTTPException(404, "找不到這個任務")


# ------------------------------------------------------------------ API
def _safe_status() -> dict:
    try:
        return diarize.status()
    except Exception:  # 狀態檢查失敗不可讓整個介面無法使用
        return {"installed": False, "model": False, "model_dir": ""}


@app.get("/api/info")
def info():
    rep = env_report()
    rec = recommended()
    return {
        "version": version(),
        "computer": rep.get("Computer") or "",
        "alias": modelplan.alias(), "display": modelplan.display_name(),
        "features": modelplan.chosen_features()[0],
        "cpu": rep.get("CPU", ""), "ram_gb": rep.get("RamGB"),
        "gpus": [g.get("Name", "") for g in (rep.get("Gpus") or [])],
        "tier": rep.get("Tier", ""), "engine": rep.get("Engine", ""),
        "recommended": rec,
        "models": {k: {"label": v[1], "ready": (ov_model_dir(k) / "openvino_encoder_model.xml").exists()}
                   for k, v in MODELS.items()},
        "benchmarked": benchmark_file().exists(),
        "ytdlp": downloader.update_status(),
        "diarization": _safe_status(),
        "translator": translate.status(),
        "llm": llm.status(),
        "glossaries": glossary.names(),
        "media_root": str(media_root()),
        "uploads_dir": str(uploads_dir()),
    }


@app.post("/api/jobs")
async def create_jobs(files: list[UploadFile] = File(...), model: str = Form(""), lang: str = Form("auto"),
                      speakers: str = Form(""), glossary_name: str = Form("", alias="glossary"), tidy_opt: str = Form("", alias="tidy")):
    rec = recommended()
    model = model if model in MODELS else rec["model"]
    created = []
    for uf in files:
        name = Path(uf.filename or "audio").name
        if Path(name).suffix.lower() not in EXTS:
            created.append({"name": name, "state": "error", "error": "不支援的檔案格式"})
            continue
        n = name_budget(uploads_dir())
        stem = pipeline._safe(Path(name).stem, n)
        jid = f"{datetime.now():%Y%m%d_%H%M%S}_{stem}"
        d = uploads_dir() / jid
        if d.exists():
            jid += f"_{uuid.uuid4().hex[:4]}"
            d = uploads_dir() / jid
        d.mkdir(parents=True)
        src = d / (stem + Path(name).suffix.lower())  # 保留原檔名（Windows 安全版），輸出檔名才會一致
        with src.open("wb") as out:
            while chunk := await uf.read(4 * 1024 * 1024):
                out.write(chunk)
        _jobs[jid] = {"id": jid, "name": name, "dir": str(d), "src": str(src), "model": model,
                      "device": rec["device"], "lang": lang, "state": "queued", "progress": 0.0,
                      "diarize": _parse_diarize(speakers), "glossary": _gl_name(glossary_name),
                      "tidy": tidy_opt == "1", "stage": "排隊中", "log": [], "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
        _tasks.put(("job", jid))
        created.append(_public(_jobs[jid]))
    return created


@app.post("/api/jobs/url")
def create_url_job(url: str = Form(...), model: str = Form(""), lang: str = Form("auto"), speakers: str = Form(""),
                   glossary_name: str = Form("", alias="glossary"), tidy_opt: str = Form("", alias="tidy")):
    """貼網址：先確認是單一、可下載的影片（約數秒），再加入佇列下載＋辨識"""
    try:
        return _enqueue_url(url, model, lang, speakers, glossary_name, tidy_opt == "1")
    except downloader.DownloadError as e:
        raise HTTPException(400, str(e))


def _gl_name(v: str) -> str:
    return v if v and v in glossary.names() else ""


def _enqueue_url(url: str, model: str = "", lang: str = "auto", speakers: str = "", glossary_name: str = "",
                 want_tidy: bool = False) -> dict:
    """確認網址（需要登入時自動帶入同步的登入資料重試）→ 建立任務、排入佇列"""
    meta = downloader.probe_auto(url)
    use_cookies = bool(meta.pop("cookies", False))
    rec = recommended()
    model = model if model in MODELS else rec["model"]
    title = meta["title"]  # 已轉正體
    stem = pipeline._safe(title, name_budget(downloads_dir()))
    jid = f"{datetime.now():%Y%m%d_%H%M%S}_{stem}"
    d = downloads_dir() / jid  # 網路下載的任務放 downloads\\（上傳的放 uploads\\）
    if d.exists():
        jid += f"_{uuid.uuid4().hex[:4]}"
        d = downloads_dir() / jid
    d.mkdir(parents=True)
    (d / "source_url.json").write_text(json.dumps({"url": url.strip(), **meta,
                                                   "added_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    _jobs[jid] = {"id": jid, "name": title, "dir": str(d), "url": url.strip(), "file_stem": stem, "model": model,
                  "device": rec["device"], "lang": lang, "state": "queued", "progress": 0.0,
                  "diarize": _parse_diarize(speakers), "glossary": _gl_name(glossary_name), "tidy": want_tidy,
                  "stage": "排隊中（等待下載）", "use_cookies": use_cookies,
                  "log": [f"{datetime.now():%H:%M:%S} 網址：{url.strip()}" + ("（需要登入，已帶入同步的登入資料）" if use_cookies else "")],
                  "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    _tasks.put(("job", jid))
    return _public(_jobs[jid])


def _hidden_file() -> Path:
    return uploads_dir() / "_queue_hidden.json"


def _hidden() -> set[str]:
    try:
        return set(json.loads(_hidden_file().read_text(encoding="utf-8")))
    except Exception:
        return set()


@app.post("/api/jobs/dismiss")
def dismiss(ids: str = Form("")):
    """從「處理中與最近完成」清單移除（只是不顯示；檔案與任務紀錄都保留，不刪除任何檔案）。
    ids 空白＝清除所有已完成／失敗的任務。"""
    hidden = _hidden()
    want = [i for i in ids.split(",") if i] or [j["id"] for j in list_jobs() if j["state"] not in ("queued", "running")]
    hidden.update(want)
    _hidden_file().write_text(json.dumps(sorted(hidden), ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "count": len(want)}


def _feats(d: Path) -> dict:
    """列表標示與篩選用：已分辨說話者、已翻譯、有整理稿、有校對"""
    return {"spk": any(d.glob("*.speakers.json")), "tr": any(d.glob("*.zh-TW.json")),
            "td": any(d.glob("*.tidy.json")), "ed": any(d.glob("*.asr.json"))}


@app.get("/api/jobs")
def list_jobs():
    seen, out = set(), []
    hidden = _hidden()
    for jid, j in sorted(_jobs.items(), reverse=True):
        jd = Path(j["dir"]) if j.get("dir") else None
        out.append(_public(j) | {"hidden": jid in hidden, "tags": _read_tags(jd) if jd else []} | (_feats(jd) if jd and jd.exists() else {}))
        seen.add(jid)
    dirs = [d for base in task_bases() for d in base.iterdir()]
    for d in sorted(dirs, key=lambda p: p.name, reverse=True):  # 資料夾名稱以日期時間開頭
        if d.is_dir() and d.name not in seen and not d.name.startswith(("benchmark_", "_")):
            j = _job_from_disk(d)
            if j:  # 篩選用：是否已分辨說話者、已翻譯
                out.append(_public(j) | {"hidden": d.name in hidden, "tags": _read_tags(d)} | _feats(d))
    out.sort(key=lambda x: x.get("id") or "", reverse=True)  # 依建立時間排，後續處理中的任務不跳到最上面
    return out


@app.get("/api/jobs/{jid}")
def job_detail(jid: str):
    d = _job_dir(jid)
    j = _jobs.get(jid) or _job_from_disk(d) or {"id": jid, "name": jid, "state": "unknown"}
    out = _public(j)
    out["log"] = j.get("log", [])[-200:]
    # 時間軸檔＝與字幕同名的 .json（不可抓到 job.json、source_url.json 等紀錄檔）
    srt = _main_srt(d)
    seg_file = srt.with_suffix(".json") if srt and srt.with_suffix(".json").exists() else \
        next((f for f in d.glob("*.json") if f.name not in ("job.json", "source_url.json") and not f.name.endswith(_EXTRA_SUFFIX)), None)
    is_done = out.get("state") == "done"
    if is_done and seg_file:
        try:
            out["segments"] = json.loads(seg_file.read_text(encoding="utf-8"))
        except Exception:
            out["segments"] = []
    out["files"] = {ext: f.name for ext in ("mp3", "srt", "txt") if (f := _main_file(d, ext))}
    if is_done and srt:
        sp = diarize.load(srt.with_suffix(""))
        if sp:
            out["speakers"] = {"names": sp["names"], "seg": sp["seg"], "count": sp["count"]}
        b = srt.with_suffix("")
        gr = glossary.load_report(b)
        if gr:
            out["glossary"] = {"name": gr.get("glossary"), "created": gr.get("created"),
                               "replaced": gr.get("replaced", []), "flagged": gr.get("flagged", [])}
        rv = _review(b)
        out["review"] = {"has_asr": textout.with_ext(b, ".asr.json").exists(), "edited": rv.get("edited", []),
                         "measure": rv.get("measure")}
        td = tidy.load(b)
        if td:
            td["stale"] = bool(rv.get("updated") and td.get("created") and rv["updated"] > td["created"])  # 整理後又校對過
            out["tidy"] = td
            out["files"]["tidy"] = tidy.out_file(b).name
        tr = translate.load(srt.with_suffix(""))
        if tr:
            out["translation"] = {"lines": tr["lines"], "src": tr["src"]}
            out["files"]["zh"] = textout_with(srt, "zh-TW.srt").name
            out["files"]["bi"] = textout_with(srt, "bilingual.srt").name
        if isinstance(out.get("segments"), list):
            out["src_lang"] = translate.detect([s.get("text", "") for s in out["segments"][:200]])
    if is_done:
        mf, kind = _media_file(d)
        out["media"] = {"kind": kind, "name": mf.name} if mf else None
        if "mp3" not in out["files"] and mf and kind == "audio":
            out["files"]["audio"] = mf.name  # 沒另存 MP3（原始檔已是 m4a 等）→ 提供原始錄音下載
        if mf and kind == "video" and j.get("url"):
            out["files"]["video"] = mf.name  # 網址下載的影片
    return out


@app.post("/api/jobs/{jid}/diarize")
def diarize_job(jid: str, speakers: str = Form("auto")):
    """已完成的任務補做說話者辨識（只分析聲音，不重新轉文字）"""
    d = _job_dir(jid)
    cur = _jobs.get(jid)
    if cur and cur.get("state") in ("queued", "running"):
        raise HTTPException(409, "這個任務正在處理中")
    base = _job_from_disk(d)
    if not base or base.get("state") != "done":
        raise HTTPException(409, "任務完成後才能分辨說話者")
    j = dict(base, dir=str(d), state="queued", progress=0.0, stage="排隊中（分辨說話者）", log=[],
             diarize=_parse_diarize(speakers) or {"n": None}, diarize_only=True)
    _jobs[jid] = j
    _tasks.put(("job", jid))
    return _public(j)


@app.post("/api/jobs/{jid}/translate")
def translate_job(jid: str, src: str = Form("auto")):
    """已完成的任務翻譯成正體中文（雙語對照）"""
    d = _job_dir(jid)
    cur = _jobs.get(jid)
    if cur and cur.get("state") in ("queued", "running"):
        raise HTTPException(409, "這個任務正在處理中")
    if not translate.ready():
        raise HTTPException(409, "還沒有翻譯模型：請管理者執行 admin-download-translator.cmd")
    base = _job_from_disk(d)
    if not base or base.get("state") != "done":
        raise HTTPException(409, "任務完成後才能翻譯")
    srt = _main_srt(d)
    if srt and src == "auto" and textout_with(srt, "json").exists():  # 原文已是中文就不排入佇列
        try:
            segs = json.loads(textout_with(srt, "json").read_text(encoding="utf-8"))
            if translate.detect([x.get("text", "") for x in segs[:200]]) == "zh":
                raise HTTPException(409, "原文已是中文，不需要翻譯")
        except HTTPException:
            raise
        except Exception:
            pass
    j = dict(base, dir=str(d), state="queued", progress=0.0, stage="排隊中（翻譯成中文）", log=[],
             translate={"src": src if src in translate.LANGS or src == "auto" else "auto"}, translate_only=True)
    _jobs[jid] = j
    _tasks.put(("job", jid))
    return _public(j)


@app.post("/api/jobs/{jid}/tidy")
def tidy_job(jid: str):
    """已完成的任務整理逐字稿（標點、分段、小標題）；可重新整理（覆蓋上次的整理稿）"""
    d = _job_dir(jid)
    cur = _jobs.get(jid)
    if cur and cur.get("state") in ("queued", "running"):
        raise HTTPException(409, "這個任務正在處理中")
    if not llm.ready():
        raise HTTPException(409, "還沒有本機 LLM：請到「參數設定 → 本機 LLM」下載")
    base = _job_from_disk(d)
    if not base or base.get("state") != "done":
        raise HTTPException(409, "任務完成後才能整理")
    j = dict(base, dir=str(d), state="queued", progress=0.0, stage="排隊中（整理逐字稿）", log=[], tidy_only=True)
    _jobs[jid] = j
    _tasks.put(("job", jid))
    return _public(j)


@app.post("/api/jobs/{jid}/speakers")
def rename_speakers(jid: str, names: str = Form(...)):
    """改說話者名稱（JSON 陣列），SRT／TXT 一起更新"""
    d = _job_dir(jid)
    cur = _jobs.get(jid)
    if cur and cur.get("state") in ("queued", "running"):
        raise HTTPException(409, "任務處理中，請完成後再改名")
    srt = _main_srt(d)
    if not srt or not srt.with_suffix(".json").exists():
        raise HTTPException(404, "找不到逐字稿")
    try:
        lst = json.loads(names)
        assert isinstance(lst, list)
    except Exception:
        raise HTTPException(400, "名稱格式不正確")
    segs = json.loads(srt.with_suffix(".json").read_text(encoding="utf-8"))
    try:
        data = diarize.rename(srt.with_suffix(""), segs, [str(x) for x in lst])
    except diarize.DiarizeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "names": data["names"]}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    j = _jobs.get(jid)
    if not j or j.get("state") not in ("queued", "running"):
        raise HTTPException(409, "這個任務不在處理中，無法取消")
    if j["state"] == "queued" and _bg(j):  # 已完成任務的後續處理還在排隊：取消後任務仍是完成（不可寫 cancelled.txt）
        j.update(state="done", stage="完成", progress=1.0)
        for flag, _ in _BG_KINDS:
            j.pop(flag, None)
    elif j["state"] == "queued":
        j.update(state="cancelled", stage="已取消")
        (Path(j["dir"]) / "cancelled.txt").write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} 使用者取消\n", encoding="utf-8")
    else:
        j["cancel"] = True
        j["stage"] = "取消中（下載會立即停止；辨識在目前這一段完成後停止）"
    return {"ok": True}


# ------------------------------------------------------------------ 專有名詞庫與校對（0.11.0）
def _review(base: Path) -> dict:
    try:
        return json.loads(textout.with_ext(base, ".review.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_review(base: Path, data: dict) -> None:
    textout.with_ext(base, ".review.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _job_segs(jid: str) -> tuple[Path, list[dict]]:
    d = _job_dir(jid)
    cur = _jobs.get(jid)
    if cur and cur.get("state") in ("queued", "running"):
        raise HTTPException(409, "任務處理中，請完成後再操作")
    srt = _main_srt(d)
    if not srt or not textout_with(srt, "json").exists():
        raise HTTPException(404, "找不到逐字稿")
    return srt.with_suffix(""), json.loads(textout_with(srt, "json").read_text(encoding="utf-8"))


def _rewrite(base: Path, segs: list[dict]) -> None:
    """更新逐字稿：時間軸 json＋SRT／TXT；有分辨說話者時，SRT／TXT 保留說話者標示"""
    textout.write_outputs([(s["start"], s["end"], s["text"]) for s in segs], base)
    sp = diarize.load(base)
    if sp:
        diarize.write_files(base, segs, sp)


@app.post("/api/jobs/{jid}/glossary")
def apply_glossary(jid: str, name: str = Form(...), from_i: int = Form(-1)):
    """已完成的任務套用詞庫（只做誤寫替換，不重新辨識）；常用詞類的誤寫只列出提醒。
    from_i ≥ 0：校對途中用，只套用到第 from_i 句以後、還沒校對過的句子，回傳改到的句子"""
    base, segs = _job_segs(jid)
    try:
        entries = glossary.load(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    if from_i < 0:
        new, rep = glossary.apply(segs, entries)
        _rewrite(base, new)
        glossary.save_report(base, name, rep)
        return {"ok": True, "replaced": sum(r["count"] for r in rep["replaced"]), "flagged": len(rep["flagged"])}
    done = set(_review(base).get("edited", []))
    idx = [i for i in range(max(0, from_i), len(segs)) if i not in done]
    part, rep = glossary.apply([segs[i] for i in idx], entries)
    for r in rep["replaced"] + rep["flagged"]:
        r["i"] = idx[r["i"]]
    changed = [{"i": i, "text": s["text"]} for i, s in zip(idx, part) if s["text"] != segs[i]["text"]]
    n_rep = sum(r["count"] for r in rep["replaced"])
    new_flag = list(rep["flagged"])
    if changed:
        for c in changed:
            segs[c["i"]]["text"] = c["text"]
        _rewrite(base, segs)
        old = glossary.load_report(base) or {}
        if old.get("glossary") == name:  # 併入原本的報告
            rep["replaced"] = old.get("replaced", []) + rep["replaced"]
            seen = {(f["i"], f["word"]) for f in old.get("flagged", [])}
            rep["flagged"] = old.get("flagged", []) + [f for f in rep["flagged"] if (f["i"], f["word"]) not in seen]
        glossary.save_report(base, name, rep, old.get("prompt", ""))
    return {"ok": True, "replaced": n_rep, "changed": changed, "flagged": new_flag}


@app.post("/api/jobs/{jid}/segment")
async def edit_segment(jid: str, request: Request):
    """校對：修改第 i 句的文字（不改時間）。第一次修改前先保留辨識原稿 .asr.json，供正確率比對"""
    data = await request.json()
    base, segs = _job_segs(jid)
    i, text = int(data.get("i", -1)), re.sub(r"\s*\n\s*", " ", str(data.get("text", ""))).strip()
    if not 0 <= i < len(segs):
        raise HTTPException(400, "句子編號不正確")
    asr = textout.with_ext(base, ".asr.json")
    if not asr.exists():
        asr.write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    if segs[i]["text"] == text:
        return {"ok": True, "changed": False}
    segs[i]["text"] = text
    _rewrite(base, segs)
    rv = _review(base)
    rv["edited"] = sorted(set(rv.get("edited", [])) | {i})
    rv["updated"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    _save_review(base, rv)
    return {"ok": True, "changed": True, "edited": len(rv["edited"])}


@app.post("/api/jobs/{jid}/measure")
def measure_job(jid: str, name: str = Form(...), start_min: float = Form(0), end_min: float = Form(10)):
    """正確率：校對後的版本（標準答案）與辨識原稿逐句比對，只看指定時間範圍"""
    base, segs = _job_segs(jid)
    asr = textout.with_ext(base, ".asr.json")
    if not asr.exists():
        raise HTTPException(409, "還沒有校對過：請先在「校對模式」修改逐字稿，再產生報告")
    try:
        entries = glossary.load(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    hyp = json.loads(asr.read_text(encoding="utf-8"))
    rep = glossary.measure(segs, hyp, entries, start_min * 60, end_min * 60)
    rep |= {"glossary": name, "start_min": start_min, "end_min": end_min, "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    rv = _review(base)
    rv["measure"] = rep
    _save_review(base, rv)
    return rep


@app.get("/api/jobs/{jid}/edits")
def job_edits(jid: str, name: str):
    """校對結果中每一處修改，供挑選加入詞庫"""
    base, segs = _job_segs(jid)
    asr = textout.with_ext(base, ".asr.json")
    if not asr.exists():
        raise HTTPException(409, "還沒有校對過")
    try:
        entries = glossary.load(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return glossary.edits(segs, json.loads(asr.read_text(encoding="utf-8")), entries)


@app.post("/api/jobs/{jid}/replace")
async def job_replace(jid: str, request: Request):
    """只在這份逐字稿替換（不寫進詞庫）：{pairs:[{wrong,right}], from_i}；只改還沒校對過的句子，回傳改到的句子"""
    data = await request.json()
    pairs = [(str(p.get("wrong", "")).strip(), str(p.get("right", "")).strip()) for p in data.get("pairs", [])[:300]]
    pairs = sorted([p for p in pairs if p[0] and p[1] and p[0] != p[1]], key=lambda p: -len(p[0]))
    base, segs = _job_segs(jid)
    asr = textout.with_ext(base, ".asr.json")
    if not asr.exists():
        asr.write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    done = set(_review(base).get("edited", []))
    changed = []
    for i in range(max(0, int(data.get("from_i", 0))), len(segs)):
        if i in done:
            continue
        t = segs[i]["text"]
        for w, r in pairs:
            t = t.replace(w, r)
        if t != segs[i]["text"]:
            segs[i]["text"] = t
            changed.append({"i": i, "text": t})
    if changed:
        _rewrite(base, segs)
    return {"ok": True, "changed": changed}


@app.post("/api/jobs/{jid}/judge")
def job_judge(jid: str, name: str = Form(...)):
    """校對結果中每一處修改，再由本機 LLM 判讀是否值得加入詞庫（判讀過的記在 .review.json，不重複判讀）"""
    lst = job_edits(jid, name)
    if not llm.ready() or not load_settings().get("llm_judge", True):
        return {"llm": False, "items": lst}
    base, _ = _job_segs(jid)
    rv = _review(base)
    cache = rv.get("judge", {}) if rv.get("judge_v") == llm.JUDGE_V else {}  # 判讀規則改版時重新判讀
    rv["judge_v"] = llm.JUDGE_V
    todo = [x for x in lst if x["kind"] != "known" and f"{x['wrong']}→{x['right']}" not in cache]
    err = ""
    if todo:
        try:
            res = llm.judge(todo)
            for k, x in enumerate(todo):
                if k in res:
                    cache[f"{x['wrong']}→{x['right']}"] = res[k]
            rv["judge"] = cache
            _save_review(base, rv)
        except Exception as e:
            err = str(e)
    for x in lst:
        j = cache.get(f"{x['wrong']}→{x['right']}")
        if j and x["kind"] != "known":
            x["likely"] = j["add"]
            x["why"] = "LLM：" + (j["why"] or ("像專有名詞" if j["add"] else "一般用字"))
            t = j["term"]
            if j["add"] and t and t != x["right"] and x["kind"] == "new" and x["right"] in t:
                # LLM 建議的完整詞＝前文＋正確寫法＋後文（例：電信 → 中華電信）：誤寫也一起帶上前後文
                L, R = t.split(x["right"], 1)
                if x.get("pre", "").endswith(L) and x.get("post", "").startswith(R):
                    x["wrong"], x["right"] = L + x["wrong"] + R, t
                    x["pre"], x["post"] = x["pre"][:len(x["pre"]) - len(L)], x["post"][len(R):]
            if j["flag"]:
                x["risky"] = True
    return {"llm": True, "items": lst, "error": err}


@app.get("/api/llm")
def llm_status():
    return llm.status()


@app.post("/api/llm/download")
def llm_download(key: str = Form("")):
    return llm.start_download(key or None)


@app.post("/api/llm/test")
def llm_test():
    try:
        return llm.test()
    except Exception as e:
        raise HTTPException(500, f"本機 LLM 測試失敗：{e}")


@app.post("/api/llm/settings")
def llm_settings(model: str = Form(""), judge: str = Form("")):
    data = load_settings()
    if model in llm.MODELS:
        data["llm_model"] = model
    if judge in ("0", "1"):
        data["llm_judge"] = judge == "1"
    save_settings(data)
    _plan_refresh()
    return llm.status()


@app.post("/api/glossary/{name}/add")
async def glossary_add(name: str, request: Request):
    """批次加入詞庫：{items:[{term, wrong, flag}]}"""
    data = await request.json()
    res = {"mis": 0, "new": 0, "exists": 0}
    try:
        for it in data.get("items", [])[:300]:
            res[glossary.add_entry(name, str(it.get("term", "")), str(it.get("wrong", "")), bool(it.get("flag")))] += 1
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return res


@app.post("/api/glossary/{name}/miswrite")
def glossary_miswrite(name: str, term: str = Form(...), wrong: str = Form(...)):
    """把量測找到的誤聽寫法加進詞庫的「常見誤寫」"""
    try:
        ok = glossary.add_miswrite(name, term, wrong)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    if not ok:
        raise HTTPException(400, "沒有加入（詞庫找不到這個詞，或已經有這個誤寫）")
    return {"ok": True}


# ------------------------------------------------------------------ 標籤（Hashtag）與批次處理（0.10.2）
def _norm_tag(t: str) -> str:
    t = re.sub(r"\s+", "", str(t or "")).lstrip("#＃")
    t = re.sub(r"[,，、;；]", "", t)
    return t[:20]


def _read_tags(d: Path | None) -> list[str]:
    """標籤存在各任務資料夾的 tags.json（資料夾搬到哪，標籤跟到哪）"""
    try:
        return [t for t in json.loads((d / "tags.json").read_text(encoding="utf-8")) if isinstance(t, str)]
    except Exception:
        return []


def _write_tags(d: Path, tags: list[str]) -> None:
    f = d / "tags.json"
    if tags:
        f.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")
    elif f.exists():
        f.unlink()  # 程式自己產生的標籤檔，標籤清空時移除


@app.post("/api/jobs/tags")
async def update_tags(request: Request):
    """{ids:[…], add:[…], remove:[…]}：加入／移除標籤（每個任務最多 10 個）"""
    data = await request.json()
    add = [t for t in (_norm_tag(x) for x in data.get("add", [])) if t]
    rem = {t for t in (_norm_tag(x) for x in data.get("remove", [])) if t}
    n = 0
    for jid in data.get("ids", [])[:500]:
        try:
            d = _job_dir(str(jid))
        except HTTPException:
            continue
        cur = [t for t in _read_tags(d) if t not in rem]
        for t in add:
            if t not in cur:
                cur.append(t)
        _write_tags(d, cur[:10])
        n += 1
    return {"ok": True, "count": n}


@app.get("/api/tags")
def all_tags():
    """所有用過的標籤與次數（輸入時提示，避免同義不同寫法）"""
    cnt: dict[str, int] = {}
    for base in task_bases():
        for d in base.iterdir():
            if d.is_dir():
                for t in _read_tags(d):
                    cnt[t] = cnt.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))]


@app.post("/api/jobs/batch")
async def batch_jobs(request: Request):
    """批次處理：{action: diarize|translate|delete, ids:[…], speakers?, src?}；處理中或不符合的任務略過"""
    data = await request.json()
    action, ids = data.get("action"), [str(x) for x in data.get("ids", [])][:500]
    ok, skipped = [], []
    for jid in ids:
        try:
            if action == "diarize":
                diarize_job(jid, speakers=str(data.get("speakers") or "auto"))
            elif action == "translate":
                translate_job(jid, src="auto")
            elif action == "delete":
                delete_job(jid)
            else:
                raise HTTPException(400, "不支援的批次動作")
            ok.append(jid)
        except HTTPException as e:
            skipped.append({"id": jid, "reason": e.detail})
    return {"ok": len(ok), "skipped": skipped}


@app.get("/api/jobs-zip")
def jobs_zip(ids: str):
    """把選取任務的字幕打包下載（主要 SRT，有雙語／中文字幕時一併放入），每個任務一個資料夾"""
    import zipfile
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for jid in [i for i in ids.split(",") if i][:500]:
            try:
                d = _job_dir(jid)
            except HTTPException:
                continue
            srt = _main_srt(d)
            if not srt:
                continue
            for f in (srt, textout_with(srt, "txt"), textout_with(srt, "bilingual.srt"), textout_with(srt, "zh-TW.srt")):
                if f.exists():
                    z.write(f, f"{d.name}/{f.name}")
                    n += 1
    if not n:
        raise HTTPException(404, "選取的任務沒有字幕可下載")
    name = f"逐字稿_{datetime.now():%Y%m%d_%H%M}.zip"
    from urllib.parse import quote
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(name)}"})


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    """刪除任務資料夾（<MEDIA_ROOT>\\uploads\\<任務> 內的字幕、MP3、網頁上傳的原始檔）。
    只刪程式自己在 uploads 底下產生的資料夾；命令列轉檔時指定的原始檔在別處，不會動到。"""
    d = _job_dir(jid)
    j = _jobs.get(jid)
    if j and j.get("state") in ("queued", "running"):
        raise HTTPException(409, "任務還在處理中，請先取消再刪除")
    shutil.rmtree(d)
    _jobs.pop(jid, None)
    hidden = _hidden()
    if jid in hidden:
        hidden.discard(jid)
        _hidden_file().write_text(json.dumps(sorted(hidden), ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


# 瀏覽器能直接播放的格式；其他格式（mkv、avi、wmv…）改播轉好的 MP3
PLAY_VIDEO = {".mp4", ".m4v", ".webm", ".mov"}
PLAY_AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac", ".webm"}


def _media_file(d: Path) -> tuple[Path | None, str]:
    """回傳 (播放用檔案, 'video'|'audio')：優先原始檔（影片才看得到畫面），不行就用 MP3"""
    src = None
    try:
        r = json.loads((d / "job.json").read_text(encoding="utf-8"))
        p = Path(r["source"])
        if p.exists():
            src = p
    except Exception:
        pass
    if src is None:  # 網頁上傳的原始檔放在任務資料夾
        src = next((f for f in d.iterdir() if f.suffix.lower() in EXTS and f.suffix.lower() != ".mp3"), None)
    if src and src.suffix.lower() in PLAY_VIDEO:
        try:
            if not audio.probe(src)["has_video"]:  # 例如網址下載的 webm 只有聲音
                return src, "audio"
        except Exception:
            pass
        return src, "video"
    if src and src.suffix.lower() in PLAY_AUDIO:
        return src, "audio"
    mp3 = next(iter(d.glob("*.mp3")), None)
    return mp3, "audio"


@app.get("/api/jobs/{jid}/media")
def media(jid: str):
    f, _ = _media_file(_job_dir(jid))
    if not f:
        raise HTTPException(404, "找不到可播放的檔案")
    return FileResponse(f)  # 支援 Range，可拖動進度


@app.get("/api/jobs/{jid}/subtitle.vtt")
def subtitle_vtt(jid: str):
    """SRT 轉 WebVTT，給影片播放器疊字幕（全螢幕也看得到）"""
    d = _job_dir(jid)
    srt = _main_srt(d)
    if not srt:
        raise HTTPException(404, "還沒有字幕")
    body = srt.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", body)
    return Response("WEBVTT\n\n" + body, media_type="text/vtt; charset=utf-8")


@app.get("/api/jobs/{jid}/download/{kind}")
def download(jid: str, kind: str):
    d = _job_dir(jid)
    if kind not in ("mp3", "srt", "txt", "raw", "audio", "video", "zh", "bi", "tidy"):
        raise HTTPException(400, "只能下載 mp3、srt、txt、原始錄音、影片、中文字幕、雙語字幕、整理稿")
    if kind in ("txt", "tidy"):  # 整理過的任務，純文字檔本身就是整理稿
        srt = _main_srt(d)
        if srt:
            tidy.load(srt.with_suffix(""))  # 舊版另存的整理稿先搬過來
        f = _main_file(d, "txt")
    elif kind == "raw":  # 一句一行的版本：由時間軸即時產生（整理過的任務已沒有這個檔）
        from urllib.parse import quote
        srt = _main_srt(d)
        jf = textout_with(srt, "json") if srt else None
        if not jf or not jf.exists():
            raise HTTPException(404, "檔案不存在")
        segs = json.loads(jf.read_text(encoding="utf-8"))
        name = srt.with_suffix("").name + ".一句一行.txt"
        return Response(("\ufeff" + "\n".join(x["text"] for x in segs) + "\n").encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(name)}", "Cache-Control": "no-store"})
    elif kind in ("zh", "bi"):
        srt = _main_srt(d)
        f = textout_with(srt, "zh-TW.srt" if kind == "zh" else "bilingual.srt") if srt else None
        f = f if f and f.exists() else None
    elif kind in ("audio", "video"):
        f, _ = _media_file(d)
    else:
        f = _main_file(d, kind)
    if not f:
        raise HTTPException(404, "檔案不存在")
    nc = {"Cache-Control": "no-store"} if kind in ("srt", "txt", "raw", "zh", "bi") else None  # 文字檔校對、整理後會變，不用瀏覽器快取
    return FileResponse(f, filename=f.name, headers=nc)


@app.post("/api/jobs/{jid}/open")
def open_folder(jid: str):
    d = _job_dir(jid)
    if os.name == "nt":
        os.startfile(d)
    return {"ok": True, "path": str(d)}


# ------------------------------------------------------------------ 設定
def _free_gb(p: Path) -> float | None:
    try:
        q = p
        while not q.exists() and q != q.parent:
            q = q.parent
        return round(shutil.disk_usage(q).free / 1024 ** 3, 1)
    except Exception:
        return None


def _busy() -> bool:
    return any(j.get("state") in ("queued", "running") for j in _jobs.values()) or \
        _bench["state"] in ("queued", "running")


@app.get("/api/settings")
def get_settings():
    root, src = media_root_source()
    legacy = legacy_jobs_dir()
    prev = load_settings().get("previous_media_root") or ""
    prev_n = sum(count_tasks(Path(prev) / s) for s in ("uploads", "downloads")) if prev and Path(prev) != root else 0
    return {
        "media_root": str(root), "source": src, "default": DEFAULT_MEDIA_ROOT,
        "env": os.environ.get("MEDIA_ROOT", ""),
        "free_gb": _free_gb(root),
        "dirs": {"uploads": str(root / "uploads"), "benchmark": str(root / "benchmark"), "downloads": str(root / "downloads")},
        "tasks": count_tasks(root / "uploads") + count_tasks(root / "downloads"),
        "legacy": {"path": str(legacy), "tasks": count_tasks(legacy)},
        "previous": {"path": prev, "tasks": prev_n} if prev_n else None,
        "busy": _busy(),
        "models": _models_info(),
        "machine": _machine(),
        "llm": llm.status(),
        "login": downloader.cookies_status() | {"paired": load_settings().get("ext_id") or "",
                                                "ext_dir": str(EXT_DIR)},
    }


def _models_info() -> dict:
    root, src = models_root_source()
    items = [
        {"key": "breeze", "label": "Breeze-ASR-25（轉逐字稿，臺灣華語）", "path": str(ov_model_dir("breeze")),
         "ready": (ov_model_dir("breeze") / "openvino_encoder_model.xml").exists(), "hint": "執行 setup.cmd（會從共用來源複製）"},
        {"key": "turbo", "label": "Whisper large-v3-turbo（轉逐字稿，較快）", "path": str(ov_model_dir("turbo")),
         "ready": (ov_model_dir("turbo") / "openvino_encoder_model.xml").exists(), "hint": "執行 setup.cmd（會從共用來源複製）"},
        {"key": "diarize", "label": "pyannote（分辨說話者）", "path": str(diarize.model_dir()),
         "ready": diarize.model_ready(), "hint": "請管理者執行 admin-download-pyannote.cmd，或複製 D:\\models\\diarization"},
        {"key": "translate", "label": "TranslateGemma 4B（翻譯成中文）", "path": str(translate.model_dir()),
         "ready": translate.ready(), "hint": "請管理者執行 admin-download-translator.cmd，或複製 D:\\models\\llm"},
        {"key": "llm", "label": f"本機 LLM {llm.MODELS[llm.current()]['label']}（詞庫判讀、整理逐字稿）", "path": str(llm.model_dir()),
         "ready": llm.ready(), "hint": "在下方「本機 LLM」按下載"},
    ]
    return {"root": str(root), "source": src, "default": DEFAULT_MODELS_ROOT, "env": os.environ.get("MODELS_ROOT", ""),
            "free_gb": _free_gb(root), "items": items, "pending_restart": bool(load_settings().get("models_restart"))}


# ------------------------------------------------------------------ Chrome 擴充功能「逐字稿登入同步」（0.10.0）
EXT_DIR = ROOT / "chrome_extension"


def _ext_check(request: Request) -> str:
    """只接受 Chrome 擴充功能送來的請求（網頁無法偽造 Origin）；第一次配對後固定只認那一個擴充功能"""
    origin = request.headers.get("origin") or ""
    if not origin.startswith("chrome-extension://"):
        raise HTTPException(403, "只接受「逐字稿登入同步」擴充功能的請求")
    ext_id = origin[len("chrome-extension://"):].strip("/")
    data = load_settings()
    paired = data.get("ext_id")
    if not paired:
        data["ext_id"] = ext_id
        save_settings(data)
    elif paired != ext_id:
        raise HTTPException(403, "這個擴充功能尚未配對：請到網頁「參數設定」按「解除配對」後再同步一次")
    return ext_id


@app.get("/api/ping")
def ext_ping(request: Request):
    """擴充功能檢查程式是否執行中（只回版本與同步時間，不含登入資料）；一般網頁不可呼叫，也不觸發配對"""
    if (request.headers.get("origin") or "").startswith(("http://", "https://")):
        raise HTTPException(403, "只接受「逐字稿登入同步」擴充功能的請求")
    return {"app": "transcribe", "version": version(), "cookies": downloader.cookies_status()}


@app.post("/api/cookies")
async def ext_cookies(request: Request):
    """擴充功能送來的登入資料（JSON：{cookies:[Chrome cookie…], source?}）→ data\\cookies.txt；內容不寫入任何紀錄"""
    ext_id = _ext_check(request)
    body = await request.body()
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(413, "資料太大")
    try:
        data = json.loads(body)
        cookies = data["cookies"]
        assert isinstance(cookies, list)
    except Exception:
        raise HTTPException(400, "格式不正確")
    info = downloader.save_cookies(cookies, {"ext_id": ext_id, "source": str(data.get("source") or "manual")[:20]})
    return {"ok": True, "count": info["count"], "domains": info["domains"], "updated": info["updated"]}


@app.post("/api/enqueue")
async def ext_enqueue(request: Request):
    """擴充功能右鍵「轉逐字稿」：{urls:[…], speakers?} → 每個網址建立任務（設定沿用本機建議）"""
    _ext_check(request)
    try:
        data = await request.json()
        urls = [str(u) for u in data.get("urls", [])][:10]
    except Exception:
        raise HTTPException(400, "格式不正確")
    out = []
    for u in urls:
        try:
            j = await asyncio.to_thread(_enqueue_url, u, "", "auto", str(data.get("speakers") or ""))
            out.append({"url": u, "ok": True, "name": j["name"], "id": j["id"]})
        except downloader.DownloadError as e:
            out.append({"url": u, "ok": False, "error": str(e)})
    return {"results": out}


@app.post("/api/login/unpair")
def login_unpair():
    data = load_settings()
    data.pop("ext_id", None)
    save_settings(data)
    return {"ok": True}


@app.post("/api/login/clear")
def login_clear():
    """刪除同步過來的登入資料（使用者在參數設定按「刪除登入資料」並確認）"""
    downloader.clear_cookies()
    return {"ok": True}


# ------------------------------------------------------------------ 這台電腦：別名、功能、模型配置方案（0.14.0）
def _plan_refresh() -> None:
    """設定或實測結果改變後，重寫 env-reports\\<電腦>.models.json（失敗不影響原本的動作）"""
    try:
        modelplan.write()
    except Exception as e:
        print(f"更新模型配置方案失敗：{e}")


def _machine() -> dict:
    try:
        plan = modelplan.compute()
    except Exception as e:  # 方案計算失敗不可讓設定頁打不開
        return {"error": str(e), "prep": dict(modelplan.prep)}
    return plan | {"prep": dict(modelplan.prep), "feature_info": modelplan.manifest()["features"]}


@app.get("/api/machine")
def get_machine():
    return _machine()


@app.post("/api/machine")
def set_machine(alias: str = Form(None), features: str = Form(None)):
    """修改電腦別名與要用的功能；功能改變後，缺少且能自動取得的模型立刻在背景準備"""
    feats = None if features is None else [f for f in features.split(",") if f in modelplan.OPTIONAL]
    modelplan.save_choice(features=feats, alias_text=alias)
    plan = modelplan.write()
    if feats is not None and plan["missing"]:
        modelplan.start_prepare(log=print)
    return _machine()


@app.post("/api/machine/prepare")
def machine_prepare():
    st = modelplan.start_prepare(log=print)
    if st.get("state") != "running":
        plan = modelplan.compute()
        if plan["missing"]:
            raise HTTPException(400, "缺少的模型沒辦法自動取得：" + "；".join(
                f"{plan['models'][m]['label']}：{plan['models'][m]['how']}" for m in plan["missing"]))
    return _machine()


@app.post("/api/settings/models")
def set_models_root(path: str = Form("")):
    """修改模型位置（空白＝回到環境變數或 D:\\models）。需重新開啟 start.cmd 才生效（已載入的模型不會自動更換）"""
    if _busy():
        raise HTTPException(409, "還有任務或效能實測在進行，請等完成後再修改位置")
    data = load_settings()
    v = path.strip().strip('"')
    if v:
        p = Path(v)
        if not p.is_absolute() or (os.name == "nt" and not p.drive):
            raise HTTPException(400, "請輸入完整路徑，例如 D:\\models 或 E:\\models")
        if not p.is_dir():
            raise HTTPException(400, f"找不到這個資料夾：{p}")
        ok = [n for n in ("breeze-asr-25", "large-v3-turbo") if (p / "asr" / "ov" / n / "openvino_encoder_model.xml").exists()]
        if not ok:
            raise HTTPException(400, f"這個位置找不到辨識模型（應該有 {p}\\asr\\ov\\breeze-asr-25）。請確認是完整複製的 models 資料夾。")
        data["models_root"] = str(p)
    else:
        data.pop("models_root", None)
    old = str(models_root())
    data["models_restart"] = True
    save_settings(data)
    return {"ok": True, "models_root": str(models_root()), "old": old,
            "note": "已儲存。請關閉 start.cmd 視窗再重新開啟，新的模型位置才會生效。"}


@app.post("/api/settings")
def set_settings(media_root_path: str = Form("", alias="media_root")):
    """修改媒體檔位置。空白＝回到預設（環境變數 MEDIA_ROOT 或 D:\\mediafiles）。
    不會搬移或刪除舊位置的檔案。"""
    if _busy():
        raise HTTPException(409, "還有任務或效能實測在進行，請等完成後再修改位置")
    old = media_root()
    data = load_settings()
    v = media_root_path.strip().strip('"')
    if v:
        p = Path(v)
        if not p.is_absolute() or (os.name == "nt" and not p.drive):
            raise HTTPException(400, "請輸入完整路徑，例如 D:\\mediafiles 或 E:\\逐字稿檔案")
        if not Path(p.anchor).exists():
            raise HTTPException(400, f"找不到 {p.anchor} 磁碟")
        try:
            for sub in ("uploads", "benchmark", "downloads"):
                (p / sub).mkdir(parents=True, exist_ok=True)
            t = p / f".write_test_{uuid.uuid4().hex[:6]}"
            t.write_text("ok", encoding="utf-8")
            t.unlink()  # 只刪自己剛建立的測試檔
        except Exception as e:
            raise HTTPException(400, f"這個位置無法寫入：{e}")
        data["media_root"] = str(p)
    else:
        data.pop("media_root", None)
    save_settings(data)
    new = media_root_source()[0]
    if new != old:
        data["previous_media_root"] = str(old)
        save_settings(data)
    for d in (uploads_dir, benchmark_dir, downloads_dir):
        d()
    left = (count_tasks(old / "uploads") + count_tasks(old / "downloads")) if new != old else 0
    return {"ok": True, "media_root": str(new), "old": str(old), "old_tasks": left}


@app.get("/api/benchmark")
def bench_status():
    data = None
    if benchmark_file().exists():
        try:
            data = json.loads(benchmark_file().read_text(encoding="utf-8"))
        except Exception:
            data = None
    return {"state": _bench["state"], "log": _bench["log"][-300:], "error": _bench["error"],
            "started": _bench["started"], "finished": _bench["finished"], "result": data,
            "queued_jobs": _tasks.qsize()}


@app.post("/api/benchmark")
def bench_start(long: bool = Form(True)):
    if _bench["state"] in ("running", "queued"):
        raise HTTPException(409, "效能實測已在進行中")
    _bench.update(state="queued", log=["等待目前的轉檔任務完成後開始…"] if _tasks.qsize() else [], error="")
    _tasks.put(("bench", "long" if long else "short"))
    return {"ok": True}


@app.middleware("http")
async def _no_cache(request, call_next):
    """網頁檔（html／js／css）不讓瀏覽器快取：程式更新後重新整理就是新版畫面"""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")


def main():
    import uvicorn
    data = load_settings()  # 重新開啟後，「需要重新開啟」的提示就可以拿掉
    if data.pop("models_restart", None):
        save_settings(data)
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=downloader.maybe_update, daemon=True).start()  # 一天最多一次，不拖慢啟動
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"影音轉逐字稿 v{version()} 已啟動：{url}")
    print("要結束程式，請直接關閉這個視窗。")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
