/* 影音轉逐字稿 — 前端（不依賴外部套件，離線可用） */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* 私密模式等情況忽略 */ } },
  };
  const api = async (url, opt) => {
    const r = await fetch(url, opt);
    if (!r.ok) {
      let msg = `伺服器回應 ${r.status}`;
      try { msg = (await r.json()).detail || msg; } catch { }
      throw new Error(msg);
    }
    return r.json();
  };
  const toast = msg => {
    const t = $("#toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3200);
  };
  const dur = sec => {
    sec = Math.round(sec || 0);
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  };
  const human = sec => sec < 60 ? `${Math.round(sec)} 秒` : `${Math.round(sec / 60)} 分鐘`;
  const minsec = sec => { sec = Math.round(sec); return sec < 60 ? `${sec} 秒` : `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`; };
  const DEV = { GPU: "顯示卡", CPU: "處理器（CPU）", auto: "自動" };
  const MODEL = { breeze: "Breeze-ASR-25", turbo: "large-v3-turbo" };
  const SPK = `<option value="auto">自動判斷人數</option><option value="2">2 人</option><option value="3">3 人</option><option value="4">4 人</option><option value="5">5 人</option><option value="6">6 人</option>`;
  const STATE = { queued: "排隊中", running: "處理中", done: "完成", error: "失敗", interrupted: "未完成", cancelled: "已取消" };

  /* ---------- 確認視窗 ---------- */
  const confirmBox = (title, msg, okLabel = "確定") => new Promise(res => {
    const d = $("#confirmDlg");
    $("#dlgTitle").textContent = title; $("#dlgMsg").textContent = msg; $("#dlgOk").textContent = okLabel;
    d.returnValue = "no";
    d.addEventListener("close", () => res(d.returnValue === "yes"), { once: true });
    d.showModal();
  });
  const TRASH = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>';
  const cancelJob = async (id, name) => {
    if (!await confirmBox("取消轉檔？", `「${name}」會停止辨識，已處理的進度不會保留。之後要轉檔需重新上傳。`, "取消轉檔")) return false;
    try { await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }); toast("已送出取消，目前這一段辨識完就會停止"); return true; }
    catch (e) { toast(e.message); return false; }
  };
  const deleteJob = async (id, name) => {
    if (!await confirmBox("刪除這個任務？", `會永久刪除「${name}」的任務資料夾，包含字幕（SRT）、純文字、MP3 與上傳的原始檔，無法復原。`, "永久刪除")) return false;
    try { await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }); toast(`已刪除：${name}`); return true; }
    catch (e) { toast(e.message); return false; }
  };

  let info = null;
  let autoplayId = "";  // 從任務清單點進任務頁時自動播放

  /* ---------- 字型大小 ---------- */
  const setFs = fs => {
    document.documentElement.dataset.fs = fs;
    document.querySelectorAll("button[data-fs]").forEach(b => b.setAttribute("aria-pressed", b.dataset.fs === fs));
    store.set("fs", fs);
  };
  document.querySelectorAll("button[data-fs]").forEach(b => b.addEventListener("click", () => setFs(b.dataset.fs)));
  setFs(store.get("fs", "m"));

  /* ---------- 外觀：自動（跟系統）／明亮／暗黑 ---------- */
  const setTheme = t => {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else { delete document.documentElement.dataset.theme; t = "auto"; }
    store.set("theme", t);
    document.querySelectorAll("[data-theme-set]").forEach(b => b.setAttribute("aria-pressed", b.dataset.themeSet === t));
  };
  document.querySelectorAll("[data-theme-set]").forEach(b => b.addEventListener("click", () => setTheme(b.dataset.themeSet)));
  setTheme(store.get("theme", "auto"));

  /* ---------- 完成提示音（Web Audio 即時產生，不需音效檔） ---------- */
  let actx = null;
  const audioCtx = () => {
    try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); } catch { return null; }
    if (actx.state === "suspended") actx.resume().catch(() => { });
    return actx;
  };
  // 瀏覽器規定要先有使用者操作才能發聲：第一次點擊／按鍵時先喚醒
  ["pointerdown", "keydown"].forEach(ev => addEventListener(ev, () => audioCtx(), { once: true }));
  const chime = (ok = true) => {
    const ctx = audioCtx(); if (!ctx) return;
    const notes = ok ? [[880, 0], [1318.5, 0.16]] : [[440, 0], [349.2, 0.22]];
    const t0 = ctx.currentTime + 0.02;
    notes.forEach(([f, d]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t0 + d);
      g.gain.exponentialRampToValueAtTime(0.35, t0 + d + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + d + (ok ? 0.9 : 0.6));
      o.connect(g).connect(ctx.destination);
      o.start(t0 + d); o.stop(t0 + d + 1);
    });
  };
  const soundOn = () => $("#soundOn").checked;
  $("#soundOn").checked = store.get("sound", "1") === "1";
  $("#soundOn").addEventListener("change", e => { store.set("sound", e.target.checked ? "1" : "0"); if (e.target.checked) chime(true); });
  $("#soundTest").addEventListener("click", () => chime(true));

  /* 背景留意任務狀態：不論停在哪一頁，任務完成／失敗都會提醒 */
  const baseTitle = document.title;
  let jobStates = null;
  const watchJobs = async () => {
    let jobs;
    try { jobs = await api("/api/jobs"); } catch { return; }
    const now = Object.fromEntries(jobs.map(j => [j.id, j.state]));
    if (jobStates) {
      const finished = jobs.filter(j => (jobStates[j.id] === "running" || jobStates[j.id] === "queued") && (j.state === "done" || j.state === "error"));
      if (finished.length) {
        const ok = finished.every(j => j.state === "done");
        if (soundOn()) chime(ok);
        toast(ok ? `完成：${finished.map(j => j.name).join("、")}` : `有任務失敗：${finished.filter(j => j.state === "error").map(j => j.name).join("、")}`);
        if (document.hidden) document.title = `${ok ? "✓ 完成" : "✗ 失敗"}｜${baseTitle}`;
      }
    }
    jobStates = now;
  };
  addEventListener("visibilitychange", () => { if (!document.hidden) document.title = baseTitle; });
  setInterval(watchJobs, 2000); watchJobs();

  /* ---------- 選單收合 ---------- */
  const appEl = $("#app");
  const mobile = () => matchMedia("(max-width: 760px)").matches;
  if (store.get("collapsed", "0") === "1") appEl.classList.add("collapsed");
  /* 學習模式時自動收合左側選單（讓影片更大）；切回觀看模式或離開任務頁時恢復。使用者自己按過就不再自動處理 */
  let autoCollapsed = false;
  const learnSide = on => {
    if (matchMedia("(max-width: 760px)").matches) return;  // 手機版選單本來就是抽屜
    if (on && !appEl.classList.contains("collapsed")) { appEl.classList.add("collapsed"); autoCollapsed = true; }
    else if (!on && autoCollapsed) { appEl.classList.remove("collapsed"); autoCollapsed = false; }
  };
  $("#toggle").addEventListener("click", () => {
    autoCollapsed = false;
    if (mobile()) { appEl.classList.toggle("open"); return; }
    appEl.classList.toggle("collapsed");
    store.set("collapsed", appEl.classList.contains("collapsed") ? "1" : "0");
  });
  $("#fabMenu").addEventListener("click", () => appEl.classList.add("open"));
  $("#scrim").addEventListener("click", () => appEl.classList.remove("open"));

  /* ---------- 上傳區的波形 ---------- */
  (() => {
    const svg = $("#wave"); let h = "";
    const heights = [6, 14, 22, 12, 30, 18, 26, 10, 34, 20, 14, 28, 8, 24, 16, 32, 12, 22, 6, 18, 28, 10, 20, 14, 8];
    heights.forEach((v, i) => { const x = 6 + i * 8.7; h += `<rect x="${x}" y="${20 - v / 2}" width="4" height="${v}" rx="2" fill="currentColor"/>`; });
    svg.innerHTML = h;
  })();

  /* ---------- 路由 ---------- */
  const TITLES = { transcribe: "轉逐字稿", history: "任務紀錄", bench: "本機效能", settings: "參數設定", job: "任務內容" };
  let poll = null;
  const route = () => {
    const [page, arg] = (location.hash.slice(1) || "transcribe").split("/");
    const p = TITLES[page] ? page : "transcribe";
    document.querySelectorAll(".page").forEach(s => s.hidden = s.id !== `page-${p}`);
    document.querySelectorAll("nav a").forEach(a => a.toggleAttribute("aria-current", a.dataset.page === p || (p === "job" && a.dataset.page === "history")));
    document.querySelectorAll("nav a[aria-current]").forEach(a => a.setAttribute("aria-current", "page"));
    $("#title").textContent = TITLES[p];
    $("#title").hidden = p === "job";  // 任務頁不放標題，讓影片在最上面
    appEl.classList.remove("open");
    clearInterval(poll);
    if (p === "history") loadTags();
    if (p === "transcribe" || p === "history") { loadJobs(); poll = setInterval(loadJobs, 1500); }
    if (p !== "job") {
      learnSide(false);
      document.querySelectorAll("#page-job video, #page-job audio").forEach(m => m.pause());  // 離開任務頁就停止播放
    }
    if (p === "job") { loadJob(decodeURIComponent(arg || "")); poll = setInterval(() => loadJob(decodeURIComponent(arg || ""), true), 1500); }
    if (p === "bench") { loadBench(); poll = setInterval(() => loadBench(true), 2000); }
    if (p === "settings") loadSettings();
  };
  addEventListener("hashchange", route);

  /* ---------- 基本資訊 ---------- */
  const loadInfo = async () => {
    info = await api("/api/info");
    $("#ver").textContent = `v${info.version}`;
    $("#machine").innerHTML = (info.alias ? `<b>${esc(info.alias)}</b><br><span>${esc(info.computer)}</span>` : esc(info.computer))
      + `<br>${esc(info.gpus.join("、") || info.cpu)}`;
    $("#machine").title = info.alias ? `電腦別名「${info.alias}」，電腦名稱 ${info.computer}（可在「參數設定 → 這台電腦」修改）` : "可在「參數設定 → 這台電腦」設定別名";
    const r = info.recommended;
    $("#recHint").textContent = r.source === "benchmark"
      ? `本機建議：${MODEL[r.model]} × ${DEV[r.device] || r.device}（依效能實測）`
      : "這台電腦還沒做效能實測，先用預設配置。到「本機效能」做一次實測，可找出最快的配置。";
    const dz = info.diarization || {};
    if (!dz.model) [...$("#spk").options].forEach(o => { if (o.value) o.disabled = true; });
    $("#spk").title = !dz.model ? "找不到說話者辨識模型，請向管理者索取 D:\\models\\diarization"
      : dz.installed ? "" : "第一次使用會自動安裝元件（約 1～2 GB，需要幾分鐘）";
    const gs = $("#gloss"), gsv = store.get("gloss", "");
    gs.innerHTML = `<option value="">不使用</option>` + (info.glossaries || []).map(g => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
    gs.value = (info.glossaries || []).includes(gsv) ? gsv : "";
    const td = $("#tidy");
    td.value = info.llm?.ready ? store.get("tidy", "") : "";
    td.options[1].disabled = !info.llm?.ready;
    td.title = info.llm?.ready ? "用本機 LLM 整理，整理稿直接取代純文字檔（檔名.txt）；SRT 字幕不動" : "需要本機 LLM：到「參數設定 → 本機 LLM」下載";
    updOpts();
    const notReady = Object.entries(info.models).filter(([, m]) => !m.ready).map(([, m]) => m.label);
    if (notReady.length) toast(`找不到模型：${notReady.join("、")}，請先執行 setup.cmd`);
  };

  /* ---------- 上傳 ---------- */
  const drop = $("#drop"), fileIn = $("#fileIn");
  const upload = async files => {
    if (!files.length) return;
    const fd = new FormData();
    [...files].forEach(f => fd.append("files", f));
    fd.append("model", $("#model").value);
    fd.append("lang", $("#lang").value);
    fd.append("speakers", $("#spk").value);
    fd.append("glossary", $("#gloss").value);
    fd.append("tidy", $("#tidy").value);
    toast(`上傳 ${files.length} 個檔案中…`);
    try {
      const res = await api("/api/jobs", { method: "POST", body: fd });
      const bad = res.filter(r => r.state === "error");
      toast(bad.length ? `${bad.map(b => b.name).join("、")}：不支援的檔案格式` : `已加入 ${res.length} 個任務`);
      loadJobs();
    } catch (e) { toast(`上傳失敗：${e.message}`); }
    fileIn.value = "";
  };
  fileIn.addEventListener("change", () => upload(fileIn.files));
  $("#spk").value = store.get("spk", "");
  $("#spk").addEventListener("change", e => store.set("spk", e.target.value));

  /* 辨識設定：平常只顯示一行摘要，按「⚙ 辨識設定」才展開（記住展開狀態） */
  const optsPanel = $("#optsPanel"), optsBtn = $("#optsBtn");
  const selText = sel => sel.options[sel.selectedIndex]?.text || "";
  const updOpts = () => {
    const r = info?.recommended;
    const m = $("#model").value ? selText($("#model")).replace(/（.*/, "")
      : `本機建議${r ? `（${MODEL[r.model] || r.model} × ${DEV[r.device] || r.device}）` : ""}`;
    $("#optsSum").textContent = `目前設定：${m}｜語言：${selText($("#lang"))}｜說話者：${selText($("#spk"))}｜詞庫：${selText($("#gloss"))}${$("#tidy").value ? "｜轉完整理" : ""}`;
  };
  ["#model", "#lang", "#spk", "#gloss", "#tidy"].forEach(s => $(s).addEventListener("change", updOpts));
  $("#tidy").addEventListener("change", e => store.set("tidy", e.target.value));
  $("#gloss").addEventListener("change", e => store.set("gloss", e.target.value));
  const setOpts = open => {
    optsPanel.hidden = !open; optsBtn.setAttribute("aria-expanded", open);
    optsBtn.textContent = open ? "⚙ 收合設定" : "⚙ 辨識設定"; store.set("optsOpen", open ? "1" : "0");
  };
  optsBtn.addEventListener("click", () => setOpts(optsPanel.hidden));
  setOpts(store.get("optsOpen", "0") === "1");
  updOpts();

  /* ---------- 貼網址 ---------- */
  const submitUrl = async url => {
    url = (url || "").trim();
    if (!url) return;
    const btn = $("#urlGo");
    btn.disabled = true; btn.textContent = "確認網址中…";
    const fd = new FormData();
    fd.append("url", url); fd.append("model", $("#model").value); fd.append("lang", $("#lang").value);
    fd.append("speakers", $("#spk").value);
    fd.append("glossary", $("#gloss").value);
    fd.append("tidy", $("#tidy").value);
    try {
      const j = await api("/api/jobs/url", { method: "POST", body: fd });
      toast(`已加入：${j.name}`);
      $("#urlIn").value = "";
      loadJobs();
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; btn.textContent = "下載並轉逐字稿"; }
  };
  $("#urlForm").addEventListener("submit", e => { e.preventDefault(); submitUrl($("#urlIn").value); });

  /* 清除已完成：只從這個清單移除，檔案與任務紀錄都保留 */
  const dismiss = async ids => {
    const fd = new FormData(); fd.append("ids", ids.join(","));
    try { const r = await api("/api/jobs/dismiss", { method: "POST", body: fd }); toast(`已從清單移除 ${r.count} 個任務（仍可在任務紀錄找到）`); loadJobs(); }
    catch (e) { toast(e.message); }
  };
  $("#clearDone").addEventListener("click", () => dismiss([]));
  // 清單上的按鈕：從清單移除／取消／刪除
  /* 任務紀錄：勾選、標籤（點標籤＝篩選、＋標籤＝直接輸入） */
  const saveTags = async (ids, add, remove = []) => {
    try { await api("/api/jobs/tags", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, add, remove }) }); }
    catch (e) { toast(e.message); return false; }
    await loadTags(); return true;
  };
  const splitTags = v => v.split(/[\s,，、#＃]+/).map(x => x.trim()).filter(Boolean);
  $("#allJobs").addEventListener("change", e => {
    const c = e.target.closest("[data-sel]"); if (!c) return;
    if (c.checked) selected.add(c.dataset.sel); else selected.delete(c.dataset.sel);
    updBatch();
  });
  $("#allJobs").addEventListener("click", async e => {
    const chip = e.target.closest("[data-tagf]");
    if (chip) { e.preventDefault(); $("#fltTag").value = chip.dataset.tagf; store.set("fltTag", chip.dataset.tagf); renderHistory(true); return; }
    const add = e.target.closest("[data-tagadd]");
    if (add) {
      e.preventDefault();
      const id = add.dataset.tagadd;
      const inp = document.createElement("input");
      inp.type = "text"; inp.className = "tag-input"; inp.setAttribute("list", "tagList"); inp.placeholder = "輸入標籤，Enter 完成";
      add.replaceWith(inp); inp.focus();
      let done = false;
      const finish = async ok => {
        if (done) return; done = true;
        const t = splitTags(inp.value);
        if (ok && t.length && await saveTags([id], t)) toast(`已加上標籤：${t.map(x => "#" + x).join(" ")}`);
        inp.blur(); loadJobs();
      };
      inp.addEventListener("keydown", ev => { if (ev.key === "Enter") finish(true); if (ev.key === "Escape") finish(false); });
      inp.addEventListener("blur", () => finish(true));
    }
  });
  $("#selAll").addEventListener("change", e => {
    filterJobs(allJobsCache).forEach(j => e.target.checked ? selected.add(j.id) : selected.delete(j.id));
    renderHistory(true);
  });
  document.querySelectorAll("[data-batch]").forEach(b => b.addEventListener("click", async () => {
    const ids = [...selected], act = b.dataset.batch;
    if (!ids.length) return;
    if (act === "clear") { selected.clear(); renderHistory(true); return; }
    if (act === "tag-add" || act === "tag-remove") {
      const t = splitTags($("#batchTag").value);
      if (!t.length) { toast("請先輸入標籤"); $("#batchTag").focus(); return; }
      if (await saveTags(ids, act === "tag-add" ? t : [], act === "tag-remove" ? t : []))
        toast(`${act === "tag-add" ? "已加上" : "已移除"}標籤 ${t.map(x => "#" + x).join(" ")}（${ids.length} 筆）`);
      $("#batchTag").value = ""; loadJobs(); return;
    }
    if (act === "zip") { location.href = `/api/jobs-zip?ids=${encodeURIComponent(ids.join(","))}`; return; }
    const LBL = { diarize: "分辨說話者", translate: "翻譯成中文", delete: "刪除" };
    if (act === "delete" && !(await confirmBox("批次刪除", `確定永久刪除選取的 ${ids.length} 個任務與檔案？處理中的任務會略過。`, "刪除"))) return;
    try {
      const r = await api("/api/jobs/batch", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: act, ids, speakers: $("#batchSpk").value }) });
      const sk = r.skipped.length ? `，略過 ${r.skipped.length} 筆（${[...new Set(r.skipped.map(x => x.reason))].slice(0, 2).join("；")}）` : "";
      toast(`${LBL[act]}：${act === "delete" ? "已刪除" : "已加入佇列"} ${r.ok} 筆${sk}`);
      if (act === "delete") selected.clear();
      loadJobs();
    } catch (e) { toast(e.message); }
  }));

  ["#activeJobs", "#allJobs"].forEach(sel => $(sel).addEventListener("click", async e => {
    if (e.target.closest("[data-tagf],[data-tagadd],.tag-input,[data-sel]")) return;
    const row = e.target.closest("a.row");
    if (row) {  // 點清單上的任務：進入任務頁並直接播放
      autoplayId = decodeURIComponent(row.getAttribute("href").split("/")[1] || "");
      window.scrollTo(0, 0);
      return;
    }
    const b = e.target.closest("[data-dismiss],[data-cancel],[data-delete]"); if (!b) return;
    e.preventDefault();
    if (b.dataset.dismiss) dismiss([b.dataset.dismiss]);
    else if (b.dataset.cancel) { if (await cancelJob(b.dataset.cancel, b.dataset.name)) loadJobs(); }
    else if (b.dataset.delete) { if (await deleteJob(b.dataset.delete, b.dataset.name)) loadJobs(); }
  }));
  drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } });
  ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => {
    if (e.dataTransfer.files.length) return upload(e.dataTransfer.files);
    const u = (e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "").split("\n")[0].trim();
    if (/^https?:\/\//i.test(u)) { $("#urlIn").value = u; submitUrl(u); }  // 從瀏覽器拖曳網址進來
  });

  /* ---------- 任務清單 ---------- */
  /* 任務紀錄排序（記住選擇） */
  const SORT_LABEL = { created: "建立時間", name: "名稱", duration: "錄音長度", asr: "辨識耗時", state: "狀態", origin: "來源" };
  const STATE_ORDER = { running: 0, queued: 1, error: 2, interrupted: 3, cancelled: 4, done: 5 };
  const sortVal = (j, k) => k === "name" ? (j.name || "").toLowerCase() : k === "duration" ? (j.summary?.duration ?? -1)
    : k === "asr" ? (j.summary?.asr_sec ?? -1) : k === "state" ? (STATE_ORDER[j.state] ?? 9) : k === "origin" ? j.origin || "" : j.created || "";
  const sortJobs = jobs => {
    const k = store.get("sortKey", "created"), dir = store.get("sortDir", "desc") === "asc" ? 1 : -1;
    return [...jobs].sort((a, b) => {
      const x = sortVal(a, k), y = sortVal(b, k);
      const c = typeof x === "number" ? x - y : String(x).localeCompare(String(y), "zh-Hant");
      return c * dir || String(b.created || "").localeCompare(String(a.created || ""));  // 相同時新的在前
    });
  };
  const updSortUi = () => {
    $("#sortKey").value = store.get("sortKey", "created");
    const asc = store.get("sortDir", "desc") === "asc";
    $("#sortDir").textContent = asc ? "↑ 升冪" : "↓ 降冪";
    $("#sortDir").title = asc ? "目前由小到大（舊到新、A 到 Z），按一下改降冪" : "目前由大到小（新到舊、Z 到 A），按一下改升冪";
  };
  $("#sortKey").addEventListener("change", e => { store.set("sortKey", e.target.value); updSortUi(); renderHistory(); });
  $("#sortDir").addEventListener("click", () => { store.set("sortDir", store.get("sortDir", "desc") === "asc" ? "desc" : "asc"); updSortUi(); renderHistory(); });
  updSortUi();

  /* 任務紀錄搜尋與篩選（條件記住；搜尋字邊打邊篩） */
  let allJobsCache = [];
  const FLT = ["fltState", "fltOrigin", "fltDays", "fltFeat", "fltTag"];
  const selected = new Set();  // 任務紀錄勾選的任務（清單自動更新後保留）
  let allTags = [];
  const loadTags = async () => {
    try { allTags = await api("/api/tags"); } catch { return; }
    $("#tagList").innerHTML = allTags.map(t => `<option value="${esc(t.tag)}">`).join("");
    const cur = store.get("fltTag", "");
    $("#fltTag").innerHTML = `<option value="">全部標籤</option>` + allTags.map(t => `<option value="${esc(t.tag)}">#${esc(t.tag)}（${t.count}）</option>`).join("");
    $("#fltTag").value = allTags.some(t => t.tag === cur) ? cur : "";
  };
  const updBatch = () => {
    for (const id of [...selected]) if (!allJobsCache.some(j => j.id === id)) selected.delete(id);
    $("#batchActs").hidden = !selected.size;
    $("#selN").textContent = selected.size;
    const vis = filterJobs(allJobsCache);
    $("#selAll").checked = vis.length > 0 && vis.every(j => selected.has(j.id));
  };
  const filterJobs = jobs => {
    const q = $("#fltQ").value.trim().toLowerCase(), st = $("#fltState").value, org = $("#fltOrigin").value;
    const days = +$("#fltDays").value, feat = $("#fltFeat").value;
    const now = new Date(), today = now.toISOString().slice(0, 10);
    return jobs.filter(j => {
      if (q && !`${j.name || ""} ${j.url || ""} ${(j.tags || []).map(t => "#" + t).join(" ")}`.toLowerCase().includes(q)) return false;
      const tg = $("#fltTag").value;
      if (tg && !(j.tags || []).includes(tg)) return false;
      if (st === "active" && !["running", "queued"].includes(j.state)) return false;
      if (st === "error" && !["error", "interrupted"].includes(j.state)) return false;
      if (st && !["active", "error"].includes(st) && j.state !== st) return false;
      if (org && j.origin !== org) return false;
      if (feat && !j[feat]) return false;
      if (days && j.created) {
        const d = new Date(j.created.replace(" ", "T"));
        if (days === 1 ? j.created.slice(0, 10) !== new Date(now - now.getTimezoneOffset() * 6e4).toISOString().slice(0, 10)
          : now - d > days * 864e5) return false;
      }
      return true;
    });
  };
  const renderHistory = force => {
    // 正在輸入標籤時不重畫，避免輸入框被自動更新洗掉
    if (!force && document.activeElement?.closest?.("#allJobs") && document.activeElement.tagName === "INPUT"
        && document.activeElement.type === "text") return;
    const list = sortJobs(filterJobs(allJobsCache));
    $("#allJobs").innerHTML = list.map(j => jobRow(j, false, true)).join("")
      || `<li class="empty">${allJobsCache.length ? "沒有符合條件的任務。" : "還沒有任何轉檔紀錄。"}</li>`;
    const filtered = list.length !== allJobsCache.length;
    $("#jobCount").textContent = allJobsCache.length ? (filtered ? `符合 ${list.length} 筆／共 ${allJobsCache.length} 筆` : `共 ${allJobsCache.length} 筆`) : "";
    $("#fltClear").hidden = !filtered && !$("#fltQ").value;
    updBatch();
  };
  FLT.forEach(id => { $("#" + id).value = store.get(id, ""); $("#" + id).addEventListener("change", e => { store.set(id, e.target.value); renderHistory(); }); });
  $("#fltQ").addEventListener("input", renderHistory);
  $("#fltClear").addEventListener("click", () => { $("#fltQ").value = ""; FLT.forEach(id => { $("#" + id).value = ""; store.set(id, ""); }); renderHistory(); });

  const jobRow = (j, dismissable, showDate) => {
    const s = j.summary;
    let meta = j.created || "";
    if (j.state === "running" || j.state === "queued") meta = (j.stage || "") + (j.elapsed ? `｜已進行 ${minsec(j.elapsed)}` : "");  // 處理中一律顯示目前階段
    else if (s) meta = `${dur(s.duration)} 錄音｜辨識 ${human(s.asr_sec)}｜${MODEL[s.model] || s.model} × ${DEV[s.device] || s.device}｜${s.chars} 字`;
    else if (j.error) meta = j.error;
    if (showDate && j.created) meta = `${j.created.slice(0, 16)}｜${j.origin === "url" ? "網址" : "上傳"}${meta && meta !== j.created ? "｜" + meta : ""}`;
    const pct = Math.round((j.progress || 0) * 100);
    const st = j.state === "running" ? `${pct}%` : (STATE[j.state] || j.state);
    const BGN = { tidy: "整理逐字稿", diarize: "分辨說話者", translate: "翻譯成中文" };
    const bp = j.bg ? Math.round((j.bg.progress || 0) * 100) : 0;
    const bar = (j.state === "running" || j.state === "queued") ? `<span class="bar"><i style="width:${pct}%"></i></span>`
      : j.bg ? `<span class="bar bg-bar" title="${esc(BGN[j.bg.kind] || "")}${j.bg.state === "queued" ? "（排隊中）" : ` ${bp}%`}｜${esc(j.bg.stage || "")}"><i style="width:${bp}%"></i></span>` : "";
    const active = j.state === "running" || j.state === "queued";
    let acts = "";
    if (active) acts = `<button class="cancel" data-cancel="${esc(j.id)}" data-name="${esc(j.name)}">取消</button>`;
    else {
      if (dismissable) acts += `<button class="x" data-dismiss="${esc(j.id)}" aria-label="從清單移除 ${esc(j.name)}" title="從清單移除（不刪檔案，仍在任務紀錄）">×</button>`;
      acts += `<button class="x del" data-delete="${esc(j.id)}" data-name="${esc(j.name)}" aria-label="刪除 ${esc(j.name)}" title="刪除任務與檔案">${TRASH}</button>`;
    }
    const sel = showDate ? `<input type="checkbox" class="sel" data-sel="${esc(j.id)}" ${selected.has(j.id) ? "checked" : ""} aria-label="選取 ${esc(j.name)}">` : "";
    const FEAT = [["spk", "說話者", "已分辨說話者"], ["td", "整理稿", "已有整理稿"], ["tr", "雙語", "已翻譯成中文"], ["ed", "已校對", "已在校對模式修改過"]];
    const feats = j.state === "done" ? FEAT.filter(([k]) => j[k]).map(([k, t, tip]) => `<span class="feat f-${k}" title="${tip}">${t}</span>`).join("") : "";
    const tags = showDate ? `<span class="tags">${(j.tags || []).map(t => `<button type="button" class="tagchip" data-tagf="${esc(t)}" title="只顯示這個標籤">#${esc(t)}</button>`).join("")}<button type="button" class="tag-add" data-tagadd="${esc(j.id)}">＋標籤</button></span>` : "";
    return `<li class="has-x st-${esc(j.state)}${showDate ? " selectable" : ""}">${sel}<a class="row" href="#job/${encodeURIComponent(j.id)}">
      <span class="name">${j.kind === "diarize" && active ? `<span class="tag">分辨說話者</span>` : j.kind === "translate" && active ? `<span class="tag">翻譯</span>` : j.kind === "tidy" && active ? `<span class="tag">整理逐字稿</span>` : ""}${esc(j.name)}</span><span class="state ${j.state}">${esc(st)}</span>${feats ? `<span class="feats">${feats}</span>` : ""}${j.bg ? `<span class="tag bg-tag" title="${esc(j.bg.stage || "")}">${esc(BGN[j.bg.kind] || "處理中")}${j.bg.state === "queued" ? " 排隊中" : ` ${bp}%`}</span>` : ""}
      <span class="meta">${esc(meta)}</span>${bar}</a><span class="row-acts">${acts}</span>${tags}</li>`;
  };
  const loadJobs = async () => {
    let jobs;
    try { jobs = await api("/api/jobs"); } catch { return; }
    const active = jobs.filter(j => j.state === "running" || j.state === "queued");
    const recent = jobs.filter(j => j.state !== "running" && j.state !== "queued" && !j.hidden).slice(0, 8);
    $("#activeJobs").innerHTML = [...active, ...recent].map(j => jobRow(j, true)).join("")
      || `<li class="empty">目前沒有任務。把檔案拖到上方即可開始；完成的任務都在「任務紀錄」。</li>`;
    $("#clearDone").hidden = recent.length === 0;
    allJobsCache = jobs;
    renderHistory();
  };

  /* ---------- 播放器（仿 YouTube）：控制列疊在畫面下緣、閒置自動隱藏；點畫面播放／暫停 ---------- */
  const RATES = [0.25, 0.5, 1, 1.25, 1.5, 2];
  const ICON = {
    play: '<path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/>',
    pause: '<path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor" stroke="none"/>',
    vol: '<path d="M4 9v6h4l5 4V5L8 9z" fill="currentColor" stroke="none"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
    mute: '<path d="M4 9v6h4l5 4V5L8 9z" fill="currentColor" stroke="none"/><path d="M16 9l5 6M21 9l-5 6"/>',
    cc: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M10 10.5a2 2 0 1 0 0 3M16 10.5a2 2 0 1 0 0 3"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    fs: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    fsx: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  };
  const svg = (name, label) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[name]}</svg><span class="sr">${label}</span>`;

  const playerHtml = (id, kind, src) => {
    src = src || `/api/jobs/${encodeURIComponent(id)}/media`;
    const media = kind === "video"
      ? `<video id="pl" preload="metadata" playsinline src="${src}"></video>`
      : `<audio id="pl" preload="metadata" src="${src}"></audio><div class="audio-bg" aria-hidden="true">${$("#wave")?.outerHTML.replace('id="wave"', "") || ""}</div>`;
    return `<div class="yt ${kind} paused" id="player" tabindex="0" aria-label="播放器：空白鍵播放／暫停，左右鍵倒退／快轉 5 秒（校對模式為上一句／下一句），F 全螢幕，C 字幕">
      ${media}
      <div class="yt-cap" id="cap" aria-live="off"></div>
      <div class="yt-big" aria-hidden="true"><svg viewBox="0 0 24 24">${ICON.play}</svg></div>
      <div class="yt-ctl" id="ctl">
        <div class="yt-prog" id="prog" role="slider" aria-label="播放進度" tabindex="-1">
          <div class="yt-buf" id="buf"></div><div class="yt-done" id="done"></div><div class="yt-knob" id="knob"></div>
          <div class="yt-tip" id="tip"></div>
        </div>
        <div class="yt-row">
          <button class="yt-btn" id="bPlay">${svg("play", "播放")}</button>
          <div class="yt-vol"><button class="yt-btn" id="bMute">${svg("vol", "靜音")}</button>
            <input type="range" id="vol" min="0" max="1" step="0.05" value="1" aria-label="音量"></div>
          <span class="yt-time" id="time">0:00 / 0:00</span>
          <span class="yt-sp"></span>
          <div class="yt-menu-wrap">
            <button class="yt-btn yt-aa" id="bStyle" aria-haspopup="true" aria-expanded="false" title="字幕外觀"><b>Aa</b><span class="sr">字幕外觀</span></button>
            <div class="yt-menu yt-style" id="styleMenu" role="menu" hidden>${capStyleMenu()}</div>
          </div>
          <button class="yt-btn" id="bCC" aria-pressed="true" title="字幕（C）">${svg("cc", "字幕")}</button>
          <div class="yt-menu-wrap">
            <button class="yt-btn yt-rate" id="bRate" aria-haspopup="true" aria-expanded="false" title="播放速度">${svg("gear", "播放速度")}<b id="rateTxt">1×</b></button>
            <div class="yt-menu" id="rateMenu" role="menu" hidden>
              <div class="yt-menu-h">播放速度</div>
              ${RATES.map(r => `<button role="menuitemradio" data-rate="${r}" aria-checked="${r === 1}">${r === 1 ? "正常" : r + "×"}</button>`).join("")}
            </div>
          </div>
          <button class="yt-btn" id="bFs" title="全螢幕（F）">${svg("fs", "全螢幕")}</button>
        </div>
      </div></div>`;
  };

  /* ---------- 校對模式（0.11.3）：觀看版面／全螢幕，字幕直接修改；每句存檔＋瀏覽器草稿，存不到自動重試 ---------- */
  const draftKey = id => `proofDraft:${id}`;
  const getDrafts = id => { try { return JSON.parse(store.get(draftKey(id), "{}")); } catch { return {}; } };
  const setDraft = (id, i, text) => { const d = getDrafts(id); if (text == null) delete d[i]; else d[i] = text; if (Object.keys(d).length) store.set(draftKey(id), JSON.stringify(d)); else try { localStorage.removeItem(draftKey(id)); } catch { } };
  let proofRetry = null, proofNav = null, playerKeys = null, llmPoll = null;
  document.addEventListener("keydown", e => { if (playerKeys && $("#player")) playerKeys(e); });
  const saveSeg = async (id, i, text) => api(`/api/jobs/${encodeURIComponent(id)}/segment`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ i, text }) });
  const flushDrafts = async (id, onEdit, quiet) => {
    const d = getDrafts(id); let n = 0;
    for (const [i, text] of Object.entries(d)) {
      try { await saveSeg(id, +i, text); setDraft(id, i, null); onEdit(+i, text); n++; } catch { return -1; }
    }
    if (n && !quiet) toast(`已補存 ${n} 句先前未存檔的修改`);
    return n;
  };
  const bindProof = (id, sg, onEdit, flagged = []) => {
    const box = $("#player"), pl = $("#pl");
    if (!box || !pl || !sg.length) return;
    clearInterval(proofRetry);
    const ed = document.createElement("div");
    ed.className = "proof-ed";
    ed.innerHTML = `<div class="pe-prev" id="pePrev"></div>
      <textarea id="peText" rows="2" spellcheck="false" aria-label="修改這一句字幕"></textarea>
      <div class="pe-hint" id="peHint"></div>
      <div class="pe-next" id="peNext"></div>
      <div class="pe-bar"><button type="button" id="pePrevBtn" title="上一句（←；編輯中 Shift+Tab 或 Alt+←）">◀ 上一句</button>
        <button type="button" id="peReplay" title="重播這句（Ctrl+空白鍵）">↺ 重播這句</button>
        <button type="button" id="peNextBtn" title="下一句（→；編輯中 Tab 或 Alt+→）">下一句 ▶</button>
        <span class="pe-no" id="peNo"></span><span class="pe-st" id="peSt"></span>
        <button type="button" id="peSideBtn" title="改過的地方裡，可能要加入詞庫的詞（每改 10 句自動檢查）">📖 詞庫候選 <b id="peSideN" class="pe-badge"></b></button></div>`;
    box.appendChild(ed);
    const ta = $("#peText"), st = $("#peSt");
    const starts = sg.map(s => s.start);
    const find = t => { let lo = 0, hi = starts.length - 1, k = 0; while (lo <= hi) { const m = (lo + hi) >> 1; if (starts[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; } return k; };
    let cur = -1, editing = false, typingT = null, sinceCheck = 0;
    const flagMap = {};
    const addFlags = fs => fs.forEach(f => { const a = (flagMap[f.i] ||= []); if (!a.some(x => x.word === f.word)) a.push(f); });
    addFlags(flagged);
    const CHECK_EVERY = 10;
    const status = (t, cls) => { st.textContent = t; st.className = "pe-st " + (cls || ""); };
    const show = k => {
      cur = k;
      $("#pePrev").textContent = k > 0 ? sg[k - 1].text : "";
      $("#peNext").textContent = k < sg.length - 1 ? sg[k + 1].text : "";
      $("#peNo").textContent = `第 ${k + 1}／${sg.length} 句 ${dur(sg[k].start)}`;
      const fl = (flagMap[k] || []).filter(f => sg[k].text.includes(f.word));  // 詞庫「只提醒」：這句有可能聽錯的詞
      $("#peHint").textContent = fl.length ? "⚠ " + fl.map(f => `「${f.word}」可能是 ${f.maybe.join("／")}`).join("；") : "";
      const d = getDrafts(id);
      ta.value = d[k] != null ? d[k] : sg[k].text;
      ta.classList.toggle("draft", d[k] != null);
    };
    const commit = async () => {  // 存這一句：先存瀏覽器草稿，再存到伺服器；失敗就保留草稿自動重試
      if (cur < 0) return true;
      const text = ta.value.replace(/\s*\n\s*/g, " ").trim();
      if (text === sg[cur].text) { setDraft(id, cur, null); ta.classList.remove("draft"); return true; }
      setDraft(id, cur, text);
      status("存檔中…");
      try {
        await saveSeg(id, cur, text);
        setDraft(id, cur, null); ta.classList.remove("draft"); onEdit(cur, text);
        status("✓ 已存", "ok");
        if (++sinceCheck >= CHECK_EVERY) checkCands(true);  // 每改 10 句檢查一次有沒有要加入詞庫的詞
        return true;
      } catch (e) {
        status("⚠ 尚未存檔，稍後自動重試", "bad"); return false;
      }
    };
    const go = async k => { await commit(); k = Math.max(0, Math.min(sg.length - 1, k)); pl.currentTime = sg[k].start + 0.01; show(k); };
    pl.addEventListener("timeupdate", () => { if (!editing) { const k = find(pl.currentTime); if (k !== cur) show(k); } });
    ta.addEventListener("focus", () => { editing = true; if (!pl.paused) pl.pause(); });
    ta.addEventListener("blur", () => { editing = false; commit(); });
    ta.addEventListener("input", () => {  // 打字途中每 0.5 秒先存草稿（關掉頁面也不會遺失）
      clearTimeout(typingT); ta.classList.add("draft");
      typingT = setTimeout(() => { setDraft(id, cur, ta.value); status("草稿已暫存"); }, 500);
    });
    ta.addEventListener("keydown", async e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (await commit()) { ta.blur(); pl.play().catch(() => { }); } }
      else if (e.key === "Escape") { e.preventDefault(); setDraft(id, cur, null); ta.value = sg[cur].text; ta.classList.remove("draft"); ta.blur(); }
      else if (e.key === "Tab" || (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight"))) {  // 編輯中用 Tab 或 Alt+左右鍵換句
        e.preventDefault(); await go(cur + (e.shiftKey || e.key === "ArrowLeft" ? -1 : 1)); ta.focus(); }
      else if (e.key === " " && e.ctrlKey) { e.preventDefault(); pl.currentTime = sg[cur].start + 0.01; pl.play().catch(() => { }); }
    });
    /* --- 側邊欄：改過的地方裡，可能要加入詞庫的詞（陸續累積，一次加入） --- */
    const side = document.createElement("aside");
    side.className = "proof-side"; side.hidden = true; side.setAttribute("aria-label", "詞庫候選");
    side.innerHTML = `<div class="ps-h"><b>詞庫</b><span class="note" id="psName"></span>
        <button type="button" class="ps-manbtn" id="psManBtn" aria-expanded="false">＋ 自己新增</button><button type="button" class="ps-close" aria-label="關閉">✕</button></div>
      <div class="ps-man" id="psMan" hidden>
        <p class="note">當下沒錯也可以先記下專有名詞。可先在字幕上選取文字再按「＋ 自己新增」自動帶入。</p>
        <label>正確寫法<input type="text" id="pmTerm" aria-label="正確寫法"></label>
        <label>常見誤寫（選填）<input type="text" id="pmWrong" placeholder="不知道可空白" aria-label="常見誤寫"></label>
        <select id="pmMode" class="pm" aria-label="處理方式"><option value="term">只加正確寫法（當辨識提示，不改字幕）</option><option value="job">加正確寫法＋只在這份逐字稿改正（不記誤寫）</option><option value="flag">記誤寫，只提醒</option><option value="rep">記誤寫，自動替換（以後的檔案也會改）</option></select>
        <div class="ps-acts"><button type="button" class="btn primary" id="pmAdd">套用</button><button type="button" class="btn" id="pmCancel">取消</button></div></div>
      <div class="ps-dyn"></div>`;
    box.appendChild(side);
    const dyn = side.querySelector(".ps-dyn");
    let lastSel = "";  // 字幕編輯框最後選取的文字（按按鈕時選取範圍還在，但先記下來保險）
    ["select", "mouseup", "keyup"].forEach(ev => ta.addEventListener(ev, () => { const v = ta.value.slice(ta.selectionStart, ta.selectionEnd).trim(); if (v) lastSel = v; }));
    ed.addEventListener("mouseup", () => { const v = (window.getSelection()?.toString() || "").trim(); if (v) lastSel = v; });  // 上一句／下一句也可選取
    new MutationObserver(() => box.classList.toggle("side-open", !side.hidden)).observe(side, { attributes: true, attributeFilter: ["hidden"] });
    const gName = () => $("#glName")?.value || (info?.glossaries || [])[0] || "";
    const skipKey = "proofSkip:" + id;
    const getSkip = () => { try { return new Set(JSON.parse(store.get(skipKey, "[]"))); } catch { return new Set(); } };
    const addSkip = keys => { const st2 = getSkip(); keys.forEach(k => st2.add(k)); store.set(skipKey, JSON.stringify([...st2])); };
    const ckey = x => `${x.wrong}→${x.right}`;
    let cands = [], judgeBy = "";
    const KINDS = { mis: "加到「{t}」的常見誤寫", new: "新增詞" };
    // 處理方式：誤寫是短詞或常用詞時，自動替換容易改錯 → 預設只加正確寫法（當辨識提示，不記誤寫）
    const hanN = t => (t.match(/[\u3400-\u9fff]/g) || []).length;
    const shortW = w => !/[A-Za-z]{3,}/.test(w) && hanN(w) <= 2;
    const defMode = x => shortW(x.wrong) ? "term" : x.risky ? "job" : "rep";
    const MODES = { term: "只加正確寫法（當辨識提示，不改字幕）", job: "加正確寫法＋只在這份逐字稿改正（不記誤寫）", flag: "記誤寫，只提醒", rep: "記誤寫，自動替換（以後的檔案也會改）" };
    const ctxHtml = x => `<span class="ps-ctx">…${esc(x.pre || "")}<del>${esc(x.wrong)}</del><ins>${esc(x.right)}</ins>${esc(x.post || "")}…</span>`;
    const item = (x, k) => `<div class="ps-it${x.likely ? "" : " ps-low"}" data-k="${k}">
        <input type="checkbox" class="pk" ${x.likely ? "checked" : ""} aria-label="加入">
        <div class="ps-body">
          <div class="ps-meta"><a href="#" data-go="${x.i}">第 ${x.i + 1} 句</a>・${esc(x.why)}${x.kind === "mis" ? `・加到「${esc(x.term)}」` : ""}</div>
          <div class="ps-c">${ctxHtml(x)}</div>
          <div class="ps-w"><button type="button" class="ps-ext" data-ext="L" title="往前多帶一個字" ${x.pre ? "" : "disabled"}>＋◀</button>
            <input type="text" class="pw" value="${esc(x.wrong)}" aria-label="誤寫"> → <input type="text" class="pr" value="${esc(x.right)}" aria-label="正確寫法">
            <button type="button" class="ps-ext" data-ext="R" title="往後多帶一個字" ${x.post ? "" : "disabled"}>▶＋</button></div>
          <select class="pm" aria-label="處理方式">${Object.entries(MODES).map(([v, t]) => `<option value="${v}" ${defMode(x) === v ? "selected" : ""}>${t}</option>`).join("")}</select>
          ${defMode(x) === "term" ? `<div class="ps-warn">誤寫只有兩個字以下，自動替換容易改錯，預設不改字幕；要改請選其他處理方式或按「＋◀／▶＋」擴大範圍</div>`
            : defMode(x) === "job" ? `<div class="ps-warn">原稿寫法可能是常用詞，預設只在這份逐字稿改正、不記進詞庫的誤寫</div>` : ""}</div>
        <button type="button" class="ps-x" aria-label="略過這一項，之後不再列出">略過</button></div>`;
    const renderSide = () => {
      const hi = cands.filter(x => x.likely), lo = cands.filter(x => !x.likely);
      $("#peSideN").textContent = hi.length || "";
      $("#psName").textContent = `「${gName() || "未選"}」`;
      dyn.innerHTML = `<div class="ps-sub">可能要加入詞庫（從你改過的地方找）</div>
        <div class="ps-list">${hi.length ? hi.map(x => item(x, cands.indexOf(x))).join("") : `<p class="note">目前沒有待處理的詞。</p>`}
        ${lo.length ? `<details><summary>其他修改 ${lo.length} 處（比較像一般用字修正）</summary>${lo.map(x => item(x, cands.indexOf(x))).join("")}</details>` : ""}</div>
        <div class="ps-foot">${cands.length ? `<button type="button" class="btn primary ps-go" id="psAdd">套用勾選</button>
          <p class="note">勾選的詞寫進詞庫，並依處理方式改正左邊字幕（你改過的句子不動）。不要的按「略過」，之後不再列出。</p>` : ""}
          <p class="note">每改 ${CHECK_EVERY} 句自動檢查一次｜${esc(judgeBy || "")}</p></div>`;
    };
    const checkCands = async open => {
      sinceCheck = 0;
      const name = gName(); if (!name) return;
      let list;
      const nt = side.querySelector(".ps-sub");
      if (nt && info?.llm?.ready) nt.textContent = "本機 LLM 判讀中…";
      try {
        const fd = new FormData(); fd.append("name", name);
        const r = await api(`/api/jobs/${encodeURIComponent(id)}/judge`, { method: "POST", body: fd });
        list = r.items; judgeBy = r.llm ? (r.error ? `本機 LLM 判讀失敗（${r.error}），改用規則` : "本機 LLM 判讀") : "規則判斷（到「參數設定 → 本機 LLM」下載模型可更準）";
      } catch { return; }
      const sk = getSkip(), before = new Set(cands.map(ckey));
      list.forEach(x => { x.key0 = ckey(x); });  // 擴大範圍會改到 wrong／right，略過與已套用用原本的鍵記錄
      cands = list.filter(x => x.kind !== "known" && !sk.has(x.key0));
      renderSide();
      const fresh = cands.filter(x => x.likely && !before.has(ckey(x))).length;
      if (open && fresh) { side.hidden = false; status(`📖 發現 ${fresh} 個可能要加入詞庫的詞`, "ok"); }
    };
    $("#peSideBtn").addEventListener("click", () => { side.hidden = !side.hidden; if (!side.hidden) checkCands(false); });
    side.addEventListener("click", async e => {
      const t = e.target;
      if (t.closest(".ps-close")) { side.hidden = true; return; }
      const g = t.closest("[data-go]"); if (g) { e.preventDefault(); go(+g.dataset.go); return; }
      const ex = t.closest(".ps-ext");
      if (ex) {  // 擴大範圍：誤寫與正確寫法同時多帶前（後）一個字
        const row = ex.closest(".ps-it"), x = cands[+row.dataset.k];
        if (ex.dataset.ext === "L" && x.pre) { const c = x.pre.slice(-1); x.pre = x.pre.slice(0, -1); x.wrong = c + row.querySelector(".pw").value; x.right = c + row.querySelector(".pr").value; }
        if (ex.dataset.ext === "R" && x.post) { const c = x.post[0]; x.post = x.post.slice(1); x.wrong = row.querySelector(".pw").value + c; x.right = row.querySelector(".pr").value + c; }
        const sel = row.querySelector(".pk").checked, mode = row.querySelector(".pm").value;
        row.outerHTML = item(x, +row.dataset.k);
        const nr = side.querySelector(`.ps-it[data-k="${row.dataset.k}"]`);
        nr.querySelector(".pk").checked = sel; nr.querySelector(".pm").value = mode;
        return;
      }
      if (t.closest(".ps-x")) { const k = +t.closest(".ps-it").dataset.k; addSkip([cands[k].key0 || ckey(cands[k])]); cands.splice(k, 1); renderSide(); return; }
      if (t.id === "psManBtn") {
        const m = $("#psMan"); m.hidden = !m.hidden; t.setAttribute("aria-expanded", String(!m.hidden));
        if (!m.hidden) {
          const sel = (window.getSelection()?.toString() || "").trim() || lastSel;
          if (sel && sel.length <= 30) $("#pmTerm").value = sel;
          $("#pmTerm").focus();
        }
        return;
      }
      if (t.id === "pmCancel") { $("#psMan").hidden = true; $("#pmTerm").value = $("#pmWrong").value = ""; return; }
      if (t.id === "pmAdd") {
        const term = $("#pmTerm").value.trim(), wrong = $("#pmWrong").value.trim(), m = $("#pmMode").value;
        if (!term) { toast("請填正確寫法"); $("#pmTerm").focus(); return; }
        if (wrong && m === "term") toast("有填常見誤寫，但處理方式是「只加正確寫法」：誤寫不會記下");
        if (await addItems([{ term, wrong: m === "term" || m === "job" ? "" : wrong, flag: m === "flag", local: m === "job" ? wrong : "" }])) {
          $("#pmTerm").value = $("#pmWrong").value = ""; lastSel = ""; $("#psMan").hidden = true;
        }
        return;
      }
      if (t.id === "psAdd") {
        const rows = [...side.querySelectorAll(".ps-it")].filter(r => r.querySelector(".pk").checked);
        const items = rows.map(r => { const m = r.querySelector(".pm").value, w = r.querySelector(".pw").value.trim(), tm = r.querySelector(".pr").value.trim();
          return { wrong: m === "term" || m === "job" ? "" : w, term: tm, flag: m === "flag", local: m === "job" ? w : "" }; });
        if (!items.length) { toast("請先勾選要加入的項目"); return; }
        if (await addItems(items)) {  // 套用過的項目從清單移除、之後不再列出；沒勾的留著
          const done = rows.map(r => cands[+r.dataset.k]);
          addSkip(done.map(x => x.key0 || ckey(x)));
          cands = cands.filter(x => !done.includes(x)); renderSide();
          if (!cands.length) status(`✓ 已套用，詞庫候選都處理完了`, "ok");
        }
      }
    });
    side.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.closest("#psMan input")) { e.preventDefault(); $("#pmAdd").click(); } });
    async function addItems(items) {
        const name = gName();
        if (!name) { toast("還沒有詞庫"); return false; }
        try {
          await commit();  // 先存好正在改的這一句，再寫詞庫、套用
          const r = await api(`/api/glossary/${encodeURIComponent(name)}/add`, { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items: items.map(({ local, ...x }) => x) }) });
          let msg = `已加入詞庫：常見誤寫 ${r.mis} 筆、新詞 ${r.new} 筆${r.exists ? `，${r.exists} 筆已存在` : ""}`;
          const apply = true;  // 一律改正這份逐字稿還沒校對過的句子
          let nFix = 0;
          // 套用範圍：整份逐字稿還沒校對過的句子（你改過的句子不動）
          if (apply && items.some(x => x.wrong)) {  // 詞庫的自動替換／只提醒
            const fd = new FormData(); fd.append("name", name); fd.append("from_i", "0");
            const a = await api(`/api/jobs/${encodeURIComponent(id)}/glossary`, { method: "POST", body: fd });
            (a.changed || []).forEach(c => onEdit(c.i, c.text, false));
            nFix += a.changed?.length || 0;
            addFlags(a.flagged || []);
            if (a.flagged?.length) msg += `；${new Set(a.flagged.map(f => f.i)).size} 句標示提醒`;
          }
          const loc = items.filter(x => x.local).map(x => ({ wrong: x.local, right: x.term }));
          if (apply && loc.length) {  // 只在這份逐字稿改正
            const a = await api(`/api/jobs/${encodeURIComponent(id)}/replace`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pairs: loc, from_i: 0 }) });
            (a.changed || []).forEach(c => onEdit(c.i, c.text, false));
            nFix += a.changed?.length || 0;
          }
          if (apply) msg += nFix ? `；逐字稿 ${nFix} 句已改正` : "；逐字稿裡沒有要改正的句子";
          const onlyTerm = items.filter(x => !x.wrong && !x.local).length;
          if (onlyTerm) msg += `（${onlyTerm} 項是「只加正確寫法」，不會改字幕）`;
          show(cur);
          toast(msg);
          return true;
        } catch (err) { toast(err.message); return false; }
    }
    checkCands(false);
    let navTo = null;  // 連按左右鍵時累計目標句（換句是非同步的）
    proofNav = d => { navTo = Math.max(0, Math.min(sg.length - 1, (navTo ?? cur) + d)); const k = navTo; go(k).then(() => { if (navTo === k) navTo = null; }); };
    $("#pePrevBtn").addEventListener("click", () => go(cur - 1));
    $("#peNextBtn").addEventListener("click", () => go(cur + 1));
    $("#peReplay").addEventListener("click", async () => { await commit(); pl.currentTime = sg[cur].start + 0.01; pl.play().catch(() => { }); });
    show(find(pl.currentTime));
    // 先前沒存到的修改：打開頁面就補存；之後每 10 秒重試
    flushDrafts(id, onEdit).then(n => { if (n > 0) show(cur); });
    proofRetry = setInterval(async () => {
      if (!$("#peText")) { clearInterval(proofRetry); return; }
      if (Object.keys(getDrafts(id)).length && !editing) { const n = await flushDrafts(id, onEdit, true); if (n > 0) status(`✓ 已補存 ${n} 句`, "ok"); }
    }, 10000);
  };

  /* ---------- 整理稿（階段 8，0.13.0）：本機 LLM 加標點、分段、小標題 ---------- */
  const BG_NAME = { tidy: "整理逐字稿", diarize: "分辨說話者", translate: "翻譯成中文" };
  const bgHtml = b => b ? `<div class="bg-task" id="bgTask" title="${esc(b.stage || "")}">
      <span class="bg-lbl" id="bgLbl">${esc(BG_NAME[b.kind] || "處理中")}${b.state === "queued" ? "：排隊中" : `：${Math.round((b.progress || 0) * 100)}%`}</span>
      <span class="bar"><i id="bgBar" style="width:${Math.round((b.progress || 0) * 100)}%"></i></span>
      <span class="note" id="bgStage">${esc(b.stage || "")}</span>
      <button class="btn small" id="bgCancel" type="button">取消</button></div>` : "";
  const updBg = b => {
    const p = Math.round((b.progress || 0) * 100);
    if ($("#bgBar")) $("#bgBar").style.width = p + "%";
    if ($("#bgLbl")) $("#bgLbl").textContent = `${BG_NAME[b.kind] || "處理中"}${b.state === "queued" ? "：排隊中" : `：${p}%`}`;
    if ($("#bgStage")) $("#bgStage").textContent = b.stage || "";
    if ($("#bgTask")) $("#bgTask").title = b.stage || "";
  };
  const bindBg = (id, j) => $("#bgCancel")?.addEventListener("click", async () => { if (await cancelJob(id, j.name)) toast("已取消"); });
  const tidyPanel = j => {
    const t = j.tidy, ready = info?.llm?.ready, id = j.id;
    if (j.bg?.kind === "tidy") return `<div class="spk-panel tidy-panel"><h3>整理稿</h3>${bgHtml(j.bg)}
      ${t ? `<details class="tidy-prev"><summary>上次的整理稿</summary><div class="tidy-text">${esc(t.text).replace(/^■ (.+)$/gm, "<b class=\"tidy-h\">$1</b>")}</div></details>` : ""}</div>`;
    const btn = `<button class="btn${t ? "" : " primary"}" id="tidyGo" ${ready ? "" : "disabled title=\"需要本機 LLM：到「參數設定 → 本機 LLM」下載\""}>${t ? "重新整理" : "整理逐字稿"}</button>`;
    const note = !ready ? `<span class="note">需要本機 LLM：到「參數設定 → 本機 LLM」下載</span>`
      : t ? `<span class="note">${esc(t.created || "")}｜${esc(t.model || "")}${t.seconds ? `｜${human(t.seconds)}` : ""}｜校對後可按「重新整理」</span>`
      : `<span class="note">加標點、依語意分段、加小標題，整理稿直接取代純文字檔（檔名.txt）；SRT 字幕不動，可在任務列表看進度${j.review?.edited?.length ? "" : "（建議先校對再整理）"}</span>`;
    return `<div class="spk-panel tidy-panel"><h3>整理稿</h3>
      <div class="spk-acts">${btn}${t ? `<button class="btn" id="tidyCopy">複製全文</button>` : ""}${note}</div>
      ${t ? `<p class="note">上方「下載純文字」下載的就是這份整理稿。需要一句一行的版本：<a href="/api/jobs/${encodeURIComponent(id)}/download/raw">下載未整理版</a></p>` : ""}
      ${t?.stale ? `<p class="state warn">逐字稿在整理後又校對過，整理稿還是舊的內容，建議按「重新整理」。</p>` : ""}
      ${j.tidy_error ? `<p class="state error">整理失敗：${esc(j.tidy_error)}</p>` : ""}
      ${t?.warnings?.length ? `<p class="state error">⚠ ${t.warnings.map(esc).join("<br>⚠ ")}</p>` : ""}
      ${t ? `<details class="tidy-prev" open><summary>預覽</summary><div class="tidy-text">${esc(t.text).replace(/^■ (.+)$/gm, "<b class=\"tidy-h\">$1</b>")}</div></details>` : ""}</div>`;
  };
  const refreshTidy = async id => {  // 只更新整理稿這一區，影片與逐字稿不重新載入
    let j; try { j = await api(`/api/jobs/${encodeURIComponent(id)}`); } catch { return; }
    const el = document.querySelector(".tidy-panel"); if (!el) return;
    el.outerHTML = tidyPanel(j); bindTidy(id, j); if (j.bg) { bindBg(id, j); lastBg = j.bg; }
  };
  const bindTidy = (id, j) => {
    $("#tidyGo")?.addEventListener("click", async () => {
      if (j.tidy && !(await confirmBox("重新整理", "會用目前（校對後）的逐字稿重新整理，覆蓋上次的整理稿。確定嗎？", "重新整理"))) return;
      try { await api(`/api/jobs/${encodeURIComponent(id)}/tidy`, { method: "POST" }); toast("已開始整理逐字稿（可繼續看影片或校對）"); refreshTidy(id); }
      catch (e) { toast(e.message); }
    });
    $("#tidyCopy")?.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(j.tidy.text); toast("已複製整理稿全文"); } catch { toast("無法複製，請改用「下載整理稿」"); }
    });
  };

  /* ---------- 專有名詞庫與校對（0.11.0） ---------- */
  const measureHtml = m => {
    if (!m) return "";
    const pct = m.term_acc == null ? "—" : (m.term_acc * 100).toFixed(1) + "%";
    const rows = m.terms.map(t => `<tr class="${t.ok < t.ref ? "miss" : ""}"><td>${esc(t.term)}</td><td class="num">${t.ref}</td><td class="num">${t.ok}</td>
      <td>${t.wrong.map(w => `<span class="wrongw">${esc(w)}</span>${t.known.includes(w) ? "<small>（詞庫已有）</small>" :
        `<button type="button" class="btn small" data-addmis="${esc(t.term)}" data-wrong="${esc(w)}">加入誤寫</button>`}`).join(" ")}
        ${t.fp ? `<span class="note">多出 ${t.fp} 次（可能換錯）</span>` : ""}</td></tr>`).join("");
    return `<div class="measure">
      <p><b class="big-num">${pct}</b> 專有名詞正確率（${m.terms_ok}／${m.terms_total} 次）｜字錯率 ${(m.cer * 100).toFixed(1)}%｜範圍 ${m.start_min}～${m.end_min} 分鐘、${m.lines} 句（改過 ${m.edited_lines} 句）｜詞庫「${esc(m.glossary)}」｜${esc(m.created)}</p>
      ${m.terms.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>專有名詞</th><th>出現</th><th>正確</th><th>模型聽成（可一鍵加入詞庫）</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="note">這個範圍沒有出現詞庫裡的專有名詞。</p>`}
      <p class="note">正確率＝校對後出現的專有名詞中，辨識原稿就寫對的比例（逐句比對）。字錯率＝原稿與校對版的文字差異比例。</p></div>`;
  };
  const glossPanel = j => {
    const gs = info?.glossaries || [], g = j.glossary, rv = j.review || {};
    const opts = gs.map(x => `<option value="${esc(x)}" ${g?.name === x ? "selected" : ""}>${esc(x)}</option>`).join("");
    const rep = g ? `<details class="gl-rep"><summary>已套用「${esc(g.name)}」：替換 ${g.replaced.reduce((a, r) => a + r.count, 0)} 處、提醒 ${g.flagged.length} 處（${esc(g.created || "")}）</summary>
      ${g.replaced.length ? `<p class="note">自動替換：</p><ul>${g.replaced.slice(0, 200).map(r => `<li><a href="#" data-goto="${r.i}">第 ${r.i + 1} 句</a>：${esc(r.from)} → <b>${esc(r.to)}</b>${r.count > 1 ? ` ×${r.count}` : ""}</li>`).join("")}</ul>` : ""}
      ${g.flagged.length ? `<p class="note">只提醒（本身也是常用詞，請自行判斷）：</p><ul>${g.flagged.slice(0, 200).map(r => `<li><a href="#" data-goto="${r.i}">第 ${r.i + 1} 句</a>：「${esc(r.word)}」可能是 ${r.maybe.map(esc).join("／")}</li>`).join("")}</ul>` : ""}
    </details>` : "";
    return `<div class="spk-panel gl-panel"><h3>專有名詞與校對</h3>
      <div class="spk-acts"><select id="glName" aria-label="詞庫">${opts}</select>
        <button class="btn" id="glApply" ${gs.length ? "" : "disabled"}>套用詞庫</button>
        <button class="btn" id="proofBtn" aria-pressed="false">在逐字稿上修改</button>
        <span class="note" id="proofNote">${rv.edited?.length ? `已校對 ${rv.edited.length} 句` : "建議用上方「校對模式」：影片播放時直接修改字幕，每句自動存檔"}</span></div>
      ${rep}
      <div class="spk-acts gl-measure">正確率報告：範圍 <input type="number" id="msFrom" min="0" step="1" value="${rv.measure?.start_min ?? 0}" aria-label="起"> ～
        <input type="number" id="msTo" min="1" step="1" value="${rv.measure?.end_min ?? 10}" aria-label="迄"> 分鐘
        <button class="btn" id="msGo" ${rv.has_asr ? "" : "disabled title=\"先在校對模式修改逐字稿\""}>產生報告</button>
        <button class="btn" id="pickBtn" ${rv.has_asr ? "" : "disabled title=\"先在校對模式修改逐字稿\""}>從校對結果挑選加入詞庫</button></div>
      <div id="pickOut"></div>
      <div id="msOut">${measureHtml(rv.measure)}</div></div>`;
  };
  const bindGloss = (id, j, onEdit) => {
    const tr = $("#tr");
    $("#glApply")?.addEventListener("click", async () => {
      const fd = new FormData(); fd.append("name", $("#glName").value);
      try { const r = await api(`/api/jobs/${encodeURIComponent(id)}/glossary`, { method: "POST", body: fd });
        toast(`已套用詞庫：替換 ${r.replaced} 處、提醒 ${r.flagged} 處`); loadJob(id); } catch (e) { toast(e.message); }
    });
    document.querySelectorAll("[data-goto]").forEach(a => a.addEventListener("click", e => {
      e.preventDefault(); const p = tr?.querySelector(`p[data-i="${a.dataset.goto}"]`);
      if (p) { tr.scrollTo({ top: p.offsetTop - tr.clientHeight / 3, behavior: "smooth" }); p.classList.add("flash"); setTimeout(() => p.classList.remove("flash"), 1500); }
    }));
    const setProof = on => {
      $("#proofBtn").setAttribute("aria-pressed", on); $("#proofBtn").textContent = on ? "結束校對" : "校對模式";
      tr?.classList.toggle("proof", on);
      tr?.querySelectorAll(".t-orig").forEach(s => { if (on) s.setAttribute("contenteditable", "plaintext-only"); else s.removeAttribute("contenteditable"); });
    };
    $("#proofBtn")?.addEventListener("click", () => setProof($("#proofBtn").getAttribute("aria-pressed") !== "true"));
    tr?.addEventListener("focusin", e => { const s = e.target.closest(".t-orig[contenteditable]"); if (s) s.dataset.old = s.textContent; });
    tr?.addEventListener("keydown", e => {
      const s = e.target.closest(".t-orig[contenteditable]"); if (!s) return;
      if (e.key === "Enter") { e.preventDefault(); s.blur(); }
      if (e.key === "Escape") { s.textContent = s.dataset.old || s.textContent; s.blur(); }
    });
    tr?.addEventListener("focusout", async e => {
      const s = e.target.closest(".t-orig[contenteditable]"); if (!s) return;
      const p = s.closest("p[data-i]"), i = +p.dataset.i, text = s.textContent.trim();
      if (text === (s.dataset.old || "").trim()) return;
      try {
        const r = await api(`/api/jobs/${encodeURIComponent(id)}/segment`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ i, text }) });
        if (r.changed) { p.classList.add("edited"); onEdit(i, text); $("#proofNote").textContent = `已校對 ${r.edited} 句（已存檔，SRT／純文字已更新）`; $("#msGo").disabled = false; }
      } catch (err) { toast(err.message); s.textContent = s.dataset.old; }
    });
    $("#msGo")?.addEventListener("click", async () => {
      const fd = new FormData(); fd.append("name", $("#glName").value); fd.append("start_min", $("#msFrom").value); fd.append("end_min", $("#msTo").value);
      try { const m = await api(`/api/jobs/${encodeURIComponent(id)}/measure`, { method: "POST", body: fd }); $("#msOut").innerHTML = measureHtml(m); bindMis(); }
      catch (e) { toast(e.message); }
    });
    const bindMis = () => document.querySelectorAll("[data-addmis]").forEach(b => b.addEventListener("click", async () => {
      const fd = new FormData(); fd.append("term", b.dataset.addmis); fd.append("wrong", b.dataset.wrong);
      try { await api(`/api/glossary/${encodeURIComponent($("#glName").value)}/miswrite`, { method: "POST", body: fd });
        b.replaceWith(Object.assign(document.createElement("small"), { textContent: "（已加入詞庫）" })); toast(`已把「${b.dataset.wrong}」加入「${b.dataset.addmis}」的常見誤寫`); }
      catch (e) { toast(e.message); }
    }));
    bindMis();
    // 從校對結果挑選加入詞庫：列出每一處修改，可改寫法、勾選、設「只提醒」
    const KIND = { mis: "加到「{t}」的常見誤寫", new: "新增詞", known: "詞庫已有" };
    $("#pickBtn")?.addEventListener("click", async () => {
      let list;
      try { list = await api(`/api/jobs/${encodeURIComponent(id)}/edits?name=${encodeURIComponent($("#glName").value)}`); }
      catch (e) { toast(e.message); return; }
      if (!list.length) { $("#pickOut").innerHTML = `<p class="note">校對結果裡沒有可加入詞庫的修改（標點、空白不列入）。</p>`; return; }
      $("#pickOut").innerHTML = `<div class="tbl-wrap pick"><table class="tbl"><thead><tr><th><input type="checkbox" id="pickAll" aria-label="全選"></th>
        <th>句</th><th>模型聽成（誤寫）</th><th></th><th>正確寫法</th><th>處理方式</th><th>只提醒</th></tr></thead><tbody>
        ${list.map((x, k) => `<tr data-k="${k}" class="${x.kind === "known" ? "dim" : ""}">
          <td><input type="checkbox" class="pk" ${x.kind === "known" ? "disabled" : ""}></td>
          <td><a href="#" data-goto="${x.i}" title="${esc(x.context)}">${x.i + 1}</a></td>
          <td><input type="text" class="pw" value="${esc(x.wrong)}" aria-label="誤寫"></td><td>→</td>
          <td><input type="text" class="pr" value="${esc(x.right)}" aria-label="正確寫法"></td>
          <td class="note">${KIND[x.kind].replace("{t}", esc(x.term))}</td>
          <td><input type="checkbox" class="pf" ${x.risky ? "checked" : ""} title="誤寫本身也是常用詞時勾選：只提醒、不自動替換">${x.risky ? `<small class="warn">只是補字，自動替換會改到正常用法</small>` : ""}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="spk-acts"><button class="btn primary" id="pickAdd">加入勾選的項目</button>
        <span class="note">可先修改寫法再加入；新詞的分類為「待分類」、提示優先 2。誤寫本身也是常用詞（例如「船隻」）請勾「只提醒」。</span></div>`;
      $("#pickAll").addEventListener("change", e => $("#pickOut").querySelectorAll(".pk:not(:disabled)").forEach(c => { c.checked = e.target.checked; }));
      $("#pickOut").querySelectorAll("[data-goto]").forEach(a => a.addEventListener("click", ev => {
        ev.preventDefault(); const p = tr?.querySelector(`p[data-i="${a.dataset.goto}"]`);
        if (p) { tr.scrollTo({ top: p.offsetTop - tr.clientHeight / 3, behavior: "smooth" }); p.classList.add("flash"); setTimeout(() => p.classList.remove("flash"), 1500); }
      }));
      $("#pickAdd").addEventListener("click", async () => {
        const items = [...$("#pickOut").querySelectorAll("tbody tr")].filter(r => r.querySelector(".pk").checked).map(r => ({
          wrong: r.querySelector(".pw").value.trim(), term: r.querySelector(".pr").value.trim(), flag: r.querySelector(".pf").checked }));
        if (!items.length) { toast("請先勾選要加入的項目"); return; }
        try {
          const r = await api(`/api/glossary/${encodeURIComponent($("#glName").value)}/add`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) });
          toast(`已加入詞庫：常見誤寫 ${r.mis} 筆、新詞 ${r.new} 筆${r.exists ? `，${r.exists} 筆已存在` : ""}`);
          $("#pickBtn").click();  // 重新整理清單
        } catch (e) { toast(e.message); }
      });
    });
  };

  /* ---------- 字幕外觀（大小、位置、原文／中文顏色、背景），記住設定 ---------- */
  const CAP_DEF = { size: "m", pos: "bottom", o: "#FFFFFF", z: "#FFE45C", bg: "dark" };
  const CAP_COLORS = ["#FFFFFF", "#FFE45C", "#7FE3FF", "#A8F0A0", "#FFB0D9", "#FFB347"];
  const CAP_SIZE = { s: .8, m: 1, l: 1.25, xl: 1.55 };
  const capStyle = () => { try { return { ...CAP_DEF, ...JSON.parse(store.get("capStyle", "{}")) }; } catch { return { ...CAP_DEF }; } };
  const capStyleMenu = () => {
    const opt = (k, v, label) => `<button type="button" data-cs="${k}:${v}" class="cs-opt">${label}</button>`;
    const sw = k => CAP_COLORS.map(c => `<button type="button" data-cs="${k}:${c}" class="cs-sw" style="background:${c}" aria-label="顏色 ${c}"></button>`).join("");
    return `<div class="yt-menu-h">字幕大小</div><div class="cs-row">${opt("size", "s", "小")}${opt("size", "m", "中")}${opt("size", "l", "大")}${opt("size", "xl", "特大")}</div>
      <div class="yt-menu-h">字幕位置</div><div class="cs-row">${opt("pos", "bottom", "下方")}${opt("pos", "top", "上方")}</div>
      <div class="yt-menu-h">原文顏色</div><div class="cs-row">${sw("o")}</div>
      <div class="yt-menu-h">中文譯文顏色</div><div class="cs-row">${sw("z")}</div>
      <div class="yt-menu-h">背景</div><div class="cs-row">${opt("bg", "dark", "黑底")}${opt("bg", "half", "半透明")}${opt("bg", "none", "無（文字陰影）")}</div>`;
  };
  const applyCapStyle = box => {
    const st = capStyle();
    box.style.setProperty("--cap-scale", CAP_SIZE[st.size] || 1);
    box.style.setProperty("--cap-o", st.o); box.style.setProperty("--cap-z", st.z);
    box.classList.toggle("cap-top", st.pos === "top");
    box.classList.remove("cap-bg-dark", "cap-bg-half", "cap-bg-none"); box.classList.add(`cap-bg-${st.bg}`);
    box.querySelectorAll("[data-cs]").forEach(b => {
      const [k, v] = b.dataset.cs.split(":");
      b.setAttribute("aria-pressed", String(st[k]) === v);
    });
  };
  /* 版面切換（觀看／學習），學習模式雙擊影片也會呼叫 */
  const setView = v => {
    store.set("view", v);
    learnSide(v === "learn");
    const jv = $("#jobview"); if (!jv) return;
    jv.className = `jobview view-${v}`;
    document.querySelectorAll("[data-view]").forEach(x => x.setAttribute("aria-pressed", x.dataset.view === v));
    const tr = $("#tr"), curP = $("#tr p.cur");
    if (tr && curP) tr.scrollTo({ top: curP.offsetTop - tr.clientHeight / 3 });
    if (v === "watch") $("#player")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const bindPlayer = (id, segs) => {
    const box = $("#player"), cap = $("#cap"), tr = $("#tr");
    let pl = $("#pl");
    if (!pl || !box) return;
    const starts = segs.map(s => s.start);
    let cur = -1, rate = 1, capOn = store.get("showCap", "1") === "1", hideT = null;
    const find = t => { let lo = 0, hi = starts.length - 1, k = -1; while (lo <= hi) { const m = (lo + hi) >> 1; if (starts[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; } return k; };
    const setIcon = (btn, name, label) => { btn.innerHTML = svg(name, label); btn.setAttribute("aria-label", label); };

    // 控制列顯示／自動隱藏
    const showCtl = () => {
      box.classList.add("active"); clearTimeout(hideT);
      if (!pl.paused) hideT = setTimeout(() => { if (!box.querySelector(".yt-menu:not([hidden])")) box.classList.remove("active"); }, 2500);
    };
    box.addEventListener("mousemove", showCtl);
    box.addEventListener("mouseleave", () => { if (!pl.paused) box.classList.remove("active"); });

    const togglePlay = () => { if (pl.paused) pl.play().catch(() => { }); else pl.pause(); };
    const toggleFs = () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else box.requestFullscreen?.().catch(() => toast("這個瀏覽器不支援全螢幕"));
    };
    const applyCap = () => { cap.hidden = !capOn; $("#bCC").setAttribute("aria-pressed", capOn); };
    const updTime = () => {
      const d = pl.duration || 0, t = pl.currentTime || 0;
      $("#time").textContent = `${dur(t)} / ${dur(d)}`;
      const pct = d ? t / d * 100 : 0;
      $("#done").style.width = pct + "%"; $("#knob").style.left = pct + "%";
      if (pl.buffered.length && d) $("#buf").style.width = pl.buffered.end(pl.buffered.length - 1) / d * 100 + "%";
    };
    const onTime = () => {
      updTime();
      const t = pl.currentTime, k = find(t);
      const sg = k >= 0 && t <= segs[k].end + 0.3 ? segs[k] : null;
      if (sg && sg.html) { if (cap._k !== k || cap._h !== sg.html) { cap.innerHTML = sg.html; cap._k = k; cap._h = sg.html; } }
      else { cap.textContent = sg ? sg.text : ""; cap._k = -1; }
      cap.classList.toggle("empty", !cap.textContent);
      if (k === cur || !tr) return;
      tr.querySelector("p.cur")?.classList.remove("cur");
      cur = k;
      const p = k >= 0 ? tr.querySelector(`p[data-i="${k}"]`) : null;
      if (p) {
        p.classList.add("cur");
        if ($("#follow")?.checked && (p.offsetTop < tr.scrollTop || p.offsetTop + p.offsetHeight > tr.scrollTop + tr.clientHeight))
          tr.scrollTo({ top: p.offsetTop - tr.clientHeight / 3, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      }
    };
    const bindMedia = () => {
      pl.playbackRate = rate; pl.volume = +$("#vol").value;
      pl.addEventListener("timeupdate", onTime);
      pl.addEventListener("seeked", onTime);
      pl.addEventListener("loadedmetadata", updTime);
      pl.addEventListener("progress", updTime);
      pl.addEventListener("play", () => { box.classList.remove("paused"); setIcon($("#bPlay"), "pause", "暫停"); showCtl(); });
      pl.addEventListener("pause", () => { box.classList.add("paused", "active"); setIcon($("#bPlay"), "play", "播放"); clearTimeout(hideT); });
      pl.addEventListener("ended", () => box.classList.add("paused", "active"));
      pl.addEventListener("volumechange", () => setIcon($("#bMute"), pl.muted || pl.volume === 0 ? "mute" : "vol", pl.muted ? "取消靜音" : "靜音"));
      pl.addEventListener("error", () => {
        if (pl.tagName !== "VIDEO") { toast("這個檔案無法在瀏覽器播放"); return; }
        // 影片編碼瀏覽器不支援（例如部分 mov）→ 改播轉好的 MP3
        const a = document.createElement("audio");
        a.id = "pl"; a.preload = "metadata"; a.src = `/api/jobs/${encodeURIComponent(id)}/download/mp3`;
        pl.replaceWith(a); pl = a;
        box.classList.replace("video", "audio");
        box.insertAdjacentHTML("afterbegin", `<div class="audio-bg" aria-hidden="true">${$("#wave")?.outerHTML.replace('id="wave"', "") || ""}</div>`);
        toast("這個影片格式瀏覽器無法播放，改播聲音");
        bindMedia();
      }, { once: true });
    };
    bindMedia(); applyCap(); showCtl();

    // 點畫面：播放／暫停；雙擊：全螢幕（控制列上的點擊不算）
    box.addEventListener("click", e => {
      if (e.target.closest(".yt-ctl,.proof-ed,.proof-side")) return;
      const openMenu = box.querySelector(".yt-menu:not([hidden])");
      if (openMenu) { openMenu.hidden = true; return; }
      togglePlay(); box.focus({ preventScroll: true });
    });
    box.addEventListener("dblclick", e => {
      if (e.target.closest(".yt-ctl,.proof-ed,.proof-side")) return;
      if ($("#jobview")?.classList.contains("view-learn")) setView("watch");  // 學習模式雙擊：切到觀看模式
      else toggleFs();
    });
    document.addEventListener("fullscreenchange", () => {
      const on = document.fullscreenElement === box;
      box.classList.toggle("fs", on); setIcon($("#bFs"), on ? "fsx" : "fs", on ? "結束全螢幕" : "全螢幕");
    });

    $("#bPlay").addEventListener("click", togglePlay);
    $("#bFs").addEventListener("click", toggleFs);
    $("#bMute").addEventListener("click", () => { pl.muted = !pl.muted; });
    $("#vol").addEventListener("input", e => { pl.volume = +e.target.value; pl.muted = pl.volume === 0; });
    $("#bCC").addEventListener("click", () => { capOn = !capOn; store.set("showCap", capOn ? "1" : "0"); applyCap(); });
    $("#bRate").addEventListener("click", () => {
      $("#styleMenu").hidden = true;
      const m = $("#rateMenu"); m.hidden = !m.hidden; $("#bRate").setAttribute("aria-expanded", !m.hidden);
    });
    $("#bStyle").addEventListener("click", () => {
      $("#rateMenu").hidden = true;
      const m = $("#styleMenu"); m.hidden = !m.hidden; $("#bStyle").setAttribute("aria-expanded", !m.hidden);
    });
    $("#styleMenu").addEventListener("click", e => {
      e.stopPropagation();
      const b = e.target.closest("[data-cs]"); if (!b) return;
      const [k, v] = b.dataset.cs.split(":"); const st = capStyle(); st[k] = v; store.set("capStyle", JSON.stringify(st));
      applyCapStyle(box);
    });
    applyCapStyle(box);
    box.querySelectorAll("[data-rate]").forEach(b => b.addEventListener("click", () => {
      rate = +b.dataset.rate; pl.playbackRate = rate;
      $("#rateTxt").textContent = rate === 1 ? "1×" : rate + "×";
      box.querySelectorAll("[data-rate]").forEach(x => x.setAttribute("aria-checked", x === b));
      $("#rateMenu").hidden = true; $("#bRate").setAttribute("aria-expanded", "false");
    }));

    // 進度條：點擊或拖曳跳轉，滑過顯示時間
    const prog = $("#prog");
    const posAt = e => { const r = prog.getBoundingClientRect(); return Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1); };
    let dragging = false;
    prog.addEventListener("pointerdown", e => { dragging = true; prog.setPointerCapture(e.pointerId); pl.currentTime = posAt(e) * (pl.duration || 0); updTime(); });
    prog.addEventListener("pointermove", e => {
      const f = posAt(e), tip = $("#tip");
      tip.textContent = dur(f * (pl.duration || 0)); tip.style.left = f * 100 + "%";
      if (dragging) { pl.currentTime = f * (pl.duration || 0); updTime(); }
    });
    prog.addEventListener("pointerup", () => { dragging = false; });

    // 鍵盤
    // 左右鍵在整個任務頁都有效（不必先點播放器）：觀看／學習模式倒退／快轉 5 秒；校對模式換上一句／下一句
    playerKeys = e => {
      const t = e.target;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || t.isContentEditable || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const d = e.key === "ArrowLeft" ? -1 : 1;
      if ($("#jobview")?.classList.contains("view-proof") && proofNav) proofNav(d);
      else pl.currentTime = Math.min(Math.max(0, pl.currentTime + d * 5), pl.duration || 0);
      showCtl();
    };
    box.addEventListener("keydown", e => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.closest(".proof-ed,.proof-side")) return;  // 校對打字時不觸發快捷鍵
      const k = e.key.toLowerCase();
      if (k === " " || k === "k") { e.preventDefault(); togglePlay(); }
      else if (k === "f") toggleFs();
      else if (k === "c") $("#bCC").click();
      else if (k === "m") $("#bMute").click();
      else return;
      showCtl();
    });

    if (tr) {
      tr.addEventListener("click", e => {
        const p = e.target.closest("p[data-i]"); if (!p) return;
        if (tr.classList.contains("proof") && !e.target.closest("time")) return;  // 校對模式：點文字是編輯，點時間才跳轉
        pl.currentTime = segs[+p.dataset.i].start + 0.01; pl.play().catch(() => { });
      });
      tr.addEventListener("keydown", e => { if (e.key === "Enter" && !tr.classList.contains("proof")) e.target.closest("p[data-i]")?.click(); });
    }
  };

  /* ---------- 任務詳情：影片在最上面，下面才是資訊、下載與逐字稿 ---------- */
  let lastJobState = "", lastBg = null;
  const loadJob = async (id, quiet) => {
    let j;
    try { j = await api(`/api/jobs/${encodeURIComponent(id)}`); }
    catch (e) { if (!quiet) $("#jobDetail").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    if (quiet && j.state === "done" && lastJobState === "done") {  // 後續處理（整理等）：只更新進度，不重畫畫面
      if (j.bg) { updBg(j.bg); lastBg = j.bg; return; }
      if (lastBg) {
        const k = lastBg.kind; lastBg = null;
        if (k === "tidy") { refreshTidy(id); toast("整理稿完成"); return; }
        // 分辨說話者、翻譯會改變逐字稿：重畫整頁
      } else return;
    }
    if (quiet && j.state === lastJobState && j.state !== "running" && j.state !== "queued") return;
    lastBg = j.bg || null;
    lastJobState = j.state;
    const s = j.summary, pct = Math.round((j.progress || 0) * 100);
    const done = j.state === "done";
    let html = "";
    // 版面：觀看模式（影片在上、字幕疊在畫面）／學習模式（影片在左、逐字稿在右，可雙語對照）
    const view = store.get("view", "watch");
    const tl = Array.isArray(j.translation?.lines) ? j.translation.lines : null;
    let lang = tl ? store.get("lang", "bi") : "orig";
    html += `<div class="jobview view-${["learn", "proof"].includes(view) ? view : "watch"}" id="jobview">`;
    if (done && j.media) {
      const canTr = !tl && j.src_lang && j.src_lang !== "zh", trReady = info?.translator?.ready;
      html += `<div class="jv-bar viewbar">
        <div class="segctl" role="group" aria-label="版面">
          <button type="button" data-view="watch" aria-pressed="${view !== "learn"}">觀看模式</button>
          <button type="button" data-view="learn" aria-pressed="${view === "learn"}">學習模式</button>
          <button type="button" data-view="proof" aria-pressed="${view === "proof"}">校對模式</button></div>
        ${tl ? `<label class="langsel">字幕 <select id="langSel">
          <option value="orig">原文</option><option value="zh">中文</option><option value="bi">雙語對照</option></select></label>` : ""}
        ${canTr ? `<button type="button" class="btn small" id="trBtn" ${trReady ? "" : "disabled"}>翻譯成中文（雙語對照）</button>
          ${trReady ? "" : `<span class="note">需要管理者先執行 admin-download-translator.cmd 準備翻譯模型</span>`}` : ""}
        ${j.trans_error ? `<span class="state error">翻譯失敗：${esc(j.trans_error)}</span>` : ""}${j.tidy_error ? `<span class="state error">整理失敗：${esc(j.tidy_error)}</span>` : ""}
      </div>`;
      html += `<div class="jv-player">${playerHtml(id, j.media.kind)}</div>`;
    }
    html += `<div class="jv-info"><div class="job-head"><h2>${esc(j.name)}</h2>`;
    if (j.url) html += `<p class="src-url">來源：<a href="${esc(j.url)}" target="_blank" rel="noopener noreferrer">${esc(j.url)}</a></p>`;
    if (s) {
      html += `<div class="facts"><span>錄音長度 <b>${dur(s.duration)}</b></span><span>辨識耗時 <b>${human(s.asr_sec)}</b></span>
        <span>總耗時 <b>${human(s.total_sec)}</b></span><span>${esc(MODEL[s.model] || s.model)} × ${esc(DEV[s.device] || s.device)}</span>
        <span><b>${s.segments}</b> 句、<b>${s.chars}</b> 字</span>${s.chunk_min ? `<span>每 ${s.chunk_min} 分鐘切段</span>` : ""}</div>`;
    } else if (j.state === "running" || j.state === "queued") {
      html += `<div class="facts"><span>${esc(j.stage || STATE[j.state])}</span><span><b>${pct}%</b></span>${j.elapsed ? `<span>已進行 ${minsec(j.elapsed)}</span>` : ""}</div>
        <div class="bar" style="margin-bottom:1rem"><i style="width:${pct}%"></i></div>
        <button class="btn" id="cancelJob">取消轉檔</button>`;
    } else if (j.state === "cancelled") {
      html += `<p class="state cancelled">已取消。需要的話請重新${j.url ? "貼上網址" : "上傳檔案"}。</p>`;
    } else if (j.error) {
      html += `<p class="state error">失敗：${esc(j.error)}</p>`;
    } else if (j.state === "interrupted") {
      html += `<p class="state interrupted">這個任務在處理途中程式被關閉，請重新${j.url ? "貼上網址" : "上傳檔案"}。</p>`;
    }
    html += `</div>`;
    const f = j.files || {};
    if (done) {
      html += `<div class="actions">
        ${f.srt ? `<a class="btn primary" href="/api/jobs/${encodeURIComponent(id)}/download/srt">下載 SRT 字幕</a>` : ""}
        ${f.txt ? `<a class="btn" href="/api/jobs/${encodeURIComponent(id)}/download/txt" title="${f.tidy ? "整理稿（有標點、分段、小標題）" : "一句一行"}">下載純文字${f.tidy ? "（整理稿）" : ""}</a>` : ""}
        ${f.bi ? `<a class="btn" href="/api/jobs/${encodeURIComponent(id)}/download/bi">下載雙語字幕</a>` : ""}
        ${f.zh ? `<a class="btn" href="/api/jobs/${encodeURIComponent(id)}/download/zh">下載中文字幕</a>` : ""}
        ${f.mp3 ? `<a class="btn" href="/api/jobs/${encodeURIComponent(id)}/download/mp3">下載 MP3</a>` : ""}
        ${f.video ? `<a class="btn" href="/api/jobs/${encodeURIComponent(id)}/download/video">下載影片</a>` : ""}
        ${f.audio ? `<a class="btn" href="/api/jobs/${encodeURIComponent(id)}/download/audio">下載原始錄音</a>` : ""}
        <button class="btn" id="openDir">開啟資料夾</button>
        <button class="btn" id="delJob">刪除</button>
        <a class="back" href="#history"><span aria-hidden="true">←</span> 回任務紀錄</a></div>`;
      if (j.bg && j.bg.kind !== "tidy") html += bgHtml(j.bg);
      const sp = j.speakers;
      if (sp) {
        html += `<div class="spk-panel"><h3>說話者</h3><div class="spk-list">${sp.names.map((n, i) =>
          `<label class="spk-item"><span class="spk-dot s${i % 6}" aria-hidden="true"></span><span class="sr">說話者 ${i + 1} 名稱</span>
            <input type="text" maxlength="30" data-spk="${i}" value="${esc(n)}"></label>`).join("")}</div>
          <div class="spk-acts"><button class="btn" id="spkSave">儲存名稱</button>
          <span class="note">改名後 SRT 與純文字會一起更新。分錯人時可重新分辨：</span>
          <select id="spkN" aria-label="說話人數">${SPK}</select><button class="btn small" id="spkRedo">重新分辨</button></div></div>`;
      } else {
        html += `<div class="spk-panel"><h3>說話者</h3><div class="spk-acts">
          <select id="spkN" aria-label="說話人數">${SPK}</select>
          <button class="btn" id="spkRedo">分辨說話者</button>
          <span class="note">只分析聲音、不重新轉文字。${info?.diarization?.installed ? "" : "第一次使用會自動安裝元件（約 1～2 GB，需要幾分鐘）。"}</span></div></div>`;
      }
      if (j.diar_error) html += `<p class="state error">分辨說話者失敗：${esc(j.diar_error)}</p>`;
      html += glossPanel(j);
      html += tidyPanel(j);
      html += `</div><div class="jv-tr"><div class="tr-head"><h3>逐字稿</h3>
        <label class="check"><input type="checkbox" id="follow" checked> 跟著播放捲動</label>
        <span class="note">點任一句可跳到該處播放</span></div>`;
      const segsArr = Array.isArray(j.segments) ? j.segments : [];
      const editedSet = new Set(j.review?.edited || []);
      const who = i => sp && sp.seg[i] >= 0 ? sp.seg[i] : -1;
      html += `<div class="transcript lang-${lang}" id="tr">${segsArr.map((x, i) => {
        const w = who(i), tag = w >= 0 && (i === 0 || who(i - 1) !== w) ? `<b class="spk s${w % 6}">${esc(sp.names[w])}</b>` : "";
        const zh = tl ? `<span class="t-zh">${esc(tl[i] || "")}</span>` : "";
        const ed = editedSet.has(i) ? " edited" : "";
        return `<p data-i="${i}" tabindex="0" class="${w >= 0 ? `has-spk b${w % 6}` : ""}${ed}"><time>${dur(x.start)}</time><span class="t-txt">${tag}<span class="t-orig">${esc(x.text)}</span>${zh}</span></p>`;
      }).join("")}</div></div>`;
    } else {
      html += `<div class="actions">${j.state === "running" || j.state === "queued" ? "" : `<button class="btn" id="delJob">刪除</button>`}
        <a class="back" href="#history"><span aria-hidden="true">←</span> 回任務紀錄</a></div></div>`;
    }
    html += `</div>`;  // jobview
    if (j.log && j.log.length) {
      html += `<details ${j.state === "running" || j.state === "error" ? "open" : ""}><summary class="note">處理紀錄</summary><pre class="log">${esc(j.log.join("\n"))}</pre></details>`;
    }
    $("#jobDetail").innerHTML = html;
    if (done && j.media) learnSide(view === "learn");
    if (done && j.media) {
      const sg = Array.isArray(j.segments) ? j.segments : [], sp = j.speakers;
      // 播放器字幕依「原文／中文／雙語」組合；切換時直接改內容，不重新載入影片
      const capText = i => {
        const pre = sp && sp.seg[i] >= 0 ? `${sp.names[sp.seg[i]]}：` : "", o = sg[i].text, z = tl?.[i] || "";
        return lang === "zh" && z ? pre + z : lang === "bi" && z ? `${pre}${o}\n${z}` : pre + o;
      };
      // 影片上的字幕：原文與中文譯文分開上色（顏色在播放器的「Aa」設定）
      const capHtml = i => {
        const pre = sp && sp.seg[i] >= 0 ? `${esc(sp.names[sp.seg[i]])}：` : "", o = esc(sg[i].text), z = esc(tl?.[i] || "");
        if (lang === "zh" && z) return `<span class="c-z">${pre}${z}</span>`;
        if (lang === "bi" && z) return `<span class="c-o">${pre}${o}</span><span class="c-z">${z}</span>`;
        return `<span class="c-o">${pre}${o}</span>`;
      };
      const capSegs = sg.map((x, i) => ({ ...x, text: capText(i), html: capHtml(i) }));
      bindPlayer(id, capSegs);
      const onEdit = (i, t, mark = true) => {  // mark＝false：詞庫自動更正（不算校對過）
        sg[i].text = t; capSegs[i].text = capText(i); capSegs[i].html = capHtml(i);
        const p = $(`#tr p[data-i="${i}"]`);
        if (p) { const o = p.querySelector(".t-orig"); if (o && o.textContent !== t) o.textContent = t; if (mark) p.classList.add("edited"); }
      };
      bindGloss(id, j, onEdit);
      bindTidy(id, j);
      bindBg(id, j);
      bindProof(id, sg, onEdit, j.glossary?.flagged || []);
      const ls = $("#langSel");
      if (ls) {
        ls.value = lang;
        ls.addEventListener("change", () => {
          lang = ls.value; store.set("lang", lang);
          capSegs.forEach((x, i) => { x.text = capText(i); x.html = capHtml(i); });
          $("#tr").className = `transcript lang-${lang}`;
        });
      }
      document.querySelectorAll("[data-view]").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
      $("#trBtn")?.addEventListener("click", async () => {
        try { await api(`/api/jobs/${encodeURIComponent(id)}/translate`, { method: "POST", body: new FormData() }); toast("已加入佇列：翻譯成中文"); store.set("lang", "bi"); loadJob(id); }
        catch (e) { toast(e.message); }
      });
      if (autoplayId === id) {  // 從清單點進來：直接開始播放
        autoplayId = "";
        $("#pl")?.play().catch(() => { });
      }
    }
    $("#spkSave")?.addEventListener("click", async () => {
      const names = [...document.querySelectorAll("[data-spk]")].map(x => x.value.trim());
      const fd = new FormData(); fd.append("names", JSON.stringify(names));
      try { await api(`/api/jobs/${encodeURIComponent(id)}/speakers`, { method: "POST", body: fd }); toast("已更新說話者名稱，SRT 與純文字已同步更新"); loadJob(id); }
      catch (e) { toast(e.message); }
    });
    $("#spkRedo")?.addEventListener("click", async () => {
      if (j.speakers && !(await confirmBox("重新分辨說話者", "會重新分析聲音並覆蓋目前的說話者結果（已改的名稱會依順序保留）。確定嗎？", "重新分辨"))) return;
      const fd = new FormData(); fd.append("speakers", $("#spkN").value);
      try { await api(`/api/jobs/${encodeURIComponent(id)}/diarize`, { method: "POST", body: fd }); toast("已加入佇列：分辨說話者"); loadJob(id); }
      catch (e) { toast(e.message); }
    });
    $("#cancelJob")?.addEventListener("click", async () => { if (await cancelJob(id, j.name)) loadJob(id, true); });
    $("#delJob")?.addEventListener("click", async () => { if (await deleteJob(id, j.name)) location.hash = "#history"; });
    const ob = $("#openDir");
    if (ob) ob.addEventListener("click", async () => { try { const r = await api(`/api/jobs/${encodeURIComponent(id)}/open`, { method: "POST" }); toast(`已開啟：${r.path}`); } catch (e) { toast(e.message); } });
  };

  /* ---------- 本機效能 ---------- */
  const estimate = (res, rtf, hours, fmt) => {
    const r = res.recommended;
    const e = res.engines.find(x => x.ok && x.model === r.model && x.device === r.device);
    const f = res.formats.find(x => x.ok && x.format === fmt);
    if (!e || !f) return null;
    return ((f.mp3_sec_per_hour + f.decode_sec_per_hour) * hours + e.load_sec + rtf * 3600 * hours) / 60;
  };
  const rating = rtf => rtf < .1 ? "非常快" : rtf < .3 ? "快" : rtf < .7 ? "普通" : rtf < 1 ? "偏慢" : "比即時還慢";
  const ratingTip = rtf => rtf < .3 ? "長時間會議錄音也能很快拿到逐字稿。" : rtf < .7 ? "處理時可以先做別的事。"
    : rtf < 1 ? "長檔建議午休或下班時處理。" : "長檔建議改用其他電腦處理。";

  let lastBench = "";
  const loadBench = async quiet => {
    let b;
    try { b = await api("/api/benchmark"); } catch (e) { $("#benchBody").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    const key = b.state + (b.result?.tested_at || "") + b.log.length;
    if (quiet && key === lastBench) return;
    lastBench = key;
    const running = b.state === "running" || b.state === "queued";
    const res = b.result;
    let html = "";

    if (res && res.recommended && res.recommended.model) {
      const r = res.recommended, lt = res.long_test;
      const sameCfg = lt && lt.model === r.model && lt.device === r.device && lt.rtf_long;
      const rtf = sameCfg ? lt.rtf_long : r.rtf;
      const perHour = estimate(res, rtf, 1, "m4a") ?? rtf * 60;
      html += `<div class="verdict">
        <h2>這台電腦：1 小時錄音約 ${Math.round(perHour)} 分鐘完成</h2>
        <p>效能評等 <span class="rating">${rating(rtf)}</span>，${ratingTip(rtf)}</p>
        <p>建議配置：<b>${MODEL[r.model]}</b> × <b>${DEV[r.device] || r.device}</b>。${esc(r.reason)}。轉逐字稿時會自動採用。</p>
        ${r.draft ? `<p>趕時間：改選 large-v3-turbo，1 小時約 ${Math.round(r.draft.min_per_hour)} 分鐘，準確度略低。</p>` : ""}
      </div>`;

      // 時間比例條
      html += `<h2 class="sec">預估處理時間</h2><div class="ratio">`;
      [[0.5, "30 分鐘錄音"], [1, "1 小時錄音"], [2, "2 小時錄音"]].forEach(([h, lab]) => {
        const m = estimate(res, rtf, h, "m4a");
        if (m == null) return;
        html += `<div class="ratio-row"><span class="lab">${lab}</span><div class="track">
          <span class="rec" style="width:${h / 2 * 100}%"></span>
          <span class="proc" style="width:${Math.min(m / 120 * 100, 100)}%"></span>
          <span class="txt">錄音 ${h * 60} 分鐘 → 處理約 <b>${Math.max(1, Math.round(m))} 分鐘</b></span></div></div>`;
      });
      html += `</div><p class="note">灰色是錄音長度，橘色是處理時間（m4a 錄音，含轉檔、載入模型與辨識）。${sameCfg ? "依長檔實測速度換算。" : "依 3 分鐘樣本換算，實際通常更快。"}筆電請插電並設為最佳效能。</p>`;

      // 配置比較
      const ok = res.engines.filter(e => e.ok);
      const maxMin = Math.max(...ok.map(e => e.min_per_hour), 1);
      const g = ok.find(e => e.model === r.model && e.device === "GPU"), c = ok.find(e => e.model === r.model && e.device === "CPU");
      html += `<h2 class="sec">各種配置比較</h2>`;
      if (g && c) html += g.rtf < c.rtf
        ? `<p class="explain">用顯示卡比用 CPU 快約 <b>${(c.rtf / g.rtf).toFixed(1)} 倍</b>。</p>`
        : `<p class="explain">這台的顯示卡反而比 CPU <b>慢</b>（約多花 ${(g.rtf / c.rtf).toFixed(1)} 倍時間），所以建議用 CPU。這裡的「顯示卡」指 OpenVINO 能用的 Intel 顯示卡（內顯）。</p>`;
      html += `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>配置</th><th>1 小時錄音約需</th><th>記憶體</th><th>辨識出的開頭</th></tr></thead><tbody>`;
      res.engines.forEach(e => {
        if (!e.ok) { html += `<tr><td>${MODEL[e.model]} × ${DEV[e.device]}</td><td colspan="3" class="state error">無法使用：${esc(e.error)}</td></tr>`; return; }
        const best = e.model === r.model && e.device === r.device;
        html += `<tr class="${best ? "best" : ""}"><td>${MODEL[e.model]} × ${DEV[e.device]}${best ? "（建議）" : ""}</td>
          <td class="num"><span class="mini" style="width:${Math.max(4, e.min_per_hour / maxMin * 90)}px"></span>${Math.round(e.min_per_hour)} 分鐘</td>
          <td class="num">${e.peak_mb ? (e.peak_mb / 1024).toFixed(1) + " GB" : "-"}</td><td>${esc(e.preview)}</td></tr>`;
      });
      html += `</tbody></table></div><p class="note">以 3 分鐘樣本量測。Breeze-ASR-25 對臺灣華語、中英夾雜較準；large-v3-turbo 較快但準確度略低。</p>`;

      // 長檔
      html += `<h2 class="sec">長時間錄音</h2>`;
      if (lt) {
        const ch = (lt.modes || []).find(m => m.chunk_min > 0 && m.ok);
        const diff = ch ? ch.chars_diff_pct : null;
        html += `<p class="explain"><b>${esc(lt.note)}</b></p>`;
        if (diff != null) html += `<p class="explain">${Math.abs(diff) <= 2 ? `切段與不切段的字數只差 ${diff > 0 ? "+" : ""}${diff}%，切段不會漏字或重複。` : `切段與不切段的字數差了 ${diff}%，切點附近可能有漏字或重複，建議回報。`}</p>`;
        html += `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>方式</th><th>耗時</th><th>記憶體</th><th>字數</th><th>重複句</th></tr></thead><tbody>`;
        (lt.modes || []).forEach(m => {
          html += m.ok ? `<tr><td>${esc(m.label)}</td><td class="num">${minsec(m.sec)}</td><td class="num">${m.peak_mb ? (m.peak_mb / 1024).toFixed(1) + " GB" : "-"}</td><td>${m.chars}${m.chunk_min ? `（${m.chars_diff_pct > 0 ? "+" : ""}${m.chars_diff_pct}%）` : ""}</td><td>${m.repeats}</td></tr>`
            : `<tr><td>${esc(m.label)}</td><td colspan="4" class="state error">${esc(m.error)}</td></tr>`;
        });
        html += `</tbody></table></div><p class="note">測試內容：${esc(lt.source)} 前 ${dur(lt.test_sec)}。</p>`;
      } else {
        html += `<p class="explain">還沒做長檔測試，暫時採用「超過 15 分鐘每 10 分鐘切段」。勾選下方「包含長檔測試」再實測一次即可。</p>`;
      }

      // 格式
      const fm = res.formats.filter(f => f.ok);
      html += `<h2 class="sec">檔案格式</h2><p class="explain">${fm.map(f => f.format).join("、")} 都能處理，前處理每小時錄音只要 ${Math.round(Math.min(...fm.map(f => f.mp3_sec_per_hour + f.decode_sec_per_hour)))}～${Math.round(Math.max(...fm.map(f => f.mp3_sec_per_hour + f.decode_sec_per_hour)))} 秒，選哪種格式對速度幾乎沒有影響。有原始錄音檔時，優先用錄音檔而不是影片（檔案小、上傳快）。</p>`;
      html += `<p class="note">實測時間：${esc(res.tested_at)}｜程式版本 ${esc(res.app_version)}｜${esc(info?.cpu || "")}｜${esc((info?.gpus || []).join("、"))}｜記憶體 ${esc(info?.ram_gb ?? "")} GB</p>`;
    } else if (!running) {
      html += `<div class="verdict"><h2>這台電腦還沒做效能實測</h2>
        <p>實測會找出這台電腦最快的辨識方式，並預估轉一小時錄音要多久。之後轉逐字稿會自動採用結果。</p></div>`;
    }

    html += `<div class="bench-start">
      <button class="btn primary" id="benchGo" ${running ? "disabled" : ""}>${running ? "實測進行中…" : res ? "重新實測" : "開始實測"}</button>
      <label class="check"><input type="checkbox" id="benchLong" ${store.get("benchLong", "1") === "1" ? "checked" : ""} ${running ? "disabled" : ""}> 包含長檔測試（多約 6–10 分鐘）</label>
      <span class="note">快速健檢約 5–10 分鐘；實測時請勿同時轉檔。</span></div>`;
    if (running || b.state === "error" || (b.log.length && b.state === "done")) {
      if (b.error) html += `<p class="state error">${esc(b.error)}</p>`;
      html += `<details ${running ? "open" : ""}><summary class="note">實測紀錄</summary><pre class="log" id="benchLog">${esc(b.log.join("\n"))}</pre></details>`;
    }
    $("#benchBody").innerHTML = html;
    const lg = $("#benchLog"); if (lg) lg.scrollTop = lg.scrollHeight;
    $("#benchLong").addEventListener("change", e => store.set("benchLong", e.target.checked ? "1" : "0"));
    $("#benchGo").addEventListener("click", async () => {
      const fd = new FormData(); fd.append("long", $("#benchLong").checked ? "true" : "false");
      try { await api("/api/benchmark", { method: "POST", body: fd }); toast("已開始效能實測"); lastBench = ""; loadBench(); }
      catch (e) { toast(e.message); }
    });
    if (b.state === "done" && quiet && !running) loadInfo();  // 實測完成後更新建議配置
  };

  /* ---------- 參數設定 ---------- */
  /* ---------- 這台電腦：別名、功能、模型配置方案（0.14.0）---------- */
  let machPoll = null;
  const MST = { ready: ["✓ 就緒", "done"], missing: ["✗ 缺少", "error"], incompatible: ["⚠ 版本不符", "error"], not_needed: ["沒選用", ""] };
  const machineHtml = (m, busy) => {
    if (!m) return "";
    if (m.error) return `<div class="verdict"><h2>這台電腦</h2><p class="state error">模型配置方案無法計算：${esc(m.error)}</p></div>`;
    const FI = m.feature_info || {}, P = m.prep || {};
    const rows = Object.entries(m.plan || {}).map(([f, p]) => {
      const [t, c] = MST[p.state] || ["?", ""], it = (m.models || {})[p.model] || {};
      const extra = (p.also || []).map(a => (m.models || {})[a]).filter(Boolean)
        .map(a => `<br><span class="note">另備：${esc(a.label)}（${(MST[a.state] || ["?"])[0]}）</span>`).join("");
      return `<tr><td>${esc(FI[f]?.label || f)}</td><td>${esc(p.label)}${extra}</td><td>${esc(p.device_label)}${p.why ? `<br><span class="note">${esc(p.why)}</span>` : ""}</td>
        <td class="state ${c}">${t}${p.state === "missing" && it.how ? `<br><span class="note">${esc(it.how)}</span>` : ""}${it.problem ? `<br><span class="note">${esc(it.problem)}</span>` : ""}</td></tr>`;
    }).join("");
    const auto = (m.missing || []).filter(id => ["copy", "download"].includes(m.models[id]?.source));
    const nn = Object.values(m.models || {}).filter(x => x.state === "not_needed");
    const running = P.state === "running";
    return `<div class="verdict"><h2>這台電腦</h2>
      <p class="path">${esc(m.display)}</p>
      <p class="note">等級 ${esc(m.tier)}｜記憶體 ${m.ram_gb} GB｜模型位置 ${esc(m.models_root)}${m.source ? `｜共用來源 ${esc(m.source)}` : ""}</p></div>
    <form class="set-form" id="aliasForm">
      <label>電腦別名（讓人一看就知道是哪台，例：辦公室筆電、會議室桌機；空白＝只顯示電腦名稱）
        <input type="text" id="aliasIn" maxlength="30" value="${esc(m.alias || "")}" placeholder="${esc(m.computer)}">
      </label>
      <div class="set-btns"><button class="btn primary" type="submit">儲存別名</button></div>
    </form>
    <h2 class="sec">要用的功能</h2>
    ${m.features_set ? "" : `<p class="state error">還沒選擇要用的功能，目前依這台已有的模型判斷。請勾選後按「儲存功能」。</p>`}
    <div class="llm-set" id="featBox">${Object.entries(FI).map(([f, x]) => `<label class="chk"><input type="checkbox" name="feat" value="${f}"
        ${(m.features || []).includes(f) ? "checked" : ""} ${x.required ? "checked disabled" : ""}> ${esc(x.label)}<span class="note">　${esc(x.note)}</span></label>`).join("")}</div>
    <div class="set-btns"><button class="btn primary" type="button" id="featSave" ${busy ? "disabled" : ""}>儲存功能</button></div>
    <p class="note">只準備勾選功能需要的模型；取消勾選不會刪除已有的模型。</p>
    <h2 class="sec">模型配置方案</h2>
    <p class="note">依你選的功能、這台的硬體與效能實測，為每個功能挑出最適合的模型與裝置。</p>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>功能</th><th>使用模型</th><th>裝置</th><th>狀態</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${running ? `<div class="verdict"><p id="prepMsg">${esc(P.msg)}（${Math.round((P.progress || 0) * 100)}%）</p><div class="bar"><i id="prepBar" style="width:${Math.round((P.progress || 0) * 100)}%"></i></div></div>`
      : auto.length ? `<div class="set-btns"><button class="btn primary" type="button" id="prepGo">準備缺少的模型</button></div>
          <p class="note">${auto.map(id => `${esc(m.models[id].label)}（${m.models[id].source === "copy" ? "從共用來源複製" : "從 Hugging Face 下載"}${m.models[id].gb ? `，約 ${m.models[id].gb} GB` : ""}）`).join("、")}；在背景進行、不影響轉檔。</p>` : ""}
    ${P.state === "error" ? `<p class="state error">${Object.entries(P.errors || {}).map(([k, v]) => `${esc(m.models[k]?.label || k)}：${esc(v)}`).join("<br>")}</p>` : ""}
    ${nn.length ? `<p class="note">這台沒選用、但已經有的模型：${nn.map(x => esc(x.label)).join("、")}（保留不影響使用；需要空間時可自行刪除）。</p>` : ""}
    ${(m.unused || []).length ? `<p class="explain">模型位置裡本專案用不到的資料夾（程式不會刪除，確認沒有其他專案在用再自行刪除）：<br>${m.unused.map(u => `<code>${esc(u.path)}</code>（${u.gb} GB）${esc(u.note)}`).join("<br>")}</p>` : ""}`;
  };
  const bindMachine = m => {
    if (!m || m.error) return;
    const post = async (fd, msg) => {
      try { await api("/api/machine", { method: "POST", body: fd }); toast(msg); loadSettings(); loadInfo(); } catch (e) { toast(e.message); }
    };
    $("#aliasForm")?.addEventListener("submit", e => {
      e.preventDefault(); const fd = new FormData(); fd.append("alias", $("#aliasIn").value);
      post(fd, $("#aliasIn").value.trim() ? "已儲存電腦別名" : "已清除電腦別名");
    });
    $("#featSave")?.addEventListener("click", () => {
      const fd = new FormData();
      fd.append("features", [...document.querySelectorAll('input[name="feat"]:checked')].map(i => i.value).filter(v => v !== "transcribe").join(","));
      post(fd, "已儲存功能；缺少的模型會在背景準備");
    });
    $("#prepGo")?.addEventListener("click", async () => {
      try { await api("/api/machine/prepare", { method: "POST" }); toast("開始準備模型"); loadSettings(); } catch (e) { toast(e.message); }
    });
    clearInterval(machPoll);
    if (m.prep?.state === "running") machPoll = setInterval(async () => {
      if (!$("#prepMsg")) { clearInterval(machPoll); return; }
      try {
        const st = await api("/api/machine"), P = st.prep || {};
        if (P.state !== "running") { clearInterval(machPoll); loadSettings(); loadInfo(); return; }
        $("#prepMsg").textContent = `${P.msg}（${Math.round((P.progress || 0) * 100)}%）`;
        $("#prepBar").style.width = `${Math.round((P.progress || 0) * 100)}%`;
      } catch { }
    }, 2000);
  };
  const SRC = { settings: "在這裡設定", env: "環境變數 MEDIA_ROOT", default: "預設值", fallback: "這台電腦沒有 D 槽，暫時放在專案資料夾內，建議改到其他磁碟" };
  const loadSettings = async () => {
    let s;
    try { s = await api("/api/settings"); } catch (e) { $("#settingsBody").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    const free = s.free_gb == null ? "" : `｜可用空間 ${s.free_gb} GB`;
    let html = machineHtml(s.machine, s.busy) + `<div class="verdict">
      <h2>媒體檔位置</h2>
      <p class="path">${esc(s.media_root)}</p>
      <p class="note">來源：${esc(SRC[s.source] || s.source)}${free}｜目前有 ${s.tasks} 個任務</p>
    </div>
    <div class="tbl-wrap"><table class="tbl"><tbody>
      <tr><td>uploads</td><td>上傳的檔案：每個任務一個資料夾（原始檔、字幕、純文字、MP3、處理紀錄）</td></tr>
      <tr><td>benchmark</td><td>效能實測的暫存檔（實測結果仍存在專案的 env-reports）</td></tr>
      <tr><td>downloads</td><td>從網路下載的影音：每個任務一個資料夾（下載的錄音、字幕、純文字、來源網址）</td></tr>
    </tbody></table></div>
    <h2 class="sec">修改位置</h2>
    <form class="set-form" id="setForm">
      <label>新的位置（完整路徑）
        <input type="text" id="setPath" value="${esc(s.source === "settings" ? s.media_root : "")}" placeholder="${esc(s.env || s.default)}" ${s.busy ? "disabled" : ""}>
      </label>
      <div class="set-btns">
        <button class="btn primary" type="submit" ${s.busy ? "disabled" : ""}>儲存</button>
        <button class="btn" type="button" id="setReset" ${s.busy || s.source !== "settings" ? "disabled" : ""}>還原預設</button>
      </div>
    </form>
    <p class="note">${s.busy ? "還有任務或效能實測在進行，完成後才能修改。" : "儲存前會先確認這個位置可以寫入。修改後，新任務會存到新位置；舊位置的檔案不會被搬移或刪除，需要的話請自行搬移。空白＝使用預設值。"}</p>`;
    // ---- 本機 LLM（0.12.0）----
    const LM = s.llm;
    if (LM) {
      const dl = LM.download || {};
      html += `<h2 class="sec">本機 LLM</h2>
      <div class="verdict"><p>用途：校對時判讀要加入詞庫的詞；之後也用來把逐字稿整理成段落。完全在這台電腦執行、不上傳。</p>
        <p class="note">這台電腦記憶體 ${LM.ram_gb} GB，建議：<b>${esc(LM.models.find(m => m.key === LM.recommended)?.label || "")}</b></p></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>使用</th><th>模型</th><th>適合</th><th>狀態</th></tr></thead><tbody>
        ${LM.models.map(m => `<tr><td><input type="radio" name="llmModel" value="${m.key}" ${LM.model === m.key ? "checked" : ""} aria-label="${esc(m.label)}"></td>
          <td>${esc(m.label)}${m.key === LM.recommended ? ` <span class="tag">建議</span>` : ""}</td><td class="note">${esc(m.note)}</td>
          <td class="state ${m.ready ? "done" : ""}">${m.ready ? "✓ 已下載" : "未下載"}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="llm-set"><div class="spk-acts">
        ${LM.ready ? "" : `<button type="button" class="btn primary" id="llmDl" ${dl.state === "running" ? "disabled" : ""}>下載選取的模型</button>`}
        <span class="note" id="llmDlMsg">${dl.state === "running" ? `${esc(dl.msg)}（${Math.round((dl.progress || 0) * 100)}%）` : dl.state === "error" ? `⚠ ${esc(dl.msg)}：${esc(dl.error)}` : dl.state === "done" ? "✓ 下載完成" : "從 Hugging Face 下載（Intel 官方轉好的 OpenVINO 版，不需帳號），在背景進行、不影響轉檔"}</span></div>
        <label class="chk"><input type="checkbox" id="llmJudge" ${LM.judge ? "checked" : ""}> 校對時用本機 LLM 判讀詞庫候選（沒勾或沒下載時用規則判斷）</label>
        ${LM.ready ? `<div class="spk-acts"><button type="button" class="btn" id="llmTest">測試本機 LLM</button><span class="note" id="llmTestOut">用內建的範例詞語試判讀一次（第一次要載入模型，約需 10～60 秒）</span></div>` : ""}</div>`;
    }
    // ---- 模型位置 ----
    const M = s.models;
    if (M) {
      const MSRC = { settings: "在這裡設定", env: "環境變數 MODELS_ROOT（setup.cmd 設定）", default: "預設值" };
      html += `<h2 class="sec">模型位置</h2>
      <div class="verdict"><p class="path">${esc(M.root)}</p>
        <p class="note">來源：${esc(MSRC[M.source] || M.source)}${M.free_gb == null ? "" : `｜可用空間 ${M.free_gb} GB`}</p>
        ${M.pending_restart ? `<p class="state error">已修改模型位置，請關閉 start.cmd 視窗再重新開啟才會生效。</p>` : ""}</div>
      <p class="note">各模型是否就緒，請看上方「這台電腦 → 模型配置方案」。</p>
      <form class="set-form" id="modelsForm">
        <label>新的模型位置（完整路徑，需含 asr\\ov 等子資料夾）
          <input type="text" id="modelsPath" value="${esc(M.source === "settings" ? M.root : "")}" placeholder="${esc(M.env || M.default)}" ${s.busy ? "disabled" : ""}>
        </label>
        <div class="set-btns">
          <button class="btn primary" type="submit" ${s.busy ? "disabled" : ""}>儲存</button>
          <button class="btn" type="button" id="modelsReset" ${s.busy || M.source !== "settings" ? "disabled" : ""}>還原預設</button>
        </div>
      </form>
      <p class="note">模型一律集中放在這個資料夾（只放模型）。儲存前會檢查新位置是否有辨識模型；修改後需重新開啟 start.cmd 才生效。setup.cmd 也會採用這裡的設定。</p>`;
    }
    // ---- 登入同步（Chrome 擴充功能） ----
    const L = s.login;
    if (L) {
      html += `<h2 class="sec">登入同步（需要登入的影片）</h2>
      <div class="verdict">
        ${L.has ? `<p><span class="state done">✓ 已有登入資料</span>　上次同步：${esc(L.updated || "—")}｜${L.count ?? "—"} 筆｜${esc((L.domains || []).join("、"))}</p>`
          : `<p><span class="state error">尚未同步</span>　公開影片不受影響；需要登入的影片（會員限定、私人社團、Instagram 等）要先安裝擴充功能並同步。</p>`}
        <p class="note">${L.paired ? `已配對擴充功能：${esc(L.paired)}` : "尚未配對（第一次同步時自動配對）"}</p>
      </div>
      <ol class="explain install-steps">
        <li>Chrome 網址列輸入 <code>chrome://extensions</code>，打開右上角「開發人員模式」。</li>
        <li>按「載入未封裝項目」，選擇資料夾 <code>${esc(L.ext_dir)}</code>。</li>
        <li>按 Chrome 右上角拼圖圖示，把「逐字稿登入同步」釘選出來；點它、按「立即同步」。之後每 30 分鐘自動同步。</li>
        <li>在影片頁面或連結上按右鍵，選「轉逐字稿」可直接加入（不用複製網址）。</li>
      </ol>
      <div class="set-btns">
        <button class="btn" type="button" id="loginUnpair" ${L.paired ? "" : "disabled"}>解除配對</button>
        <button class="btn danger" type="button" id="loginClear" ${L.has ? "" : "disabled"}>刪除登入資料</button>
      </div>
      <p class="note">登入資料等同你的帳號憑證：只存在這台電腦的專案 data 資料夾，不會上傳、不寫入紀錄、打包給別人時會排除。其他電腦請各自在自己的 Chrome 安裝擴充功能，不要複製別人的登入資料。</p>`;
    }
    const notes = [];
    if (s.legacy.tasks) notes.push(`舊版的任務資料夾（${esc(s.legacy.path)}）還有 ${s.legacy.tasks} 個任務，不會顯示在任務紀錄。確認已複製到新位置後，可自行手動刪除。`);
    if (s.previous) notes.push(`上一個位置（${esc(s.previous.path)}）還有 ${s.previous.tasks} 個任務，不會顯示在任務紀錄。`);
    if (notes.length) html += `<h2 class="sec">提醒</h2>` + notes.map(n => `<p class="explain">${n}</p>`).join("");
    $("#settingsBody").innerHTML = html;
    bindMachine(s.machine);
    $("#loginUnpair")?.addEventListener("click", async () => {
      if (!(await confirmBox("解除配對", "解除後，下一個送資料過來的擴充功能會自動配對。換了電腦或重新安裝擴充功能時才需要。", "解除配對"))) return;
      try { await api("/api/login/unpair", { method: "POST" }); toast("已解除配對"); loadSettings(); } catch (e) { toast(e.message); }
    });
    $("#loginClear")?.addEventListener("click", async () => {
      if (!(await confirmBox("刪除登入資料", "會刪除這台電腦同步過來的登入資料（cookies.txt）。之後需要登入的影片要重新同步才能下載。確定嗎？", "刪除"))) return;
      try { await api("/api/login/clear", { method: "POST" }); toast("已刪除登入資料"); loadSettings(); } catch (e) { toast(e.message); }
    });
    const llmSet = async (k, v) => { const fd = new FormData(); fd.append(k, v); try { info.llm = await api("/api/llm/settings", { method: "POST", body: fd }); } catch (e) { toast(e.message); } };
    document.querySelectorAll('input[name="llmModel"]').forEach(r => r.addEventListener("change", async () => { await llmSet("model", r.value); loadSettings(); }));
    $("#llmJudge")?.addEventListener("change", e => llmSet("judge", e.target.checked ? "1" : "0"));
    $("#llmDl")?.addEventListener("click", async () => {
      const fd = new FormData(); fd.append("key", document.querySelector('input[name="llmModel"]:checked')?.value || "");
      try { await api("/api/llm/download", { method: "POST", body: fd }); toast("開始下載本機 LLM"); loadSettings(); } catch (e) { toast(e.message); }
    });
    $("#llmTest")?.addEventListener("click", async () => {
      const o = $("#llmTestOut"); o.textContent = "測試中…（載入模型＋判讀）"; $("#llmTest").disabled = true;
      try {
        const r = await api("/api/llm/test", { method: "POST" });
        o.innerHTML = `✓ ${esc(r.device)}｜載入 ${r.load_s} 秒、判讀 ${r.judge_s} 秒<br>` + r.items.map(x =>
          `${esc(x.wrong)} → ${esc(x.right)}：${x.res ? `${x.res.add ? "<b>加入</b>" : "不加入"}${x.res.add && x.res.term !== x.right ? `（完整詞：${esc(x.res.term)}）` : ""}${x.res.flag ? "，只提醒" : ""}｜${esc(x.res.why)}` : "（沒判讀到）"}`).join("<br>");
      } catch (e) { o.textContent = "⚠ " + e.message; }
      $("#llmTest").disabled = false;
    });
    clearInterval(llmPoll);
    if (s.llm?.download?.state === "running") llmPoll = setInterval(async () => {
      if (!$("#llmDlMsg")) { clearInterval(llmPoll); return; }
      try {
        const st = await api("/api/llm"); info.llm = st; const d = st.download;
        if (d.state !== "running") { clearInterval(llmPoll); loadSettings(); return; }
        $("#llmDlMsg").textContent = `${d.msg}（${Math.round((d.progress || 0) * 100)}%）`;
      } catch { }
    }, 2000);
    const saveModels = async v => {
      const fd = new FormData(); fd.append("path", v);
      try { const r = await api("/api/settings/models", { method: "POST", body: fd }); toast(r.note); loadSettings(); }
      catch (e) { toast(e.message); }
    };
    $("#modelsForm")?.addEventListener("submit", e => { e.preventDefault(); saveModels($("#modelsPath").value); });
    $("#modelsReset")?.addEventListener("click", async () => {
      if (await confirmBox("還原模型位置", `改回 ${M.env || M.default}？需重新開啟 start.cmd 才生效。`, "還原")) saveModels("");
    });
    const save = async v => {
      const fd = new FormData(); fd.append("media_root", v);
      try {
        const r = await api("/api/settings", { method: "POST", body: fd });
        toast(r.old_tasks ? `已改為 ${r.media_root}；舊位置還有 ${r.old_tasks} 個任務，不會自動搬移` : `已改為 ${r.media_root}`);
        loadSettings(); loadInfo();
      } catch (e) { toast(e.message); }
    };
    $("#setForm").addEventListener("submit", e => { e.preventDefault(); save($("#setPath").value); });
    $("#setReset").addEventListener("click", async () => {
      if (await confirmBox("還原預設位置", `改回 ${s.env || s.default}？目前位置的檔案會保留，不會被刪除。`, "還原")) save("");
    });
  };

  loadInfo().catch(e => toast(`無法連線到程式：${e.message}`)).finally(route);
})();
