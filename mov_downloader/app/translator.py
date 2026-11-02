"""字幕翻譯引擎：Google（免費）或 AI 大型語言模型（OpenAI／Azure OpenAI 相容 API）。

AI 翻譯的連線資訊放在專案根目錄的 .env（不會寫進 settings.json，也不會傳給前端）：

    LLM_ENDPOINT=https://xxx.azure-api.net/xxx
    LLM_API_KEY=你的金鑰
    LLM_MODEL=部署名稱或模型名稱
    LLM_API_STYLE=azure          # azure 或 openai
    LLM_API_VERSION=2024-10-21   # 僅 azure 需要
    LLM_KEY_HEADER=api-key       # APIM 閘道常用 Ocp-Apim-Subscription-Key
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .fsutil import atomic_write_json

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


# ------------------------------------------------------------------ .env
def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            else:
                v = v.split(" #")[0].strip()
            env[k.strip()] = v
    # 系統環境變數優先
    for k in list(env) + ["LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL", "LLM_API_STYLE",
                          "LLM_API_VERSION", "LLM_KEY_HEADER", "LLM_REASONING_EFFORT", "LLM_CONCURRENCY"]:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


PLACEHOLDER = re.compile(r"your-gateway|your-path|<.*>|xxx", re.I)


def llm_problem() -> str:
    """回傳 .env 設定的問題說明；沒問題回傳空字串。"""
    e = load_env()
    if not ENV_FILE.exists() and not os.environ.get("LLM_ENDPOINT"):
        return "找不到 .env 檔"
    miss = [k for k in ("LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL") if not e.get(k)]
    if miss:
        return "尚未填寫：" + "、".join(miss)
    if PLACEHOLDER.search(e["LLM_ENDPOINT"]):
        return "LLM_ENDPOINT 還是範例網址，請改成你實際的 API 端點"
    if not re.match(r"^https?://", e["LLM_ENDPOINT"]):
        return "LLM_ENDPOINT 必須以 https:// 開頭"
    return ""


def normalize_endpoint(endpoint: str, style: str) -> tuple[str, str, str]:
    """自動修正常見的端點寫法，回傳（端點, 格式, 說明）。

    - Azure AI Foundry 專案端點 https://x.services.ai.azure.com/api/projects/<專案>
      不是聊天 API，改用同主機的 OpenAI v1 端點 https://x.services.ai.azure.com/openai/v1
    - 端點已是 .../openai/v1 → 一律用 openai 格式（v1 不接受 api-version）
    - 端點誤填到 /openai 為止（azure 格式）→ 去掉結尾的 /openai，避免重複
    """
    ep = endpoint.rstrip("/")
    m = re.match(r"^(https?://[^/]+)/api/projects/[^/]+", ep, re.I)
    if m:
        return m.group(1) + "/openai/v1", "openai", "Foundry 專案端點已自動改用 /openai/v1"
    if re.search(r"/openai/v1$", ep, re.I):
        return ep, "openai", ""
    if style == "azure" and re.search(r"/openai$", ep, re.I):
        return ep[:-len("/openai")], style, ""
    return ep, style, ""


def llm_config() -> dict | None:
    e = load_env()
    if llm_problem():
        return None
    endpoint, style, note = normalize_endpoint(e["LLM_ENDPOINT"], (e.get("LLM_API_STYLE") or "azure").lower())
    return {
        "endpoint": endpoint,
        "note": note,
        "key": e["LLM_API_KEY"],
        "model": e["LLM_MODEL"],
        "style": style,
        "version": e.get("LLM_API_VERSION") or "2024-10-21",
        "header": e.get("LLM_KEY_HEADER") or "api-key",
        "effort": e.get("LLM_REASONING_EFFORT") or "",
        "concurrency": max(1, min(8, int(e.get("LLM_CONCURRENCY") or 4))),
    }


def llm_status() -> dict:
    c = llm_config()
    if not c:
        return {"configured": False, "problem": llm_problem()}
    host = re.sub(r"^https?://", "", c["endpoint"]).split("/")[0]
    return {"configured": True, "model": c["model"], "host": host, "style": c["style"],
            "mask": mask_words(), "mask_learned": _learned_masks()}


# ------------------------------------------------------------------ 遮蔽詞
# 有些 Azure 部署設了「封鎖清單」（例如競品名稱 Claude），只要送出或回應裡出現就被內容篩選擋下。
# 送出前把這些詞換成代號 ZZQ1、ZZQ2…，收到回應再換回來；AI 完全看不到原詞，也不會輸出原詞。
MASK_FILE = ROOT / "data" / "llm_mask.json"
_mask_lock = __import__("threading").Lock()


def _learned_masks() -> list[str]:
    try:
        return [w for w in json.loads(MASK_FILE.read_text(encoding="utf-8")) if isinstance(w, str) and w.strip()]
    except Exception:
        return []


def mask_words() -> list[str]:
    """.env 的 LLM_MASK_WORDS（逗號分隔）＋程式自動學到的詞"""
    env = re.split(r"[,，;；、]", load_env().get("LLM_MASK_WORDS", ""))
    out: list[str] = []
    for w in [*env, *_learned_masks()]:
        w = w.strip()
        if w and w.lower() not in {x.lower() for x in out}:
            out.append(w)
    return out


def is_blocked_word(word: str) -> bool:
    """單獨送出這個詞，看會不會被 Azure 內容篩選擋下（確認真的是這個詞的問題）"""
    cfg = llm_config()
    if not cfg:
        return False
    try:
        _llm_call(cfg, [{"role": "user", "content": f"請只回覆 JSON {{\"ok\": true}}。關鍵字：{word}"}], timeout=60)
        return False
    except ContentFiltered:
        return True
    except Exception:
        return False


_probed: dict[str, bool] = {}


def learn_mask(word: str) -> bool:
    """確認這個詞單獨送出也會被擋，才記成遮蔽詞；新學到回傳 True"""
    word = word.strip().strip(".,!?，。！？:：")
    if not re.fullmatch(r"[A-Za-z][\w.+\-]{1,29}", word):
        return False
    if word.lower() in {x.lower() for x in mask_words()}:
        return False
    if word.lower() not in _probed:
        _probed[word.lower()] = is_blocked_word(word)
    if not _probed[word.lower()]:
        return False
    with _mask_lock:
        cur = _learned_masks()
        if word.lower() in {x.lower() for x in mask_words()}:
            return False
        cur.append(word)
        MASK_FILE.parent.mkdir(exist_ok=True)
        atomic_write_json(MASK_FILE, cur)
    _log(f"自動加入遮蔽詞「{word}」：這個詞會被 Azure 內容篩選擋下，之後送給 AI 前會先換成代號")
    return True


def learn_from_text(text: str, limit: int = 6) -> list[str]:
    """一句被擋時，逐一測試其中的英文專有名詞（大寫開頭優先），找出被封鎖的詞並記成遮蔽詞"""
    words = list(dict.fromkeys(re.findall(r"[A-Za-z][\w.+\-]{1,29}", text or "")))
    words = sorted(words, key=lambda w: (not w[0].isupper(), words.index(w)))
    return [w for w in words[:limit] if learn_mask(w)]


def _mask_re(words: list[str]):
    alts = sorted(words, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(w) for w in alts) + r")(?![A-Za-z0-9])", re.I)


MASK_NOTE = "\n\n注意：文字中「ZZQ」加數字（例如 ZZQ1）是專有名詞的代號，請原樣保留，不可翻譯、拆開或改寫。"


def _apply_mask(messages: list[dict], words: list[str]) -> list[dict]:
    rx = _mask_re(words)
    idx = {w.lower(): i + 1 for i, w in enumerate(words)}
    out = []
    for m in messages:
        c = rx.sub(lambda mm: f"ZZQ{idx[mm.group(1).lower()]}", m["content"])
        out.append({**m, "content": c})
    if out and out[0]["role"] == "system":
        out[0] = {**out[0], "content": out[0]["content"] + MASK_NOTE}
    else:
        out.insert(0, {"role": "system", "content": MASK_NOTE.strip()})
    return out


def _unmask(text: str, words: list[str]) -> str:
    return re.sub(r"ZZQ(\d+)", lambda m: words[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(words) else m.group(0),
                  text or "")


# ------------------------------------------------------------------ Google
def _google_batches(texts: list[str]) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(texts):
        size, j = 0, i
        while j < len(texts) and size + len(texts[j]) + 1 < 4000 and j - i < 80:
            size += len(texts[j]) + 1
            j += 1
        j = max(j, i + 1)
        out.append((i, j))
        i = j
    return out


def translate_google(texts: list[str], target="zh-TW", progress=None) -> list[str]:
    from deep_translator import GoogleTranslator

    results = list(texts)
    batches = _google_batches(texts)
    done = [0]

    def work(b):
        i, j = b
        tr = GoogleTranslator(source="auto", target=target)
        chunk = [t[:4000] for t in texts[i:j]]
        try:
            out = tr.translate("\n".join(chunk)) or ""
            lines = out.split("\n")
        except Exception:
            lines = []
        if len(lines) == len(chunk):
            results[i:j] = lines
        else:
            for k in range(i, j):
                try:
                    results[k] = tr.translate(texts[k]) or texts[k]
                except Exception:
                    pass
        done[0] += 1
        if progress:
            progress(done[0] / len(batches), f"{done[0]}/{len(batches)} 批")

    with ThreadPoolExecutor(max_workers=4) as ex:   # 平行翻譯，速度約為原本 3～4 倍
        list(ex.map(work, batches))
    return results


# ------------------------------------------------------------------ AI（LLM）
SYSTEM_PROMPT = """你是專業的影片字幕翻譯員。請把使用者提供的字幕逐句翻譯成「台灣正體中文」。
規則：
1. 使用台灣慣用詞（例：軟體、程式、資料、網路、影片、使用者、伺服器），絕對不可使用簡體字或中國用語。
2. 字幕是語音辨識結果，句子可能被切斷在不同行；請參考前後文讓每行意思通順，但每一行仍須對應原本那一行，不可合併、拆分或增減行數。
3. 產品名稱、程式碼、API、指令、人名、品牌維持原文（例：WebMCP、Chrome、GitHub）。
4. 口語、簡潔，適合當字幕閱讀；不要加註解或解釋。
5. 輸入是 JSON 物件，鍵是行號、值是原文。只輸出 JSON：{"t": {"行號": "譯文", ...}}，
   每個輸入的行號都必須出現且只出現一次，不可省略任何一行；該行若只是語助詞也要翻（例如 "uh" → "呃"）。"""


class ContentFiltered(RuntimeError):
    """Azure 內容篩選擋下這次請求（通常是誤判）；只影響這一批，不應讓整份翻譯失敗"""


_DROPPED: set[str] = set()   # API 不支援、已自動移除的參數（response_format / reasoning_effort）


def _build_request(cfg: dict, messages: list[dict]) -> tuple[str, dict, dict]:
    if cfg["style"] == "azure":
        url = f"{cfg['endpoint']}/openai/deployments/{cfg['model']}/chat/completions?api-version={cfg['version']}"
    else:  # openai 相容（含 Azure OpenAI v1：endpoint 填到 .../openai/v1）
        url = f"{cfg['endpoint']}/chat/completions"
    # 一律帶 model：部分 API 閘道會用 body 的 model 欄位檢查可用模型
    body = {"model": cfg["model"], "messages": messages}
    if "response_format" not in _DROPPED:
        body["response_format"] = {"type": "json_object"}
    if cfg["effort"] and "reasoning_effort" not in _DROPPED:
        body["reasoning_effort"] = cfg["effort"]
    headers = {"Content-Type": "application/json"}
    if cfg["header"].lower() == "authorization":
        headers["Authorization"] = f"Bearer {cfg['key']}"
    else:
        headers[cfg["header"]] = cfg["key"]
    return url, headers, body


def explain_http_error(status: int, text: str, cfg: dict, url: str) -> str:
    """把 API 錯誤轉成中文說明與建議。"""
    code, msg = "", text[:300]
    try:
        err = json.loads(text).get("error") or {}
        code, msg = err.get("code") or "", err.get("message") or msg
    except Exception:
        pass
    if code == "ModelNotAllowed" or "模型" in msg:
        m = re.search(r"(gpt[\w.\-]+|o\d[\w.\-]*|[\w.\-]+-\d[\w.\-]*)", msg)
        hint = f"請把 .env 的 LLM_MODEL 改成 {m.group(1)}" if m else "請確認 .env 的 LLM_MODEL"
        return f"模型不被允許（{code or status}）：{msg}。{hint}，目前設定為「{cfg['model']}」"
    if status in (401, 403):
        return f"拒絕存取（HTTP {status} {code}）：{msg}。請確認 LLM_API_KEY 與 LLM_KEY_HEADER（目前標頭：{cfg['header']}）"
    if status == 404:
        return (f"找不到 API 路徑（HTTP 404 {code}）：{msg}。請確認 LLM_ENDPOINT、LLM_MODEL 與 LLM_API_STYLE"
                f"（目前 {cfg['style']}）。實際呼叫：{url.split('?')[0]}")
    if code == "content_filter" or "content_filter" in text:
        return (f"被 Azure 內容篩選擋下（HTTP {status} content_filter）：字幕或影片簡介中有內容被判定為違規或提示詞攻擊，"
                f"多半是誤判。被擋的句子已自動改用 Google 翻譯；若常發生，可在 Azure AI Foundry 為此部署建立較寬鬆的內容篩選設定")
    if "api version" in msg.lower() or "api-version" in msg.lower():
        return (f"API 版本不支援（HTTP {status} {code}）：{msg}。建議改用 v1 端點：.env 設 "
                f"LLM_ENDPOINT=https://<主機>/openai/v1、LLM_API_STYLE=openai；"
                f"或把 LLM_API_VERSION 改成該服務支援的版本（目前 {cfg['version']}）")
    return f"API 錯誤（HTTP {status} {code}）：{msg}"


def _llm_call(cfg: dict, messages: list[dict], timeout=120) -> str:
    if cfg.get("engine") == "local":           # 本機 Gemma 4（1.25.0）：在這台電腦上執行，不需要遮蔽詞
        from . import local_llm
        try:
            return local_llm.chat(messages)
        except local_llm.LocalLLMError as e:
            raise RuntimeError(f"本機 AI 失敗：{e}") from None
    if cfg.get("engine") == "codex":           # 本機 Codex CLI：不經過 Azure，也不需要遮蔽詞
        from . import codex_cli
        try:
            return codex_cli.chat(messages, cfg["exe"], cfg.get("model") or "", timeout=max(timeout, 300))
        except codex_cli.CodexError as e:
            raise RuntimeError(f"AI（Codex）失敗：{e}") from None
    import requests

    last = None
    host = re.sub(r"^https?://", "", cfg["endpoint"]).split("/")[0]
    words = mask_words()
    if words:
        messages = _apply_mask(messages, words)
    for attempt in range(5):
        url, headers, body = _build_request(cfg, messages)
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            if "NameResolution" in str(e) or "getaddrinfo" in str(e):
                raise RuntimeError(f"AI 翻譯找不到主機 {host}，請確認 .env 的 LLM_ENDPOINT 是否正確（或是否需要連公司 VPN）") from None
            last = "無法連線"
            time.sleep(2 * (attempt + 1))
            continue
        except requests.exceptions.Timeout:
            last = "連線逾時"
            continue
        if r.status_code == 400:
            # 模型不支援某些參數時自動移除後重送
            dropped = False
            for k in ("response_format", "reasoning_effort"):
                if k in r.text and k in body:   # 多執行緒同時遇到時，各自都要重送
                    _DROPPED.add(k)
                    dropped = True
            if dropped:
                continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(float(r.headers.get("retry-after") or 2 * (attempt + 1)))
            continue
        if r.status_code == 400 and "content_filter" in r.text:
            raise ContentFiltered("AI 翻譯" + explain_http_error(r.status_code, r.text, cfg, url))
        if r.status_code >= 400:
            raise RuntimeError("AI 翻譯" + explain_http_error(r.status_code, r.text, cfg, url))
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"AI 翻譯回應不是 JSON（HTTP {r.status_code}）：{r.text[:200]}") from None
        choices = data.get("choices") or []
        if not choices:
            _log(f"AI 回應沒有 choices：{json.dumps(data, ensure_ascii=False)[:300]}")
            return ""
        ch = choices[0]
        content = (ch.get("message") or {}).get("content") or ""
        if isinstance(content, list):   # 部分 API 以片段陣列回傳
            content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
        if not content and ch.get("finish_reason") == "content_filter":
            # AI 的「回應」被內容篩選擋下（例如回應裡出現封鎖清單的詞）→ 當成被擋處理，讓呼叫端拆小或略過
            raise ContentFiltered("AI 翻譯" + explain_http_error(400, '{"error":{"code":"content_filter"}}', cfg, ""))
        if not content:
            _log(f"AI 回應內容為空（finish_reason={ch.get('finish_reason')}）")
        return _unmask(content, words) if words else content
    raise RuntimeError(f"AI 翻譯無法完成（{last}），請稍後再試")


_CJK = re.compile(r"[\u3400-\u9fff]")


def _log(msg: str) -> None:
    from . import errlog
    errlog.warn("翻譯", msg)


def is_translated(src: str, out: str) -> bool:
    """譯文有中文字，或原文本身沒有可翻譯的字母（例如只有數字、符號）"""
    return bool(_CJK.search(out)) or not re.search(r"[^\W\d_]{2,}", src)


def _parse_llm(content: str, ids: list[int]) -> dict[int, str]:
    """解析 AI 回應，回傳 {行號: 譯文}；缺漏的行號不會出現在結果裡（之後補翻）"""
    m = re.search(r"\{.*\}", content, re.S)
    try:
        data = json.loads(m.group(0) if m else content)
    except Exception:
        return {}
    t = data.get("t", data) if isinstance(data, dict) else data
    out: dict[int, str] = {}
    if isinstance(t, dict):
        for k, v in t.items():
            try:
                out[int(str(k).strip())] = str(v)
            except ValueError:
                pass
    elif isinstance(t, list) and len(t) == len(ids):   # 模型改回傳陣列時仍可接受
        out = {i: str(v) for i, v in zip(ids, t)}
    return {k: v for k, v in out.items() if k in ids and v.strip()}


def codex_config() -> dict | None:
    """本機 Codex CLI 的設定（翻譯與重點摘要共用「設定 → 本機 Codex」的路徑與模型）"""
    from . import downloader as dl
    from . import codex_cli
    s = dl.load_settings()
    exe = codex_cli.find_codex(s.get("codex_path") or "")
    if not exe:
        return None
    m = s.get("codex_model") or ""
    return {"engine": "codex", "exe": exe, "model": m, "label": "Codex CLI" + (f"（{m}）" if m else ""),
            "concurrency": 2}


def translate_llm(texts: list[str], context: str = "", progress=None,
                  only: list[int] | None = None, labels: list[str] | None = None, cfg: dict | None = None) -> list[str]:
    """only：只翻這些行（補翻用），其餘保持原樣；labels：每行的時間（記錄被擋的位置用）；cfg：指定用哪個 AI"""
    cfg = cfg or llm_config()
    if not cfg:
        raise RuntimeError(f"AI 翻譯設定有問題：{llm_problem()}")
    todo = list(range(len(texts))) if only is None else sorted(only)
    results = list(texts)
    if not todo:
        return results
    size = 40
    batches = [todo[k:k + size] for k in range(0, len(todo), size)]
    total = len(batches)
    done = [0]
    lock = __import__("threading").Lock()

    filtered: list[int] = []   # 被內容篩選擋下、改用 Google 的行

    def ask(ids: list[int], use_ctx: bool = True) -> dict[int, str]:
        """被 Azure 內容篩選擋下時：先拿掉影片資訊重試，再對半拆小，找出被擋的句子交給 Google"""
        if len(filtered) >= 20:    # 擋太多（可能整體都會被擋）→ 剩下的直接交給 Google，避免狂打 API
            with lock:
                filtered.extend(ids)
            return {}
        try:
            return _ask(ids, use_ctx)
        except ContentFiltered:
            if use_ctx and context:
                return ask(ids, False)
            if len(ids) > 1:
                mid = len(ids) // 2
                out = ask(ids[:mid], False)
                out.update(ask(ids[mid:], False))
                return out
            if learn_from_text(texts[ids[0]]):         # 找到被封鎖的詞 → 記成遮蔽詞後重試
                try:
                    return _ask(ids, False)
                except ContentFiltered:
                    pass
            with lock:
                filtered.append(ids[0])
            at = f"（{labels[ids[0]]}）" if labels and ids[0] < len(labels) else ""
            _log(f"第 {ids[0] + 1} 行{at}被 Azure 內容篩選擋下，改用 Google 翻譯：{texts[ids[0]][:120]!r}")
            return {}

    def _ask(ids: list[int], use_ctx: bool = True) -> dict[int, str]:
        first = ids[0]
        prev = [texts[k] for k in range(max(0, first - 3), first)] if use_ctx else []   # 前文幫助理解斷句
        nxt = [texts[k] for k in range(ids[-1] + 1, min(len(texts), ids[-1] + 3))] if use_ctx else []
        user = ""
        if context and use_ctx:
            user += f"影片資訊（僅供理解用語）：\n{context[:1500]}\n\n"
        if prev:
            user += "前文（不用翻譯）：\n" + "\n".join(prev) + "\n\n"
        if nxt:
            user += "後文（不用翻譯）：\n" + "\n".join(nxt) + "\n\n"
        payload = {str(k + 1): texts[k] for k in ids}
        user += f"請翻譯以下 {len(ids)} 行（JSON，鍵為行號）：\n" + json.dumps(payload, ensure_ascii=False)
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        raw = _llm_call(cfg, msgs)   # 連線／權限錯誤直接往上拋
        got = _parse_llm(raw, [k + 1 for k in ids])
        res = {k - 1: v for k, v in got.items() if is_translated(texts[k - 1], v)}
        miss = len(ids) - len(res)
        if miss:
            _log(f"批次 {ids[0]+1}-{ids[-1]+1}：{miss}/{len(ids)} 行缺漏或未翻譯；回應開頭：{raw[:200]!r}")
        return res

    def work(ids):
        res = ask(ids)
        missing = [k for k in ids if k not in res and k not in filtered]
        # 缺漏的行：縮小批次再問一次
        for k0 in range(0, len(missing), 10):
            if missing[k0:k0 + 10]:
                try:
                    res.update(ask(missing[k0:k0 + 10]))
                except RuntimeError:
                    raise
        with lock:
            for k, v in res.items():
                results[k] = v.strip()
            done[0] += 1
            if progress:
                progress(done[0] / total, f"{done[0]}/{total} 批")

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
        list(ex.map(work, batches))

    # 再用 AI 補問兩輪（每次 10 行），仍不行才改用 Google
    for _ in range(2):
        left = [k for k in todo if not is_translated(texts[k], results[k]) and k not in filtered]
        if not left:
            break
        _log(f"AI 補翻第 {_ + 1} 輪：{len(left)} 行")
        for k0 in range(0, len(left), 10):
            for k, v in ask(left[k0:k0 + 10]).items():
                results[k] = v.strip()
    if filtered:
        _log(f"共 {len(set(filtered))} 行被 Azure 內容篩選擋下，已改用 Google 翻譯")
    left = [k for k in todo if not is_translated(texts[k], results[k])]
    if left:
        _log(f"AI 仍有 {len(left)} 行未翻譯，改用 Google 補翻")
        try:
            g = translate_google([texts[k] for k in left])
            for k, v in zip(left, g):
                results[k] = v
        except Exception as e:
            _log(f"Google 補翻失敗：{e!r}")
    still = [k for k in todo if not is_translated(texts[k], results[k])]
    if still:
        _log(f"最終仍有 {len(still)} 行未翻譯：第 {', '.join(str(k + 1) for k in still[:30])} 行")
    return results


def test_llm() -> dict:
    t0 = time.time()
    out = translate_llm(["Hello everyone, welcome back to the channel.", "Today we're going to talk about software."])
    return {"ok": True, "sample": out, "seconds": round(time.time() - t0, 1)}


def diagnose() -> dict:
    """逐步檢查 AI 翻譯連線：設定 → DNS → 連線 → 驗證與模型 → 翻譯格式。"""
    import socket
    import requests

    steps: list[dict] = []

    def add(name, status, detail="", ms=None):
        steps.append({"name": name, "status": status, "detail": detail, "ms": ms})

    def done(ok):
        return {"ok": ok, "steps": steps}

    # 1. 設定
    e = load_env()
    problem = llm_problem()
    key = e.get("LLM_API_KEY", "")
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("已填寫" if key else "未填寫")
    info = (f"端點：{e.get('LLM_ENDPOINT') or '（未填）'}\n模型：{e.get('LLM_MODEL') or '（未填）'}\n"
            f"格式：{(e.get('LLM_API_STYLE') or 'azure')}　金鑰標頭：{e.get('LLM_KEY_HEADER') or 'api-key'}　金鑰：{masked}")
    if problem:
        add("讀取 .env 設定", "fail", f"{problem}\n{info}")
        return done(False)
    cfg = llm_config()
    if cfg.get("note"):
        info += f"\n{cfg['note']}：實際使用 {cfg['endpoint']}（格式 {cfg['style']}）"
    add("讀取 .env 設定", "ok", info)

    # 2. DNS
    host = re.sub(r"^https?://", "", cfg["endpoint"]).split("/")[0]
    hostname, _, port = host.partition(":")
    t = time.time()
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(hostname, None)})
        add("解析主機名稱（DNS）", "ok", f"{hostname} → {', '.join(ips[:3])}", int((time.time() - t) * 1000))
    except Exception as ex:
        add("解析主機名稱（DNS）", "fail", f"找不到 {hostname}：{ex}\n請確認網址拼字，或是否需要連公司 VPN")
        return done(False)

    # 3. 連線
    t = time.time()
    try:
        requests.get(f"{cfg['endpoint'].split('/', 3)[0]}//{host}/", timeout=15)
        add("建立 HTTPS 連線", "ok", f"可連到 {host}", int((time.time() - t) * 1000))
    except requests.exceptions.SSLError as ex:
        add("建立 HTTPS 連線", "fail", f"SSL 憑證錯誤：{str(ex)[:200]}")
        return done(False)
    except Exception as ex:
        add("建立 HTTPS 連線", "fail", f"無法連線：{str(ex)[:200]}\n可能被防火牆或 Proxy 阻擋")
        return done(False)

    # 4. 驗證與模型
    msgs = [{"role": "system", "content": '只輸出 JSON：{"t": ["ok"]}'},
            {"role": "user", "content": "ping"}]
    url, headers, body = _build_request(cfg, msgs)
    t = time.time()
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
    except Exception as ex:
        add("API 驗證與模型", "fail", f"呼叫失敗：{str(ex)[:200]}\n呼叫網址：{url.split('?')[0]}")
        return done(False)
    ms = int((time.time() - t) * 1000)
    if r.status_code == 400 and any(k in r.text for k in ("response_format", "reasoning_effort")):
        for k in ("response_format", "reasoning_effort"):
            if k in r.text:
                _DROPPED.add(k)
        url, headers, body = _build_request(cfg, msgs)
        r = requests.post(url, headers=headers, json=body, timeout=60)
        ms = int((time.time() - t) * 1000)
    if r.status_code >= 400:
        add("API 驗證與模型", "fail", explain_http_error(r.status_code, r.text, cfg, url) +
            f"\n呼叫網址：{url.split('?')[0]}", ms)
        return done(False)
    try:
        data = r.json()
        real_model = data.get("model") or cfg["model"]
        usage = data.get("usage") or {}
    except Exception:
        add("API 驗證與模型", "fail", f"回應不是 JSON：{r.text[:200]}", ms)
        return done(False)
    note = f"HTTP {r.status_code}，實際模型：{real_model}"
    if usage:
        note += f"，用量 {usage.get('total_tokens', '?')} tokens"
    if _DROPPED:
        note += f"\n（此模型不支援 {', '.join(sorted(_DROPPED))}，已自動略過）"
    add("API 驗證與模型", "ok", note, ms)

    # 5. 實際翻譯
    t = time.time()
    try:
        sample = translate_llm(["Hello everyone, welcome back to the channel.",
                                "Today we're going to talk about software."])
        ms = int((time.time() - t) * 1000)
        if sample[0].startswith("Hello"):
            add("字幕翻譯測試", "warn", "AI 回傳格式不符，已改用 Google 補翻；可改用其他模型或回報給我", ms)
        else:
            add("字幕翻譯測試", "ok", "\n".join(sample), ms)
    except Exception as ex:
        add("字幕翻譯測試", "fail", str(ex)[:300])
        return done(False)
    return done(True)


def translate(texts: list[str], engine: str = "google", context: str = "", progress=None,
              only: list[int] | None = None, labels: list[str] | None = None) -> tuple[list[str], str]:
    """回傳 (譯文, 實際使用的引擎說明)。only：只翻這些行號（其餘原樣回傳）"""
    if engine == "local":                      # 本機 TranslateGemma（1.25.0）：不上傳、不需要金鑰
        from . import local_llm
        bad = local_llm.translate_problem()
        if bad:
            raise RuntimeError(bad)
        idx = list(range(len(texts))) if only is None else sorted(only)
        out = list(texts)
        for k, v in zip(idx, local_llm.translate([texts[k] for k in idx], progress=progress)):
            out[k] = v
        return out, "本機 TranslateGemma"
    if engine == "codex":
        cfg = codex_config()
        if not cfg:
            raise RuntimeError("AI 翻譯找不到本機的 Codex CLI：請到「設定」填入 codex 路徑並按「測試 Codex 連線」")
        return translate_llm(texts, context, progress, only, labels, cfg=cfg), cfg["label"]
    if engine == "llm" and llm_config():
        return translate_llm(texts, context, progress, only, labels), f"AI（{llm_config()['model']}）"
    idx = list(range(len(texts))) if only is None else sorted(only)
    out = list(texts)
    for k, v in zip(idx, translate_google([texts[k] for k in idx], progress=progress)):
        out[k] = v
    return out, "Google 翻譯"
