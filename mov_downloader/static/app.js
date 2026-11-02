/* 影片下載器前端 */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const store = {
    get(k, d) { try { const v = localStorage.getItem('movdl.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem('movdl.' + k, JSON.stringify(v)); } catch {} },
  };
  const api = async (path, opts = {}) => {
    const r = await fetch(path, {
      headers: { 'Content-Type': 'application/json' }, ...opts,
      body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || (r.status === 404 && path.startsWith('/api/')
      ? '背景程式還是舊版，沒有這個功能。請關閉黑色視窗後重新執行 start.bat，再重新整理此頁。' : `HTTP ${r.status}`));
    return data;
  };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let toastTimer;
  let staleServer = '';
  const toast = (msg, ms = 2800) => { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), ms); };

  /* ---------------------------------------------------- 版面：選單、字型、主題 */
  const app = $('#app');
  const titles = { queue: '下載佇列', library: '影片庫', subs: '訂閱清單', settings: '設定', errors: '錯誤紀錄', help: '使用說明' };
  const isMobile = () => matchMedia('(max-width: 860px)').matches;
  if (store.get('collapsed', false)) app.classList.add('collapsed');
  $('#collapseBtn').onclick = () => { app.classList.toggle('collapsed'); store.set('collapsed', app.classList.contains('collapsed')); };
  $('#menuBtn').onclick = () => app.classList.add('drawer');
  $('#scrim').onclick = () => app.classList.remove('drawer');

  function show(view) {
    $$('.nav-item[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
    $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
    $('#viewTitle').textContent = titles[view];
    app.classList.remove('drawer');
    store.set('view', view);
    if (view === 'library') loadLibrary();
    if (view === 'settings') loadSettings();
    if (view === 'subs') loadSubs();
    if (view === 'errors') loadErrors();
    if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  }
  $$('.nav-item[data-view]').forEach(b => b.onclick = () => show(b.dataset.view));

  let scale = store.get('scale', 1);
  const applyScale = () => {
    document.documentElement.style.setProperty('--scale', scale);
    $('#fontPct').textContent = Math.round(scale * 100) + '%';
    store.set('scale', scale);
  };
  $('#fontUp').onclick = () => { scale = Math.min(1.6, +(scale + 0.1).toFixed(2)); applyScale(); };
  $('#fontDown').onclick = () => { scale = Math.max(0.8, +(scale - 0.1).toFixed(2)); applyScale(); };
  $('#fontReset').onclick = () => { scale = 1; applyScale(); };
  applyScale();

  const applyTheme = t => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; };
  applyTheme(store.get('theme', null));
  $('#themeBtn').onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    applyTheme(next); store.set('theme', next);
  };

  /* ---------------------------------------------------- 佇列 */
  const STATUS = { queued: '排隊中', running: '下載中', done: '完成', error: '失敗', canceled: '已取消' };
  const ICON = {
    subscribe: '<svg viewBox="0 0 24 24"><path d="M5 5a14 14 0 0 1 14 14M5 11a8 8 0 0 1 8 8"/><circle cx="6" cy="18" r="1.5"/></svg>',
    force: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5"/><path d="M12 8v5l3 2"/></svg>',
    cancel_children: '<svg viewBox="0 0 24 24"><path d="M4 6h10M4 12h10M4 18h6M16 14l5 5M21 14l-5 5"/></svg>',
    cancel: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    retry: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5"/></svg>',
    up: '<svg viewBox="0 0 24 24"><path d="m6 15 6-6 6 6"/></svg>',
    down: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
    remove: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  };

  async function addUrls() {
    const text = $('#urlInput').value.trim();
    if (!text) return toast('請先貼上網址');
    try {
      const r = await api('/api/jobs', { method: 'POST', body: { text } });
      $('#urlInput').value = '';
      const msg = [`已加入 ${r.added.length} 個下載工作`];
      if (r.duplicates?.length) msg.push(`${r.duplicates.length} 個已在佇列中，略過`);
      if (r.existing?.length) msg.push(`${r.existing.length} 個之前下載過，將自動略過`);
      toast(msg.join('；'), 5000);
      refreshJobs();
    } catch (e) { toast(e.message); }
  }
  $('#addBtn').onclick = addUrls;
  api('/api/settings').then(st => $('#plQuick').checked = !!st.playlist).catch(() => {});
  $('#plQuick').onchange = e => api('/api/settings', { method: 'POST', body: { playlist: e.target.checked } })
    .then(() => toast(e.target.checked ? '含播放清單的影片網址將下載整個清單' : '含播放清單的影片網址只下載該影片'));
  $('#urlInput').addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) addUrls(); });
  $('#pasteBtn').onclick = async () => {
    try {
      const t = await navigator.clipboard.readText();
      const box = $('#urlInput');
      box.value = (box.value.trim() ? box.value.trim() + '\n' : '') + t.trim();
      box.focus();
    } catch { toast('無法讀取剪貼簿，請直接按 Ctrl+V 貼上'); }
  };
  let paused = false;
  $('#pauseBtn').onclick = async () => { await api('/api/queue/' + (paused ? 'resume' : 'pause'), { method: 'POST' }); refreshJobs(); };
  $('#clearBtn').onclick = async () => { await api('/api/queue/clear', { method: 'POST' }); refreshJobs(); };
  $('#openDirBtn').onclick = () => api('/api/open-folder', { method: 'POST' }).then(r => !r.ok && toast(r.error || '無法開啟資料夾'));

  function jobHTML(j) {
    const pct = j.status === 'done' ? 100 : (j.progress || 0);
    const indet = j.status === 'running' && !j.progress;
    const title = j.title || j.url;
    const meta = [];
    const post = j.status === 'running' && /翻譯|處理|合併|轉換|辨識|抽出|載入|安裝/.test(j.phase || '');
    meta.push(`<span class="status">${esc(j.skipped ? '已存在' : post ? '處理中' : (STATUS[j.status] || j.status))}</span>`);
    if (j.skipped) meta.push(`<span>${esc(j.phase)}</span>`);
    if (j.status === 'running' || j.status === 'queued') meta.push(`<span>${esc(j.phase)}</span>`);
    if (j.status === 'running' && j.progress) meta.push(`<span>${pct.toFixed(1)}%</span>`);
    if (j.size) meta.push(`<span>${esc(j.size)}</span>`);
    if (j.speed) meta.push(`<span>${esc(j.speed)}</span>`);
    if (j.eta) meta.push(`<span>剩餘 ${esc(j.eta)}</span>`);
    if (j.sub_lang) meta.push(`<span class="chip">字幕來源 ${esc(j.sub_lang)}</span>`);
    (j.subs || []).forEach(s => meta.push(`<span class="chip">${esc(s.split('.').slice(-2, -1)[0])}.srt</span>`));
    const acts = [];
    if (j.is_playlist && jobsCache.some(x => x.parent === j.id && x.status === 'queued')) acts.push(['cancel_children', '取消此清單中尚未開始的影片']);
    if (j.is_playlist && !j.is_scan) acts.push(['subscribe', '加入訂閱清單，之後一鍵掃描新影片']);
    if (j.status === 'queued') acts.push(['up', '上移'], ['down', '下移'], ['cancel', '取消']);
    if (j.status === 'running') acts.push(['cancel', '取消']);
    if (j.status === 'done' && j.file) acts.push(['play', '播放']);
    if (j.skipped) acts.push(['force', '強制重新下載（覆蓋既有檔案）']);
    else if (['error', 'canceled', 'done'].includes(j.status)) acts.push(['retry', '重新下載']);
    acts.push(['remove', '從清單移除']);
    if (j.playlist) meta.unshift(`<span class="chip pl" title="${esc(j.playlist.title)}">📃 ${esc(j.playlist.title.slice(0, 24))} ${j.playlist.index}/${j.playlist.count}</span>`);
    if (j.is_playlist) {
      const kids = jobsCache.filter(x => x.parent === j.id);
      const c = st => kids.filter(x => x.status === st).length;
      const sk = kids.filter(x => x.skipped).length;
      if (kids.length) meta.push(`<span class="chip pl">完成 ${c('done')}／共 ${kids.length}${sk ? `（其中 ${sk} 部已存在略過）` : ''}${c('error') ? `，失敗 ${c('error')}` : ''}</span>`);
    }
    return `<div class="job st-${j.status}${indet ? ' indet' : ''}${j.parent ? ' is-child' : ''}${j.skipped ? ' is-skipped' : ''}${j.starred ? ' is-starred' : ''}${j.is_playlist ? ' is-playlist' : ''}" data-id="${j.id}">
      <div class="thumb" style="${j.thumbnail ? `background-image:url('${esc(j.thumbnail)}')` : ''}"><span class="site">${esc(j.site)}</span></div>
      <div class="info">
        <div class="title-row"><button class="star-btn${j.starred ? ' on' : ''}" data-act="star" aria-pressed="${!!j.starred}" title="${j.starred ? '取消星號' : j.is_playlist ? '整個清單都標示星號' : '標示星號：下載後想優先看'}" aria-label="星號">${j.starred ? '★' : '☆'}</button>
          <div class="title" title="${esc(title)}">${esc(title)}</div></div>
        <div class="url">${esc(j.file || j.url)}</div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="meta">${meta.join('')}</div>
        ${j.warning ? `<div class="warn">⚠ ${esc(j.warning)}</div>` : ''}
        ${j.error ? `<div class="err">${esc(j.error)}</div>` : ''}
      </div>
      <div class="acts">${acts.map(([a, t]) => `<button class="icon-btn${a === 'remove' ? ' danger' : ''}" data-act="${a}" title="${t}" aria-label="${t}">${ICON[a]}</button>`).join('')}</div>
    </div>`;
  }

  let lastJobsSig = '';
  let jobsCache = [];
  async function refreshJobs() {
    let data;
    try { data = await api('/api/jobs'); } catch { return; }
    jobsCache = data.jobs;
    paused = data.paused;
    $('#pauseBtn').textContent = paused ? '繼續佇列' : '暫停佇列';
    const sig = JSON.stringify(data);
    if (sig === lastJobsSig) return;
    lastJobsSig = sig;
    const jobs = data.jobs;
    $('#jobList').innerHTML = jobs.map(jobHTML).join('');
    $('#queueEmpty').hidden = jobs.length > 0;
    const c = s => jobs.filter(j => j.status === s).length;
    const active = c('queued') + c('running');
    $('#stats').innerHTML = [['全部', jobs.length], ['進行中', c('running')], ['排隊', c('queued')], ['完成', c('done')], ['失敗', c('error')]]
      .map(([k, v]) => `<span class="stat">${k} <b>${v}</b></span>`).join('') + (paused ? '<span class="stat" style="color:var(--warn)">已暫停</span>' : '');
    const badge = $('#navBadge'); badge.hidden = !active; badge.textContent = active;
    document.title = active ? `(${active}) 影片下載器` : '影片下載器';
  }
  $('#jobList').addEventListener('click', async e => {
    const btn = e.target.closest('[data-act]'); if (!btn) return;
    const id = btn.closest('.job').dataset.id, act = btn.dataset.act;
    if (act === 'play') {
      const j = jobsCache.find(x => x.id === id);
      show('library'); await loadLibrary(); playFile(j.file); return;
    }
    if (act === 'subscribe') {
      const j = jobsCache.find(x => x.id === id);
      const r = await api('/api/subs', { method: 'POST', body: { urls: [j.url], track_only: true, title: (j.title || '').replace(/^📃\s*/, '') } });
      toast(r.added ? '已加入訂閱清單（現有影片不重複下載，之後掃描只抓新影片）' : '這個清單已經訂閱過了', 4000); loadSubs(); return;
    }
    if (act === 'force' && !confirm('這部影片之前已下載過，確定要重新下載並覆蓋嗎？（字幕也會重新處理與翻譯）')) return;
    if (act === 'cancel_children' && !confirm('取消這個播放清單中所有「尚未開始」的影片？')) return;
    const r = await api(`/api/jobs/${id}/${act}`, { method: 'POST' });
    if (act === 'cancel_children') toast(`已取消 ${r.canceled} 部`);
    refreshJobs();
  });
  setInterval(refreshJobs, 1000);
  refreshJobs();

  /* ---------------------------------------------------- 訂閱清單 */
  let subsCache = [], subsTimer = null;
  const when = t => t ? new Date(t * 1000).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  function renderSubs(list) {
    subsCache = list;
    $('#subEmpty').hidden = list.length > 0;
    const scanning = list.filter(x => x.scanning).length;
    const pending = list.reduce((a, x) => a + (x.pending || 0), 0);
    $('#subStats').innerHTML = `<span class="stat">訂閱 <b>${list.length}</b></span>` +
      (scanning ? `<span class="stat" style="color:var(--primary)">掃描中 <b>${scanning}</b></span>` : '') +
      (pending ? `<span class="stat">新影片下載中 <b>${pending}</b></span>` : '');
    $('#subScanAll').disabled = $('#subFillAll').disabled = !list.length;
    const b = $('#subsBadge'); b.hidden = !pending; b.textContent = pending;
    $('#subList').innerHTML = list.map(x => {
      const m = [];
      if (x.scanning) m.push('<span class="status" style="color:var(--primary)">掃描中…</span>');
      else if (x.last_error) m.push(`<span class="chip err" title="${esc(x.last_error)}">掃描失敗</span>`);
      else if (!x.last_scan) m.push('<span>尚未掃描</span>');
      else if (x.last_new) m.push(`<span class="chip new">上次新增 ${x.last_new} 部</span>`);
      else m.push('<span>上次沒有新影片</span>');
      if (x.pending) m.push(`<span class="chip pl">${x.pending} 部下載中</span>`);
      if (x.last_scan) m.push(`<span>最後掃描 ${when(x.last_scan)}</span>`);
      if (x.last_total != null) {
        const have = x.last_have != null ? x.last_have + (x.last_new || 0) - (x.pending || 0) : null;
        const lack = x.last_have != null ? x.last_total - x.last_have - (x.last_new || 0) - (x.last_deleted || 0) : 0;
        m.push(`<span>清單共 ${x.last_total} 部${x.last_have != null ? `，影片庫已有 ${Math.max(0, have)} 部` : ''}</span>`);
        if (x.last_deleted) m.push(`<span title="這些影片以前下載過、後來被你刪除，視為已看完，不會再下載">${x.last_deleted} 部看過已刪除</span>`);
        if (lack > 0) m.push(`<span class="chip warn-chip" title="這些影片在清單裡，但從來沒有下載過（例如加入訂閱時選了只追蹤新影片）">${lack} 部未下載</span>`);
      }
      return `<div class="sub${x.scanning ? ' scanning' : ''}" data-id="${x.id}">
        <div><div class="t">${esc(x.title || '（掃描後會顯示清單名稱）')}</div>
          <div class="u"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a></div>
          <div class="m">${m.join('')}</div>
          ${x.last_error ? `<div class="err">${esc(x.last_error)}</div>` : ''}</div>
        <div class="acts">
          <label class="check" title="這個清單的新影片下載後自動加星號"><input type="checkbox" data-sub="star" ${x.star ? 'checked' : ''}> 自動星號</label>
          <label class="check" title="每次掃描都把清單裡從沒下載過的影片抓下來（你刪除過的影片視為已看完，不會再抓）"><input type="checkbox" data-sub="fill" ${x.fill ? 'checked' : ''}> 一律補齊</label>
          <label class="check" title="這個清單的影片不做重點摘要（批次摘要時自動略過）"><input type="checkbox" data-sub="nosum" ${x.no_summary ? 'checked' : ''}> 不做摘要</label>
          <button class="btn sm" data-sub="scan" ${x.scanning ? 'disabled' : ''}>掃描</button>
          <button class="btn sm" data-sub="fillnow" ${x.scanning ? 'disabled' : ''} title="把這個清單裡從沒下載過的影片全部下載（你刪除過的影片視為已看完，不會再抓）">補齊缺少的</button>
          <button class="btn sm" data-sub="show" title="在影片庫顯示這個清單的影片">看影片</button>
          <button class="icon-btn danger" data-sub="remove" title="取消訂閱（已下載的影片會保留）" aria-label="取消訂閱">${ICON.remove}</button>
        </div></div>`;
    }).join('');
    clearInterval(subsTimer);
    if (scanning || pending) subsTimer = setInterval(loadSubs, 2000);
  }
  async function loadSubs() { try { renderSubs((await api('/api/subs')).subs); } catch {} }
  $('#subAdd').onclick = async () => {
    const text = $('#subUrl').value.trim();
    if (!text) return toast('請貼上播放清單或頻道網址');
    try {
      const mode = $('#subMode').value;
      const r = await api('/api/subs', { method: 'POST', body: { text, track_only: mode === 'track', fill: mode === 'fill', star: $('#subStar').checked } });
      $('#subUrl').value = '';
      toast(r.added ? `已加入 ${r.added} 個訂閱，正在讀取清單內容` + (r.duplicates ? `（${r.duplicates} 個已訂閱過）` : '') : '這些網址都已經訂閱過了', 4000);
      renderSubs(r.subs);
    } catch (e) { toast(e.message); }
  };
  async function scanSubs(ids, fill = false) {
    const r = await api('/api/subs/scan', { method: 'POST', body: { ...(ids ? { ids } : {}), fill } });
    toast(r.paused ? '下載佇列目前是暫停狀態，請先到下載佇列按「繼續佇列」才會開始掃描' : r.started ? `開始掃描 ${r.started} 個清單，${fill ? '影片庫沒有的影片都會' : '有新影片會自動'}加入下載佇列` : '已經在掃描中', 5000);
    renderSubs(r.subs);
  }
  $('#subScanAll').onclick = () => scanSubs(null);
  $('#subFillAll').onclick = () => { if (confirm('要把所有訂閱清單中「從沒下載過」的影片全部下載嗎？（你刪除過的影片視為已看完，不會再下載）')) scanSubs(null, true); };
  $('#subList').addEventListener('click', async e => {
    const b = e.target.closest('[data-sub]'); if (!b || b.type === 'checkbox') return;
    const id = b.closest('.sub').dataset.id, x = subsCache.find(v => v.id === id);
    if (b.dataset.sub === 'scan') return scanSubs([id]);
    if (b.dataset.sub === 'fillnow') return scanSubs([id], true);
    if (b.dataset.sub === 'remove') {
      if (!confirm(`取消訂閱「${x?.title || x?.url}」？已下載的影片不會被刪除。`)) return;
      renderSubs((await api('/api/subs/' + id, { method: 'POST', body: { remove: true } })).subs);
    }
    if (b.dataset.sub === 'show') {
      show('library'); await loadLibrary();
      const tag = (x?.title || '').trim().replace(/\s+/g, '_').replace(/[,，、;；#＃]/g, '').slice(0, 30);
      const f = $('#tagFilter');
      if ([...f.options].some(o => o.value.toLowerCase() === tag.toLowerCase())) { f.value = [...f.options].find(o => o.value.toLowerCase() === tag.toLowerCase()).value; }
      else { $('#libSearch').value = x?.title || ''; }
      renderLibrary();
    }
  });
  $('#subList').addEventListener('change', async e => {
    const id = e.target.closest('.sub')?.dataset.id;
    if (e.target.dataset.sub === 'fill') {
      await api('/api/subs/' + id, { method: 'POST', body: { fill: e.target.checked } });
      toast(e.target.checked ? '之後每次掃描都會補齊這個清單缺少的影片' : '之後只下載新出現的影片'); return;
    }
    if (e.target.dataset.sub === 'nosum') {
      await api('/api/subs/' + id, { method: 'POST', body: { no_summary: e.target.checked } });
      toast(e.target.checked ? '這個清單的影片之後不會做重點摘要（批次摘要時會自動略過）' : '這個清單的影片恢復可以做重點摘要');
      loadLibrary(); return;
    }
    if (e.target.dataset.sub !== 'star') return;
    await api('/api/subs/' + id, { method: 'POST', body: { star: e.target.checked } });
    toast(e.target.checked ? '之後這個清單的新影片會自動加星號' : '已關閉自動星號');
  });
  loadSubs();

  /* ---------------------------------------------------- 影片庫 */
  let libItems = [];
  let current = null;
  async function loadLibrary() {
    const data = await api('/api/library');
    libItems = data.items;
    renderLock(data);
    for (const f of [...picked]) if (!libItems.some(i => i.file === f)) picked.delete(f);
    renderLibrary();
  }
  const HIDDEN_TAG = '隱藏';
  const PAGE = 60;                 // 一次只畫 60 張卡片，捲到底再載入，影片再多也不會拖慢
  let shown = PAGE, lastListSig = '';
  const cardDate = d => d && /^\d{8}$/.test(d) ? `${d.slice(0, 4)}/${d.slice(4, 6)}/${d.slice(6)} 上傳` : '';
  const cardDur = v => { v = Math.round(v); const h = Math.floor(v / 3600), m = Math.floor(v % 3600 / 60), s2 = String(v % 60).padStart(2, '0'); return h ? `${h}:${String(m).padStart(2, '0')}:${s2}` : `${m}:${s2}`; };
  let selecting = false;
  const picked = new Set();
  const tagsOf = i => i.meta?.hashtags || [];
  function allTags() {
    const m = new Map();
    libItems.forEach(i => tagsOf(i).forEach(t => { const k = t.toLowerCase(); const e = m.get(k) || { tag: t, n: 0 }; e.n++; m.set(k, e); }));
    return [...m.values()].sort((a, b) => b.n - a.n || a.tag.localeCompare(b.tag, 'zh-Hant'));
  }
  // hashtag 依名稱排序（英數字在前、中文在後；不分大小寫、數字照大小、開頭的 _ 不計）；選單文字不加 #，在下拉選單按字母即可跳到該項
  const tagKey = t => t.replace(/^_+/, '');
  const tagGrp = t => /^[0-9A-Za-z]/.test(tagKey(t)) ? 0 : 1;        // 英數字在前、中文在後
  const byName = (a, b) => tagGrp(a) - tagGrp(b) ||
    tagKey(a).localeCompare(tagKey(b), 'zh-Hant-TW', { numeric: true, sensitivity: 'base' });
  let excluded = store.get('tagExcludes', null) || (store.get('tagExclude', '') ? [store.get('tagExclude', '')] : []);
  function renderTagControls() {
    const tags = allTags().sort((a, b) => byName(a.tag, b.tag));
    const f = $('#tagFilter'), cur = f.value;
    f.innerHTML = '<option value="">全部 hashtag</option><option value="__none">（尚未加 hashtag）</option>' +
      tags.map(t => `<option value="${esc(t.tag)}">${esc(t.tag)}（${t.n}）</option>`).join('');
    f.value = [...f.options].some(o => o.value === cur) ? cur : '';
    // 排除 hashtag（可複選）：和幫影片加 hashtag 一樣，輸入（可一次多個）後按「排除」；已排除的顯示在下方，按 ✕ 取消
    $('#exclForm').classList.toggle('on', excluded.length > 0);
    const chips = $('#exclChips');
    chips.hidden = !excluded.length;
    chips.innerHTML = excluded.length ? '<span class="hint">不顯示含有：</span>' +
      excluded.map(t => `<button class="excl-chip" data-unexcl="${esc(t)}" title="取消排除 #${esc(t)}">#${esc(t)} <span aria-hidden="true">✕</span></button>`).join('') +
      '<button class="btn sm" data-unexcl="__all">全部取消</button>' : '';
    $('#tagList').innerHTML = tags.map(t => `<option value="${esc(t.tag)}">`).join('');
  }
  function visibleItems() {
    const q = $('#libSearch').value.trim().toLowerCase();
    const tf = $('#tagFilter').value;
    const hay = i => [i.title, i.meta?.original_title, i.meta?.description, i.meta?.uploader, i.meta?.channel,
      (i.meta?.tags || []).join(' '), tagsOf(i).map(t => '#' + t).join(' '), i.meta?.note, i.meta?.site,
      i.meta?.source_url, i.meta?.webpage_url].filter(Boolean).join('\n').toLowerCase();
    // 搜尋框裡「-#標籤」或「-詞」代表排除；可同時排除多個
    const all = q.split(/\s+/).filter(Boolean);
    const words = all.filter(w => !(w.startsWith('-') && w.length > 1));
    const notWords = all.filter(w => w.startsWith('-') && w.length > 1).map(w => w.slice(1));
    const xs = new Set(excluded.map(t => t.toLowerCase()));
    const starOnly = $('#starFilter').classList.contains('on');
    return sortItems(libItems.filter(i => {
      const wf = $('#watchFilter').value, sf = $('#sumFilter').value;
      if (starOnly && !i.meta?.starred) return false;
      if (wf === 'watched' && !i.meta?.watched) return false;
      if (wf === 'unwatched' && i.meta?.watched) return false;
      if (wf === 'nosub' && i.subs.length) return false;
      if (sf === 'yes' && !i.summary) return false;
      if (sf === 'no' && i.summary) return false;
      if (sf === 'img' && !(i.slides || []).length) return false;
      if (sf === 'noimg' && (i.slides || []).length) return false;
      if (sf === 'any' && !i.summary && !(i.slides || []).length) return false;
      if (tf === '__none' && tagsOf(i).length) return false;
      if (tf && tf !== '__none' && !tagsOf(i).some(t => t.toLowerCase() === tf.toLowerCase())) return false;
      if (xs.size && tagsOf(i).some(t => xs.has(t.toLowerCase()))) return false;
      const h = hay(i);
      if (notWords.some(w => w.startsWith('#') ? tagsOf(i).some(t => t.toLowerCase() === w.slice(1)) : h.includes(w))) return false;
      return words.every(w => h.includes(w));
    }));
  }
  function sortItems(items) {
    const mode = $('#sortSel').value, top = $('#starTop').checked;
    const dl = i => i.meta?.downloaded || i.mtime || 0;
    const up = i => i.meta?.upload_date || '';
    const dur = i => i.meta?.duration || 0;
    // 沒有該項資料（例如舊影片沒有上傳日期）的一律排最後
    const withMissing = (key, dir) => (a, b) => {
      const x = key(a), y = key(b);
      if (!x && !y) return dl(b) - dl(a);
      if (!x) return 1; if (!y) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * dir || dl(b) - dl(a);
    };
    const cmp = {
      dl_desc: (a, b) => dl(b) - dl(a), dl_asc: (a, b) => dl(a) - dl(b),
      up_desc: withMissing(up, -1), up_asc: withMissing(up, 1),
      dur_asc: withMissing(dur, 1), dur_desc: withMissing(dur, -1),
      title: (a, b) => a.title.localeCompare(b.title, 'zh-Hant', { numeric: true }),
      size_desc: (a, b) => (b.bytes || 0) - (a.bytes || 0),
    }[mode] || ((a, b) => dl(b) - dl(a));
    return [...items].sort((a, b) => (top ? (!!b.meta?.starred - !!a.meta?.starred) : 0) || cmp(a, b));
  }
  function renderLibrary() {
    renderTagControls();
    const items = visibleItems();
    const tf = $('#tagFilter').value.toLowerCase();
    $('#libEmpty').hidden = libItems.length > 0;
    $('#libGrid').classList.toggle('selecting', selecting);
    const sig = items.map(i => i.file).join('|');
    if (sig !== lastListSig) { lastListSig = sig; shown = PAGE; }      // 篩選或排序改變 → 回到第一頁
    const total = items.length;
    const pageItems = items.slice(0, shown);
    $('#libMore').hidden = total <= shown;
    $('#libMoreText').textContent = `已顯示 ${Math.min(shown, total)}／${total} 部`;
    $('#libCount').textContent = total === libItems.length ? `共 ${total} 部` : `符合 ${total} 部／共 ${libItems.length} 部`;
    $('#libGrid').innerHTML = pageItems.map(i => `
      <div class="lib-item${current === i.file ? ' playing' : ''}${picked.has(i.file) ? ' picked' : ''}${i.hidden ? ' is-hidden' : ''}${i.meta?.watched ? ' is-watched' : ''}${i.meta?.starred ? ' is-starred' : ''}" data-file="${esc(i.file)}" tabindex="0">
        <span class="pick" aria-hidden="true">${picked.has(i.file) ? '✓' : ''}</span>
        <div class="poster"><img src="/thumb/${encodeURIComponent(i.file)}" loading="lazy" decoding="async" alt="">
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m10 8 6 4-6 4z" fill="#fff"/></svg>
          ${i.meta?.watched ? '<span class="seen">✓ 已看完</span>' : ''}
          <button class="star-btn${i.meta?.starred ? ' on' : ''}" data-star="${esc(i.file)}" aria-pressed="${!!i.meta?.starred}" title="${i.meta?.starred ? '取消星號' : '標示星號：想優先看'}" aria-label="星號">${i.meta?.starred ? '★' : '☆'}</button></div>
        <div class="body"><div class="t" title="${esc(i.title)}">${esc(i.title)}</div>
          ${i.meta?.uploader || i.meta?.site || i.meta?.upload_date ? `<div class="u">${esc([i.meta.site, i.meta.uploader || i.meta.channel, cardDate(i.meta.upload_date), i.meta.duration ? cardDur(i.meta.duration) : ''].filter(Boolean).join(' · '))}</div>` : ''}
          ${tagsOf(i).length ? `<div class="tags">${tagsOf(i).map(t => `<button class="tag${t.toLowerCase() === tf ? ' on' : ''}" data-tag="${esc(t)}">#${esc(t)}</button>`).join('')}</div>` : ''}
          ${i.meta?.description ? `<div class="d">${esc(i.meta.description.slice(0, 200))}</div>` : ''}
          <div class="m"><span>${esc(i.size)}</span>${i.folder ? `<span class="chip folder-chip" title="位於下載資料夾裡的「${esc(i.folder)}」子資料夾">📁 ${esc(i.folder)}</span>` : ''}${i.subs.map(s => `<span class="chip">${esc(s.label)}</span>`).join('')}${i.meta?.asr && i.subs.length ? '<span class="chip" title="這部影片原本沒有字幕，字幕是語音辨識產生的">語音辨識</span>' : ''}${!i.subs.length ? (i.meta?.no_speech ? '<span class="chip" title="這部影片沒有聲音或沒有人說話，無法產生字幕">無語音</span>' : '<span class="chip warn-chip">無字幕</span>') : ''}${i.untranslated ? `<span class="chip warn-chip" title="中文字幕中還有未翻譯的句子">${i.untranslated} 句未翻</span>` : ''}${i.summary ? `<button class="chip sm-chip" data-summary="${esc(i.file)}" title="開啟重點摘要">📝 已摘要</button>` : ''}${(i.slides || []).length ? `<button class="chip g-chip" data-gallery="${esc(i.file)}" title="逐頁觀看圖片摘要">🖼 圖片摘要 ${i.slides.length}</button>` : ''}
            ${(i.meta?.source_url || i.meta?.webpage_url) ? `<a class="src" href="${esc(i.meta.source_url || i.meta.webpage_url)}" target="_blank" rel="noopener" title="開啟來源網址" aria-label="開啟來源網址" style="margin-left:auto"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a>` : ''}
            <button class="del" data-del="${esc(i.file)}" title="刪除影片與字幕" aria-label="刪除"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button></div>
        </div>
      </div>`).join('');
    renderSelBar();
  }

  /* ---- 整理模式（多選、hashtag、隱藏） */
  function renderSelBar() {
    $('#selBar').hidden = !selecting;
    $('#selectModeBtn').classList.toggle('primary', selecting);
    if (!selecting) return;
    const sel = libItems.filter(i => picked.has(i.file));
    $('#selCount').textContent = `已選 ${sel.length} 部`;
    const m = new Map();
    sel.forEach(i => tagsOf(i).forEach(t => m.set(t.toLowerCase(), { tag: t, n: (m.get(t.toLowerCase())?.n || 0) + 1 })));
    const tags = [...m.values()].sort((a, b) => byName(a.tag, b.tag));
    $('#tagRemoveSel').innerHTML = '<option value="">移除 hashtag…</option>' + tags.map(t => `<option value="${esc(t.tag)}">${esc(t.tag)}（${t.n}）</option>`).join('');
    $('#selTags').innerHTML = sel.length ? (tags.length ? '所選影片的 hashtag：' + tags.map(t => `<span class="tag">#${esc(t.tag)} ${t.n}/${sel.length}</span>`).join('') : '所選影片還沒有 hashtag') : '點影片卡片即可選取，可搭配上方搜尋或 hashtag 篩選後按「全選」';
    ['selDelete', 'selHide', 'selUnhide', 'selWatched', 'selUnwatched', 'selStar', 'selUnstar', 'selAsr', 'selSummary'].forEach(id => $('#' + id).disabled = !sel.length);
    $('#tagRemoveSel').disabled = !tags.length;
  }
  const splitTags = v => v.split(/[,，、\s]+/).map(t => t.replace(/^[#＃]+/, '').trim()).filter(Boolean);
  async function changeTags(files, add = [], remove = []) {
    if (!files.length) return toast('請先選取影片');
    const r = await api('/api/library/hashtags', { method: 'POST', body: { files, add, remove } });
    await loadLibrary();
    return r;
  }
  $('#selectModeBtn').onclick = () => { selecting = !selecting; if (!selecting) picked.clear(); renderLibrary(); };
  $('#selDone').onclick = () => { selecting = false; picked.clear(); renderLibrary(); };
  $('#selAll').onclick = () => { visibleItems().forEach(i => picked.add(i.file)); renderLibrary(); };
  $('#selNone').onclick = () => { picked.clear(); renderLibrary(); };
  $('#tagAddForm').onsubmit = async e => {
    e.preventDefault();
    const tags = splitTags($('#tagAddInput').value);
    if (!tags.length) return toast('請輸入 hashtag');
    const r = await changeTags([...picked], tags);
    if (r) { $('#tagAddInput').value = ''; toast(`已為 ${picked.size} 部影片加上 ${tags.map(t => '#' + t).join(' ')}`); }
  };
  $('#tagRemoveSel').onchange = async e => {
    const t = e.target.value; if (!t) return;
    await changeTags([...picked], [], [t]); toast(`已移除 #${t}`);
  };
  $('#selHide').onclick = async () => {
    const st = await api('/api/lock/status');
    if (!st.has_password) { toast('請先到「設定 → 隱藏影片」設定密碼', 5000); return; }
    const n = picked.size;
    await changeTags([...picked], [HIDDEN_TAG]);
    toast(st.unlocked ? `已隱藏 ${n} 部（目前為解鎖狀態，上鎖後就看不到）` : `已隱藏 ${n} 部`, 4000);
    if (!st.unlocked) picked.clear(), renderLibrary();
  };
  $('#selUnhide').onclick = async () => { await changeTags([...picked], [], [HIDDEN_TAG]); toast('已取消隱藏'); };
  $('#selDelete').onclick = async () => {
    const files = [...picked];
    if (!confirm(`確定要刪除 ${files.length} 部影片（含字幕與資訊檔）嗎？此動作無法復原。`)) return;
    if (files.includes(current)) closePlayer();
    await api('/api/library/delete-many', { method: 'POST', body: { files } });
    picked.clear(); toast(`已刪除 ${files.length} 部`); loadLibrary();
  };
  $('#tagFilter').onchange = renderLibrary;
  $('#exclForm').onsubmit = e => {
    e.preventDefault();
    const known = new Map(allTags().map(t => [t.tag.toLowerCase(), t.tag]));   // 用既有 hashtag 的寫法
    const add = splitTags($('#exclInput').value).map(t => known.get(t.toLowerCase()) || t);
    if (!add.length) return;
    for (const t of add) if (!excluded.some(x => x.toLowerCase() === t.toLowerCase())) excluded.push(t);
    $('#exclInput').value = '';
    store.set('tagExcludes', excluded); renderLibrary();
    const miss = add.filter(t => !known.has(t.toLowerCase()));
    if (miss.length) toast(`影片庫目前沒有 #${miss.join('、#')}，已先加入排除清單`);
  };
  $('#exclChips').addEventListener('click', e => {
    const b = e.target.closest('[data-unexcl]'); if (!b) return;
    excluded = b.dataset.unexcl === '__all' ? [] : excluded.filter(t => t !== b.dataset.unexcl);
    store.set('tagExcludes', excluded); renderLibrary();
  });
  $('#watchFilter').onchange = renderLibrary;
  $('#sumFilter').onchange = renderLibrary;

  /* ---- 星號、排序 */
  async function setStarred(files, on) {
    if (!files.length) return toast('請先選取影片');
    await api('/api/library/starred', { method: 'POST', body: { files, starred: on } });
    libItems.forEach(i => { if (files.includes(i.file)) { i.meta = i.meta || {}; i.meta.starred = on; } });
    renderLibrary(); renderStarBtn();
  }
  function renderStarBtn() {
    const on = !!curItem()?.meta?.starred, b = $('#npStar');
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', on);
    b.textContent = on ? '★ 已加星號' : '☆ 星號';
  }
  const toggleStar = async () => { if (current) await setStarred([current], !curItem()?.meta?.starred); };
  $('#npStar').onclick = toggleStar;
  /* ---- 批次語音辨識 */
  let batchTimer = null, batchWasRunning = false;
  function renderBatch(b) {
    const card = $('#batchCard'), btn = $('#batchAsrBtn');
    if (b.running || batchWasRunning || b.total && !card.hidden) {
      card.hidden = false;
      const finished = b.done + b.failed + b.canceled;
      const cur = b.current;
      const pct = b.total ? (finished + (cur ? (cur.progress || 0) / 100 : 0)) / b.total * 100 : 0;
      $('#batchBar').style.width = (b.running ? pct : 100) + '%';
      $('#batchTitle').textContent = b.running ? '批次語音辨識進行中（可以繼續使用其他功能）' : '批次語音辨識已結束';
      $('#batchInfo').textContent = `完成 ${b.done}／共 ${b.total} 部` + (b.failed ? `，失敗 ${b.failed}` : '') + (b.canceled ? `，取消 ${b.canceled}` : '');
      let eta = `已經過 ${fmtSec(b.elapsed)}`;
      if (b.running && pct > 2) eta += `，約剩 ${fmtSec(b.elapsed * (100 - pct) / pct)}`;
      $('#batchEta').textContent = eta;
      $('#batchCur').textContent = cur ? `正在處理：${cur.file}　${cur.detail || ''}　${Math.round(cur.progress || 0)}%` : '';
      $('#batchCancel').hidden = !b.running; $('#batchClose').hidden = b.running;
      const e = $('#batchErr'); e.hidden = !b.errors.length;
      e.textContent = b.errors.map(x => `${x.file}：${x.error}`).join('\n');
    }
    if (batchWasRunning && !b.running) { toast(`批次語音辨識結束：完成 ${b.done} 部` + (b.failed ? `，失敗 ${b.failed} 部` : ''), 6000); loadLibrary(); }
    if (b.running && b.done !== renderBatch.lastDone) { renderBatch.lastDone = b.done; if ($('#view-library').classList.contains('active')) loadLibrary(); }
    batchWasRunning = b.running;
    btn.hidden = b.running || !b.missing;
    if (b.missing) btn.textContent = `🎙 ${b.missing} 部影片沒有字幕：批次語音辨識`;
    clearInterval(batchTimer);
    batchTimer = setInterval(loadBatch, b.running ? 1500 : 30000);
  }
  async function loadBatch() { try { renderBatch(await api('/api/asr/batch')); } catch {} }
  async function startBatch(files) {
    try { renderBatch(await api('/api/asr/batch', { method: 'POST', body: files ? { files } : {} })); $('#batchCard').hidden = false; loadBatch(); }
    catch (e) { toast(e.message, 8000); }
  }
  $('#batchAsrBtn').onclick = () => { if (confirm('要為所有沒有字幕的影片做語音辨識嗎？會在背景一部一部處理，影片多時可能需要很久，可隨時停止。')) startBatch(null); };
  $('#batchCancel').onclick = async () => { await api('/api/asr/batch/cancel', { method: 'POST' }); toast('正在停止，目前這部會中斷，尚未開始的不會處理'); };
  $('#batchClose').onclick = () => { $('#batchCard').hidden = true; };
  $('#selAsr').onclick = () => {
    const files = [...picked];
    const has = libItems.filter(i => files.includes(i.file) && i.subs.length).length;
    if (confirm(`要為所選的 ${files.length} 部影片做語音辨識嗎？` + (has ? `其中 ${has} 部已有字幕，會被新的辨識結果覆蓋。` : ''))) startBatch(files);
  };
  loadBatch();

  $('#selStar').onclick = async () => { const n = picked.size; await setStarred([...picked], true); if (n) toast(`已為 ${n} 部加上星號`); };
  $('#selUnstar').onclick = async () => { const n = picked.size; await setStarred([...picked], false); if (n) toast(`已取消 ${n} 部的星號`); };
  $('#sortSel').value = store.get('sort', 'dl_desc');
  if (!$('#sortSel').value) $('#sortSel').value = 'dl_desc';
  $('#sortSel').onchange = e => { store.set('sort', e.target.value); renderLibrary(); };
  $('#starTop').checked = store.get('starTop', true);
  $('#starTop').onchange = e => { store.set('starTop', e.target.checked); renderLibrary(); };
  $('#starFilter').onclick = e => {
    const b = e.currentTarget, on = !b.classList.contains('on');
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); renderLibrary();
  };

  /* ---- 已看完 */
  async function setWatched(files, watched) {
    if (!files.length) return toast('請先選取影片');
    await api('/api/library/watched', { method: 'POST', body: { files, watched } });
    libItems.forEach(i => { if (files.includes(i.file)) { i.meta = i.meta || {}; i.meta.watched = watched; } });
    renderLibrary(); renderWatchedBtn();
  }
  function renderWatchedBtn() {
    const on = !!curItem()?.meta?.watched, b = $('#npWatched');
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', on);
    b.textContent = on ? '✓ 已看完' : '標為已看完';
  }
  const toggleWatched = async () => {
    if (!current) return;
    const on = !curItem()?.meta?.watched;
    await setWatched([current], on); toast(on ? '已標示為已看完' : '已改回未看');
  };
  $('#npWatched').onclick = toggleWatched;
  $('#selWatched').onclick = async () => { const n = picked.size; await setWatched([...picked], true); if (n) toast(`已將 ${n} 部標為已看完`); };
  $('#selUnwatched').onclick = async () => { const n = picked.size; await setWatched([...picked], false); if (n) toast(`已將 ${n} 部標為未看`); };
  $('#autoWatched').checked = store.get('autoWatched', true);
  $('#autoWatched').onchange = e => store.set('autoWatched', e.target.checked);

  /* ---- 隱藏影片解鎖 */
  let unlocked = false;
  function renderLock(st) {
    unlocked = !!st.unlocked;
    $('#lockBtn').classList.toggle('unlocked', unlocked);
    $('#lockText').textContent = unlocked ? '重新上鎖' : '隱藏影片';
    $('#lockShackle').setAttribute('d', unlocked ? 'M8 11V8a4 4 0 0 1 7.8-1.2' : 'M8 11V8a4 4 0 0 1 8 0v3');
  }
  $('#lockBtn').onclick = async () => {
    if (unlocked) {
      await api('/api/lock/lock', { method: 'POST' });
      toast('已上鎖，隱藏影片不再顯示');
      if (libItems.find(i => i.file === current)?.hidden) closePlayer();
      picked.clear(); loadLibrary(); return;
    }
    const st = await api('/api/lock/status');
    if (!st.has_password) { toast('尚未設定密碼，請先到「設定 → 隱藏影片」設定', 5000); return; }
    $('#unlockForm').hidden = false; $('#unlockMsg').textContent = '解鎖後 2 小時未使用、或重新啟動程式會自動上鎖';
    $('#unlockInput').value = ''; $('#unlockInput').focus();
  };
  $('#unlockCancel').onclick = () => $('#unlockForm').hidden = true;
  $('#unlockForm').onsubmit = async e => {
    e.preventDefault();
    try {
      await api('/api/lock/unlock', { method: 'POST', body: { password: $('#unlockInput').value } });
      $('#unlockForm').hidden = true; $('#unlockInput').value = '';
      toast('已解鎖，顯示隱藏影片'); loadLibrary();
    } catch (err) { $('#unlockMsg').textContent = err.message; $('#unlockInput').select(); }
  };
  $('#libMoreBtn').onclick = () => { shown += PAGE; renderLibrary(); };
  new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && !$('#libMore').hidden) { shown += PAGE; renderLibrary(); } },
    { rootMargin: '600px' }).observe($('#libMore'));
  let searchTimer;
  $('#libSearch').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(renderLibrary, 150); };
  $('#libRefresh').onclick = loadLibrary;
  $('#libGrid').addEventListener('click', async e => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      if (!confirm('確定要刪除這部影片與它的字幕檔嗎？此動作無法復原。')) return;
      if (current === del.dataset.del) closePlayer();
      await api('/api/library/delete', { method: 'POST', body: { file: del.dataset.del } });
      toast('已刪除'); loadLibrary(); return;
    }
    const gc = e.target.closest('[data-gallery]');
    if (gc && !selecting) { e.stopPropagation(); openGallery(gc.dataset.gallery); return; }
    const smc = e.target.closest('[data-summary]');
    if (smc && !selecting) { e.stopPropagation(); openSummary(smc.dataset.summary); return; }
    const st = e.target.closest('[data-star]');
    if (st) { const it0 = libItems.find(i => i.file === st.dataset.star); await setStarred([st.dataset.star], !it0?.meta?.starred); return; }
    if (e.target.closest('a.src')) return;
    const it = e.target.closest('.lib-item');
    if (!it) return;
    if (selecting) {
      picked.has(it.dataset.file) ? picked.delete(it.dataset.file) : picked.add(it.dataset.file);
      renderLibrary(); return;
    }
    const tg = e.target.closest('[data-tag]');
    if (tg) {
      const f = $('#tagFilter');
      f.value = f.value.toLowerCase() === tg.dataset.tag.toLowerCase() ? '' : tg.dataset.tag;
      renderLibrary(); return;
    }
    playFile(it.dataset.file);
  });
  $('#libGrid').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.classList.contains('lib-item')) playFile(e.target.dataset.file); });

  /* ---------------------------------------------------- 播放器 */
  const video = $('#video'), player = $('#player');
  const fmt = s => { if (!isFinite(s)) return '0:00'; s = Math.floor(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = String(s % 60).padStart(2, '0'); return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`; };

  function playFile(file) {
    const item = libItems.find(i => i.file === file);
    if (!item) return toast('找不到影片檔');
    current = file;
    $('#playerWrap').hidden = false;
    $('#npTitle').textContent = item.title;
    cancelRename();
    renderInfo(item);
    renderWatchedBtn(); renderStarBtn();
    if (!$('#npTranslate').disabled) $('#trProgress').hidden = true;
    if (!$('#npSummary').disabled) $('#smProgress').hidden = true;
    $('#npGallery').hidden = !(item.slides || []).length;
    $('#npGallery').textContent = `🖼 圖片摘要（${(item.slides || []).length}）`;
    $('#npDownload').href = '/media/' + encodeURIComponent(file);
    $$('track', video).forEach(t => t.remove());
    video.src = '/media/' + encodeURIComponent(file);
    item.subs.forEach((s, i) => {
      const t = document.createElement('track');
      t.kind = 'subtitles'; t.label = s.label; t.srclang = s.lang.startsWith('zh') ? 'zh-TW' : s.lang;
      t.src = '/vtt/' + encodeURIComponent(s.file);
      video.appendChild(t);
    });
    const sel = $('#subSel');
    sel.innerHTML = '<option value="-1">關閉字幕</option>' + item.subs.map((s, i) => `<option value="${i}">${esc(s.label)}</option>`).join('');
    const want = store.get('subOn', true) && item.subs.length ? 0 : -1;
    sel.value = String(want);
    setTimeout(() => applySub(want), 50);
    const hasForeign = item.subs.some(s => !s.lang.startsWith('zh-TW'));
    $('#npTranslate').hidden = !hasForeign;
    setTranslateLabel(item); setAsrLabel(item);
    video.playbackRate = +store.get('speed', 1);
    $('#speedSel').value = String(video.playbackRate);
    video.play().catch(() => {});
    renderLibrary();
    $('#playerWrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
    player.focus({ preventScroll: true });
  }
  // 字幕自己繪製在控制列上方（瀏覽器內建字幕會被控制列遮住）
  let activeTrack = null;
  const overlay = $('#subOverlay');
  function renderCues() {
    const cues = activeTrack?.activeCues ? [...activeTrack.activeCues] : [];
    overlay.innerHTML = cues.map(c => c.text.replace(/<[^>]+>/g, '').split('\n')
      .map(l => `<span>${esc(l)}</span>`).join('')).join('');
  }
  function applySub(idx) {
    if (activeTrack) activeTrack.oncuechange = null;
    [...video.textTracks].forEach(t => t.mode = 'hidden');   // hidden 仍會載入並觸發 cuechange
    activeTrack = video.textTracks[+idx] || null;
    if (activeTrack) activeTrack.oncuechange = renderCues;
    renderCues();
  }
  const measureCtl = () => player.style.setProperty('--ctl-h', $('#controls').offsetHeight + 'px');
  new ResizeObserver(measureCtl).observe($('#controls'));
  function closePlayer() { video.pause(); video.removeAttribute('src'); video.load(); $('#playerWrap').hidden = true; current = null; renderLibrary(); }
  $('#npClose').onclick = closePlayer;

  /* ---- 影片資訊：原文簡介、備註、改名 */
  const fmtDate = d => d && /^\d{8}$/.test(d) ? `${d.slice(0, 4)}/${d.slice(4, 6)}/${d.slice(6)}` : '';
  const linkify = t => esc(t).replace(/https?:\/\/[^\s<]+/g, u => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  function renderInfo(item) {
    const m = item.meta || {};
    const rows = [];
    if (m.original_title && m.original_title !== item.title) rows.push(['原文標題', esc(m.original_title)]);
    if (m.webpage_url && m.source_url && m.webpage_url !== m.source_url) rows.push(['實際網址', `<a href="${esc(m.webpage_url)}" target="_blank" rel="noopener">${esc(m.webpage_url)}</a>`]);
    if (m.site) rows.push(['網站', esc(m.site)]);
    if (m.playlist) rows.push(['播放清單', `${m.playlist.url ? `<a href="${esc(m.playlist.url)}" target="_blank" rel="noopener">${esc(m.playlist.title)}</a>` : esc(m.playlist.title)}（第 ${m.playlist.index}／${m.playlist.count} 部）`]);
    if (m.uploader || m.channel) rows.push(['上傳者', esc(m.uploader || m.channel)]);
    if (m.upload_date) rows.push(['上傳日期', fmtDate(m.upload_date)]);
    if (m.duration) rows.push(['長度', fmt(m.duration)]);
    if (m.tags?.length) rows.push(['標籤', esc(m.tags.slice(0, 20).join('、'))]);
    const src = m.source_url || m.webpage_url || '';
    $('#npSrcLink').href = src || '#'; $('#npSrcLink').textContent = src || '（未記錄）';
    $('#npCopyUrl').disabled = !src;
    $('#npSrcForm').hidden = !!src; $('#npSrcView').hidden = !src; $('#npSrcInput').value = src;
    $('#npMeta').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
    const d = $('#npDesc');
    d.classList.remove('open');
    d.classList.toggle('empty', !m.description);
    d.innerHTML = m.description ? linkify(m.description) : (src ? '沒有簡介。可按「重新抓取」從原網址取得。' : '沒有簡介，也不知道來源網址。');
    $('#npCopyDesc').disabled = !m.description;
    $('#npFetchMeta').hidden = !src;
    requestAnimationFrame(() => { $('#npDescMore').hidden = d.scrollHeight <= d.clientHeight + 4; $('#npDescMore').textContent = '展開全文'; });
    $('#npTags').innerHTML = (m.hashtags || []).map(t => `<span class="tag" data-ptag="${esc(t)}">#${esc(t)}<span class="x" data-rmtag="${esc(t)}" title="移除" role="button" aria-label="移除 ${esc(t)}">✕</span></span>`).join('') || '<span class="hint">尚未加 hashtag</span>';
    $('#npNote').value = m.note || '';
    $('#npNoteMsg').textContent = '';
  }
  $('#npTagForm').onsubmit = async e => {
    e.preventDefault();
    const tags = splitTags($('#npTagInput').value);
    if (!tags.length) return;
    await changeTags([current], tags); $('#npTagInput').value = '';
    renderInfo(curItem());
  };
  $('#npTags').addEventListener('click', async e => {
    const rm = e.target.closest('[data-rmtag]');
    if (rm) { await changeTags([current], [], [rm.dataset.rmtag]); renderInfo(curItem()); return; }
    const t = e.target.closest('[data-ptag]');
    if (t) { $('#tagFilter').value = t.dataset.ptag; renderLibrary(); $('#libGrid').scrollIntoView({ behavior: 'smooth' }); }
  });
  $('#npDescMore').onclick = () => { const d = $('#npDesc'); d.classList.toggle('open'); $('#npDescMore').textContent = d.classList.contains('open') ? '收合' : '展開全文'; };
  const curItem = () => libItems.find(i => i.file === current);
  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); }
    catch { const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  }
  $('#npCopyDesc').onclick = async () => {
    const m = curItem()?.meta || {};
    const text = [m.original_title, m.source_url || m.webpage_url, '', m.description].filter(v => v !== undefined).join('\n').trim();
    await copyText(text); toast('已複製標題、網址與簡介');
  };
  $('#npFetchMeta').onclick = async () => {
    const btn = $('#npFetchMeta'); btn.disabled = true; btn.textContent = '抓取中…';
    try {
      const r = await api('/api/library/meta', { method: 'POST', body: { file: current, fetch: true } });
      const it = curItem(); if (it) { it.meta = r.meta; renderInfo(it); renderLibrary(); }
      toast('已更新原文簡介');
    } catch (e) { toast(e.message); }
    btn.disabled = false; btn.textContent = '重新抓取';
  };
  $('#npCopyUrl').onclick = async () => { const m = curItem()?.meta || {}; await copyText(m.source_url || m.webpage_url || ''); toast('已複製來源網址'); };
  $('#npEditUrl').onclick = () => { $('#npSrcView').hidden = true; $('#npSrcForm').hidden = false; $('#npSrcInput').focus(); };
  $('#npSrcCancel').onclick = () => renderInfo(curItem());
  $('#npSrcForm').onsubmit = async e => {
    e.preventDefault();
    const url = $('#npSrcInput').value.trim();
    if (!url) return toast('請輸入網址');
    try {
      const r = await api('/api/library/meta', { method: 'POST', body: { file: current, url } });
      const it = curItem(); if (it) { it.meta = r.meta; renderInfo(it); renderLibrary(); }
      toast('已記錄來源網址，可按「重新抓取」取得簡介');
    } catch (e) { toast(e.message); }
  };
  $('#npNoteSave').onclick = async () => {
    try {
      const r = await api('/api/library/meta', { method: 'POST', body: { file: current, note: $('#npNote').value } });
      const it = curItem(); if (it) it.meta = r.meta;
      $('#npNoteMsg').textContent = '已儲存 ✓'; setTimeout(() => $('#npNoteMsg').textContent = '', 2500);
    } catch (e) { toast(e.message); }
  };
  function cancelRename() { $('#npRenameForm').hidden = true; $('#npTitleView').hidden = false; }
  $('#npEdit').onclick = () => {
    $('#npRenameInput').value = curItem()?.title || '';
    $('#npTitleView').hidden = true; $('#npRenameForm').hidden = false;
    $('#npRenameInput').focus(); $('#npRenameInput').select();
  };
  $('#npRenameCancel').onclick = cancelRename;
  $('#npRenameInput').addEventListener('keydown', e => { if (e.key === 'Escape') cancelRename(); });
  $('#npRenameForm').onsubmit = async e => {
    e.preventDefault();
    const title = $('#npRenameInput').value.trim();
    if (!title) return toast('名稱不可空白');
    const t = video.currentTime, wasPaused = video.paused;
    // 先釋放影片檔（播放器與縮圖），Windows 才能改名
    const old = current;
    video.pause(); video.removeAttribute('src'); video.load();
    await new Promise(r => setTimeout(r, 400));
    try {
      const r = await api('/api/library/rename', { method: 'POST', body: { file: old, title } });
      {
        await loadLibrary(); playFile(r.file);
        video.addEventListener('loadedmetadata', () => { video.currentTime = t; if (wasPaused) video.pause(); }, { once: true });
        toast(r.file !== old ? '已更改名稱（字幕與資訊檔一併更名）' : '名稱未變更');
      }
    } catch (e) { toast(e.message); playFile(old); }
  };
  function setTranslateLabel(item) {
    const btn = $('#npTranslate');
    if (!item) return;
    const hasZh = item.subs.some(s => s.lang === 'zh-TW');
    btn.dataset.mode = item.untranslated ? 'fill' : 'full';
    btn.textContent = !hasZh ? '翻譯成中文字幕' : item.untranslated ? `補翻未翻譯的 ${item.untranslated} 句` : '重新翻譯中文字幕';
    btn.classList.toggle('primary', !!item.untranslated);
  }
  const fmtSec = v => v < 60 ? `${Math.round(v)} 秒` : `${Math.floor(v / 60)} 分 ${Math.round(v % 60)} 秒`;
  let trTimer = null;
  function showTask(t) {
    const box = $('#trProgress');
    box.hidden = false;
    box.classList.toggle('indet', t.status === 'running' && !t.progress);
    box.classList.toggle('err', t.status === 'error');
    $('#trBar').style.width = (t.status === 'done' ? 100 : t.progress) + '%';
    const isAsr = t.kind === 'asr';
    $('#trTitle').textContent = isAsr
      ? (t.status === 'done' ? '語音辨識完成，已產生字幕' : t.status === 'error' ? '語音辨識失敗' : '語音辨識產生字幕中（Breeze-ASR-25）')
      : (t.status === 'done' ? (t.untranslated ? `翻譯完成（${t.untranslated} 句未翻譯）` : '翻譯完成') : t.status === 'error' ? '翻譯失敗' : `翻譯字幕中（${t.engine}）`);
    $('#trInfo').textContent = t.status === 'error' ? t.error : isAsr ? (t.detail || '') : `共 ${t.lines} 句${t.detail ? '・' + t.detail : ''}`;
    $('#trCancel').hidden = !(isAsr && t.status === 'running');
    $('#trCancel').dataset.tid = t.id || '';
    $('#trPct').textContent = t.status === 'error' ? '' : Math.round(t.status === 'done' ? 100 : t.progress) + '%';
    let eta = `已經過 ${fmtSec(t.elapsed)}`;
    if (t.status === 'running' && t.progress > 3) eta += `，約剩 ${fmtSec(t.elapsed * (100 - t.progress) / t.progress)}`;
    $('#trEta').textContent = eta;
    if (!isAsr) $('#npTranslate').textContent = t.status === 'running' ? `翻譯中 ${Math.round(t.progress)}%` : $('#npTranslate').textContent;
    else $('#npAsr').textContent = t.status === 'running' ? `辨識中 ${Math.round(t.progress)}%` : $('#npAsr').textContent;
  }
  async function watchTask(tid, file) {
    clearInterval(trTimer);
    const btn = $('#npTranslate');
    btn.disabled = true; $('#npAsr').disabled = true;
    const tick = async () => {
      let t;
      try { t = await api('/api/tasks/' + tid); } catch { return; }
      if (current === file) showTask(t);
      if (t.status === 'running') return;
      clearInterval(trTimer);
      btn.disabled = false; $('#npAsr').disabled = false;
      if (t.status === 'done') {
        toast(t.kind === 'asr' ? '語音辨識完成，已產生字幕' : t.untranslated ? `翻譯完成，但仍有 ${t.untranslated} 句未翻譯，可再按「補翻」` : '已產生正體中文字幕，全部翻譯成功', 5000);
        await loadLibrary();
        if (current === file) {
          const time = video.currentTime; playFile(file);
          video.addEventListener('loadedmetadata', () => video.currentTime = time, { once: true });
          showTask(t);
          if (!t.untranslated) setTimeout(() => $('#trProgress').hidden = true, 4000);
        }
      } else toast(t.error, 8000);
      setTranslateLabel(curItem()); setAsrLabel(curItem());
    };
    trTimer = setInterval(tick, 1000);
    tick();
  }
  function setAsrLabel(item) {
    const b = $('#npAsr');
    if (!item) return;
    // 沒有任何字幕 → 顯示「語音辨識產生字幕」；字幕本來就是辨識出來的 → 可重新辨識
    b.hidden = !!item.subs.length && !item.meta?.asr;
    b.textContent = item.subs.length ? '重新語音辨識' : '語音辨識產生字幕';
    b.classList.toggle('primary', !item.subs.length);
  }
  $('#npAsr').onclick = async () => {
    const file = current, it = curItem();
    if (it?.subs.length && !confirm('要重新做語音辨識嗎？（會覆蓋現有的字幕）')) return;
    try {
      const r = await api('/api/library/transcribe', { method: 'POST', body: { file } });
      showTask({ kind: 'asr', status: 'running', progress: 0, detail: '準備中…', elapsed: 0, id: r.task });
      watchTask(r.task, file);
    } catch (e) { toast(e.message, 8000); }
  };
  $('#trCancel').onclick = async e => { if (e.target.dataset.tid) await api(`/api/tasks/${e.target.dataset.tid}/cancel`, { method: 'POST' }); };
  $('#npTranslate').onclick = async () => {
    const file = current;
    try {
      const mode = $('#npTranslate').dataset.mode || 'full';
      if (mode === 'full' && curItem()?.subs.some(s => s.lang === 'zh-TW') &&
          !confirm('要整份重新翻譯嗎？（會覆蓋現有的中文字幕）')) return;
      const r = await api('/api/library/translate', { method: 'POST', body: { file, mode } });
      showTask({ status: 'running', progress: 0, engine: '', lines: '…', detail: '準備中…', elapsed: 0 });
      watchTask(r.task, file);
    } catch (e) { toast(e.message, 8000); }
  };
  $('#subSel').onchange = e => { applySub(e.target.value); store.set('subOn', +e.target.value >= 0); };
  const applyCue = v => { player.style.setProperty('--sub-scale', (+v || 100) / 100); store.set('cue', v); };
  $('#cueSize').value = store.get('cue', '100'); applyCue($('#cueSize').value);
  $('#cueSize').onchange = e => applyCue(e.target.value);
  $('#speedSel').onchange = e => { video.playbackRate = +e.target.value; store.set('speed', +e.target.value); };

  const togglePlay = () => video.paused ? video.play() : video.pause();
  $('#playBtn').onclick = togglePlay; $('#bigPlay').onclick = togglePlay;
  video.onclick = togglePlay;
  video.ondblclick = () => toggleFs();
  $('#back10').onclick = () => video.currentTime -= 10;
  $('#fwd10').onclick = () => video.currentTime += 10;
  const setIcon = () => {
    player.classList.toggle('is-playing', !video.paused);
    $('#playIcon').innerHTML = video.paused ? '<path d="M8 5v14l11-7z"/>' : '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>';
  };
  video.onplay = video.onpause = setIcon;
  // 載入中／播放失敗的提示
  const setBusy = on => player.classList.toggle('is-loading', on);
  video.addEventListener('loadstart', () => { setBusy(true); $('#playerMsg').hidden = true; });
  video.addEventListener('waiting', () => setBusy(true));
  ['canplay', 'playing', 'loadeddata'].forEach(ev => video.addEventListener(ev, () => setBusy(false)));
  video.addEventListener('error', async () => {
    setBusy(false);
    if (!current || !video.getAttribute('src')) return;
    const code = video.error?.code;
    const box = $('#playerMsg');
    let msg = code === 4 ? '瀏覽器無法播放這個影片格式。' : code === 3 ? '影片解碼失敗，檔案可能損壞或編碼不支援。'
      : code === 2 ? '讀取影片時連線中斷，請再試一次。' : '影片無法播放。';
    try {
      const info = await api('/api/library/probe?file=' + encodeURIComponent(current));
      if (info.video) msg += `（影像：${info.video}，聲音：${info.audio || '無'}，${info.size}）`;
      if (/hevc|h265|av1|vp9/i.test(info.video || '') ) msg += ' 這種編碼在部分電腦上無法於瀏覽器播放，可改用「另存影片」後以 VLC 等播放器開啟。';
    } catch {}
    box.textContent = msg; box.hidden = false;
  });
  video.onended = () => {
    if (store.get('autoWatched', true) && current && !curItem()?.meta?.watched)
      setWatched([current], true).then(() => toast('已播放完畢，自動標示為已看完'));
  };
  video.ontimeupdate = () => {
    $('#time').textContent = `${fmt(video.currentTime)} / ${fmt(video.duration)}`;
    if (!seeking && video.duration) $('#seek').value = video.currentTime / video.duration * 1000;
  };
  let seeking = false;
  $('#seek').oninput = e => { seeking = true; if (video.duration) video.currentTime = e.target.value / 1000 * video.duration; };
  $('#seek').onchange = () => seeking = false;
  $('#volume').value = store.get('vol', 1); video.volume = +$('#volume').value;
  $('#volume').oninput = e => { video.volume = +e.target.value; video.muted = false; store.set('vol', video.volume); };
  $('#muteBtn').onclick = () => video.muted = !video.muted;
  video.onvolumechange = () => { $('#volWave').style.display = video.muted || !video.volume ? 'none' : ''; $('#volume').value = video.muted ? 0 : video.volume; };
  const toggleFs = () => document.fullscreenElement ? document.exitFullscreen() : player.requestFullscreen().catch(() => video.requestFullscreen?.());
  $('#fsBtn').onclick = toggleFs;
  $('#pipBtn').onclick = () => document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture().catch(() => toast('此瀏覽器不支援子母畫面'));

  let idleTimer;
  const wake = () => { player.classList.remove('idle'); clearTimeout(idleTimer); idleTimer = setTimeout(() => player.classList.add('idle'), 2500); };
  player.addEventListener('mousemove', wake); player.addEventListener('touchstart', wake, { passive: true });

  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  document.addEventListener('keydown', e => {
    if ($('#playerWrap').hidden || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
    if (!$('#view-library').classList.contains('active')) return;
    const k = e.key;
    const act = {
      ' ': togglePlay, k: togglePlay, K: togglePlay,
      ArrowLeft: () => video.currentTime -= 5, ArrowRight: () => video.currentTime += 5,
      ArrowUp: () => video.volume = Math.min(1, video.volume + .05), ArrowDown: () => video.volume = Math.max(0, video.volume - .05),
      f: toggleFs, F: toggleFs, w: toggleWatched, W: toggleWatched, s: toggleStar, S: toggleStar, m: () => video.muted = !video.muted, M: () => video.muted = !video.muted,
      c: () => { const s = $('#subSel'); s.value = s.value === '-1' && s.options.length > 1 ? '0' : '-1'; s.onchange({ target: s }); toast(s.value === '-1' ? '字幕已關閉' : '字幕：' + s.selectedOptions[0].text); },
      '>': () => stepSpeed(1), '.': () => stepSpeed(1), '<': () => stepSpeed(-1), ',': () => stepSpeed(-1),
    }[k];
    if (act) { e.preventDefault(); act(); wake(); }
  });
  function stepSpeed(d) {
    const i = SPEEDS.indexOf(video.playbackRate);
    const n = SPEEDS[Math.max(0, Math.min(SPEEDS.length - 1, (i < 0 ? 2 : i) + d))];
    video.playbackRate = n; $('#speedSel').value = String(n); store.set('speed', n); toast(`播放速度 ${n}×`);
  }

  /* ---------------------------------------------------- 設定 */
  const form = $('#settingsForm');
  async function loadSettings() {
    const s = await api('/api/settings');
    for (const [k, v] of Object.entries(s)) {
      const el = form.elements[k]; if (!el) continue;
      if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
    }
    syncCookieMode(); syncEngine();
    loadEnv(); loadLock(); loadAsr(); loadModels();
  }
  async function loadEnv() {
    const e = await api('/api/env');
    const c = e.cookies;
    $('#envInfo').innerHTML = `<dl class="env" style="margin:0">
      <dt>程式版本</dt><dd>v${esc(e.version)}</dd>
      <dt>yt-dlp</dt><dd>${esc(e.ytdlp)}</dd>
      <dt>ffmpeg</dt><dd>${e.ffmpeg ? esc(e.ffmpeg) : '<span style="color:var(--err)">未找到，請執行 start.bat 安裝</span>'}</dd>
      <dt>下載資料夾</dt><dd>${esc(e.download_dir)}</dd></dl>`;
    $('#extStatus').innerHTML = c.exists
      ? `✅ 擴充功能已同步 <b>${c.count}</b> 筆 Cookie（${new Date(c.updated * 1000).toLocaleString('zh-TW')}）<br>檔案位置：<code style="word-break:break-all">${esc(c.path)}</code>${c.youtube_path ? `；YouTube 登入：<code style="word-break:break-all">${esc(c.youtube_path)}</code>` : ''}<br><span class="hint">${Object.entries(c.domains).map(([d, n]) => `${esc(d)}(${n})`).join('、')}</span>`
      : '⚠ 尚未收到擴充功能同步的 Cookie。請安裝專案中的 chrome_extension 並按「同步 Cookie」；未同步時會改用「直接讀取瀏覽器 Cookie」。';
    const pr = e.ext_pair || {};
    $('#extStatus').innerHTML += pr.id
      ? `<br>🔗 已配對擴充功能：<code>${esc(pr.id)}</code>（${new Date(pr.paired * 1000).toLocaleString('zh-TW')}起），其他擴充功能無法送資料給下載器
         <button type="button" class="btn sm" id="unpairBtn">解除配對</button>`
      : '<br>🔗 尚未配對擴充功能：第一個按「同步 Cookie」的擴充功能會自動配對';
    const ub = $('#unpairBtn');
    if (ub) ub.onclick = async () => {
      if (!confirm('解除配對後，下一個按「同步 Cookie」的擴充功能會自動配對。確定要解除嗎？')) return;
      await api('/api/ext/unpair', { method: 'POST' }); loadEnv();
    };
    const y = c.youtube;
    if (y) {
      $('#extStatus').innerHTML += `<br>${y.logged_in ? '✅ YouTube：登入狀態' : '⚠ YouTube：<b>未登入</b>（Cookie 裡沒有登入資訊）'}，登入來源：<b>${esc(y.source)}</b>` +
        (y.private_updated ? `（無痕同步於 ${new Date(y.private_updated * 1000).toLocaleString('zh-TW')}）
          <button type="button" class="btn sm" id="clearPrivBtn">改回使用一般分頁的 YouTube 登入</button>`
          : `<br><span class="hint">會員影片下載失敗時，可用擴充功能的「從無痕視窗同步 YouTube 登入」，避免 Cookie 被 YouTube 輪替而失效（見使用說明）</span>`);
      const cb = $('#clearPrivBtn');
      if (cb) cb.onclick = async () => {
        if (!confirm('移除無痕視窗同步的 YouTube 登入，改用一般分頁同步的 Cookie？')) return;
        await api('/api/cookies/private', { method: 'DELETE' }); loadEnv();
      };
    }
    updatePill(c);
    const l = e.llm || {};
    $('#llmStatus').innerHTML = l.configured
      ? `✅ 已讀取 .env 的 AI 設定：模型 <b>${esc(l.model)}</b>（${esc(l.host)}，${esc(l.style)} 格式）`
      + ((l.mask || []).length ? `<br>🛡 遮蔽詞：<b>${l.mask.map(esc).join('、')}</b>（送給 AI 前換成代號，避免被 Azure 封鎖清單擋下）`
          + ((l.mask_learned || []).length ? ` <button type="button" class="btn sm" id="maskClearBtn">清除自動學到的遮蔽詞</button>` : '') : '')
      : (l.problem ? `⚠ AI 翻譯設定有問題：<b>${esc(l.problem)}</b>。` : '⚠ 尚未設定 AI 翻譯。') + '請將專案中的 <code>.env.example</code> 複製為 <code>.env</code> 並填入端點、金鑰與模型名稱，再重新啟動程式。未設定時會自動改用 Google 翻譯。';
    const mc = $('#maskClearBtn');
    if (mc) mc.onclick = async () => { await api('/api/llm-mask', { method: 'DELETE' }); loadEnv(); toast('已清除自動學到的遮蔽詞'); };
  }
  function updatePill(c) {
    const p = $('#cookiePill');
    p.classList.toggle('ok', !!c.exists);
    $('#cookieText').textContent = c.exists ? `Cookie 已同步（${c.count}）` : 'Cookie 未同步';
    p.title = c.exists ? '擴充功能已同步 Cookie' : '尚未同步 Cookie';
  }
  form.onsubmit = async e => {
    e.preventDefault();
    const body = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      body[el.name] = el.type === 'checkbox' ? el.checked : (['max_height', 'playlist_max'].includes(el.name) ? +el.value : el.value.trim());
    }
    await api('/api/settings', { method: 'POST', body });
    $('#saveMsg').textContent = '已儲存 ✓'; setTimeout(() => $('#saveMsg').textContent = '', 2500);
    loadEnv(); loadAsr(); loadModels();
  };
  // 只顯示目前 Cookie 來源用得到的欄位
  const syncCookieMode = () => {
    const m = form.elements.cookie_mode.value;
    $$('[data-mode]', form).forEach(el => el.hidden = el.dataset.mode !== m);
    $('#testCookieResult').textContent = '';
  };
  form.elements.cookie_mode.addEventListener('change', syncCookieMode);
  $('#untagBtn').onclick = async () => {
    if (!confirm('要移除先前產生重點摘要時自動加到影片上的 hashtag 嗎？\n\n會比對每部影片的摘要：影片上和摘要相同的 hashtag 會被移除（若其中有你自己加的同名 hashtag，也會一起移除）。')) return;
    try {
      const r = await api('/api/summary/untag', { method: 'POST' });
      toast(r.removed ? `已從 ${r.videos} 部影片移除 ${r.removed} 個 hashtag` : '沒有需要移除的 hashtag', 5000);
      loadLibrary();
    } catch (e) { toast(e.message, 6000); }
  };
  $('#testCookieBtn').onclick = async () => {
    const out = $('#testCookieResult'); out.textContent = '讀取中…';
    const r = await api('/api/cookies/test', { method: 'POST', body: { browser: form.elements.browser.value, profile: form.elements.browser_profile.value } });
    out.textContent = r.ok ? `成功讀到 ${r.count} 筆：${Object.keys(r.domains).slice(0, 8).join('、')}`
      : /could not copy|database/i.test(r.error) ? '失敗：瀏覽器開著時會鎖住 Cookie 資料庫，程式讀不到。用 Chrome 請改選「Chrome 擴充功能同步」（不必關閉 Chrome）'
      : /decrypt|dpapi|app.bound/i.test(r.error) ? '失敗：Chrome 新版的加密方式讓外部程式無法解讀 Cookie，請改選「Chrome 擴充功能同步」'
      : `失敗：${r.error.slice(0, 200)}（建議改用擴充功能同步）`;
  };
  async function loadAsr() {
    const a = await api('/api/asr/status');
    $('#asrStatus').innerHTML = (a.problem ? `⚠ ${esc(a.problem)}` : `✅ 已找到模型：<span style="word-break:break-all">${esc(a.model_dir)}</span>`) +
      `<br>${a.turbo_problem ? `ℹ 沒有 Whisper large-v3-turbo（${esc(a.turbo_dir)}）：外語影片也會用 Breeze 辨識` : `✅ 外語模型 Whisper large-v3-turbo：<span style="word-break:break-all">${esc(a.turbo_dir)}</span>`}` +
      `<br>${a.installed ? `✅ 語音辨識元件已安裝，可用裝置：${esc(a.devices.join('、') || 'CPU')}` : 'ℹ 語音辨識元件（openvino-genai）尚未安裝，第一次辨識時會自動安裝，約需幾分鐘'}`;
  }
  // 本機 AI 模型位置與各模型狀態（1.25.0）
  async function loadModels() {
    const m = await api('/api/models');
    const src = { settings: '網頁設定', env: '環境變數 MODELS_ROOT', default: '預設' }[m.source] || m.source;
    const l = m.local || {};
    const dl = l.download || {};
    const busy = dl.state === 'running';
    const action = x => {
      if (x.ready || !(l.downloadable || []).includes(x.key)) return '';
      if (busy && dl.key === x.key) return `<br><span class="hint">⏳ ${esc(dl.msg)}</span>`;
      return `<br><button type="button" class="btn sm" data-dl="${x.key}" ${busy ? 'disabled' : ''}>下載${x.gb ? `（約 ${x.gb} GB）` : ''}</button>`
        + (x.key === l.recommended ? ' <span class="hint">這台電腦建議用這個</span>' : '');
    };
    const rows = m.models.map(x => `<tr><td>${x.ready ? '✅' : '—'}</td><td>${esc(x.label)}${x.key === l.gemma ? ' <b>（本機摘要使用）</b>' : ''}${action(x)}</td><td>${esc(x.use)}</td><td style="word-break:break-all"><code>${esc(x.path)}</code></td></tr>`).join('');
    $('#modelsStatus').innerHTML = (m.root_exists ? `✅ 模型位置：<code>${esc(m.root)}</code>（${esc(src)}）` : `⚠ 模型位置 <code>${esc(m.root)}</code>（${esc(src)}）不存在`) +
      `<br>這台電腦記憶體約 ${l.ram_gb || '?'} GB，建議摘要用 Gemma 4 ${esc((l.recommended || '').toUpperCase())}${l.loaded ? `；目前載入：${esc(l.loaded)}（${esc(l.device)}）` : ''}` +
      `<div style="overflow-x:auto"><table class="models-table"><thead><tr><th></th><th>模型</th><th>用途</th><th>位置</th></tr></thead><tbody>${rows}</tbody></table></div>` +
      (dl.state === 'error' ? `<div style="color:var(--err)">⚠ ${esc(dl.msg)}：${esc(dl.error)}</div>` : dl.state === 'done' && dl.msg ? `<div>✅ ${esc(dl.msg)}</div>` : '') +
      `<span class="hint">— 表示這台電腦還沒有該模型。Gemma 4 可直接按「下載」（Hugging Face 公開模型，不需帳號；在背景下載，可繼續使用其他功能，中斷後再按一次會續傳）。Breeze、turbo、TranslateGemma 需向管理者索取模型位置裡對應的資料夾（「影音轉逐字稿」的母機已有），複製到上面的位置即可，不用重新啟動。</span>`;
    $$('[data-dl]', $('#modelsStatus')).forEach(b => b.onclick = async () => {
      const lbl = m.models.find(x => x.key === b.dataset.dl)?.label || '';
      if (!confirm(`下載 ${lbl} 到 ${m.root}？\n檔案較大，會在背景下載，期間可照常使用。`)) return;
      try { await api('/api/models/download', { method: 'POST', body: { key: b.dataset.dl } }); }
      catch (e) { alert(e.message); }
      loadModels();
    });
    clearTimeout(loadModels.timer);
    if (busy) loadModels.timer = setTimeout(() => loadModels().catch(() => {}), 2000);   // 下載中每 2 秒更新進度
  }
  async function loadLock() {
    const st = await api('/api/lock/status');
    $('#lockStatus').innerHTML = st.has_password ? `✅ 已設定密碼${st.unlocked ? '（目前為解鎖狀態）' : ''}` : '⚠ 尚未設定密碼：設定後才能使用「隱藏影片」';
    $('#oldPwField').hidden = !st.has_password;
    $('#pwSave').textContent = st.has_password ? '變更密碼' : '設定密碼';
  }
  $('#pwSave').onclick = async () => {
    try {
      await api('/api/lock/set', { method: 'POST', body: { old: $('#pwOld').value, new: $('#pwNew').value } });
      $('#pwOld').value = $('#pwNew').value = '';
      $('#pwMsg').textContent = '已儲存 ✓（已自動上鎖）'; loadLock();
    } catch (e) { $('#pwMsg').textContent = e.message; }
  };
  $('#testLlmBtn').onclick = async () => {
    const btn = $('#testLlmBtn'), list = $('#diagList');
    btn.disabled = true; btn.textContent = '診斷中…';
    list.hidden = false;
    list.innerHTML = '<li class="run"><span class="ic">…</span><span class="nm">檢查中，最長約 1 分鐘</span><span></span></li>';
    let r;
    try { r = await api('/api/translator/diagnose', { method: 'POST' }); }
    catch (e) { r = { ok: false, steps: [{ name: '無法呼叫下載器', status: 'fail', detail: e.message }] }; }
    const icon = { ok: '✓', fail: '✕', warn: '!' };
    list.innerHTML = r.steps.map(st => `<li class="${st.status}"><span class="ic">${icon[st.status] || '·'}</span>
        <span class="nm">${esc(st.name)}</span><span class="ms">${st.ms != null ? st.ms + ' ms' : ''}</span>
        ${st.detail ? `<span class="dt">${esc(st.detail)}</span>` : ''}</li>`).join('')
      + `<li class="summary ${r.ok ? 'ok' : 'fail'}">${r.ok ? '✅ AI 翻譯連線正常，可以在上方「翻譯引擎」選擇 AI 翻譯' : '❌ 未通過：請依紅色項目的說明修改 .env，存檔後再按一次診斷（不需重新啟動）'}</li>`;
    $('#testLlmResult').textContent = `最後檢查：${new Date().toLocaleTimeString('zh-TW')}`;
    btn.disabled = false; btn.textContent = 'AI 連線診斷';
    loadEnv();
  };
  // 翻譯或重點摘要任一選了 Codex，就顯示 Codex 的路徑、模型與測試
  const syncEngine = () => {
    const useCodex = form.elements.summary_engine.value === 'codex' || form.elements.translator.value === 'codex';
    const useLocal = form.elements.summary_engine.value === 'local';
    $$('[data-engine]', form).forEach(el => el.hidden = !(el.dataset.engine === 'codex' ? useCodex : el.dataset.engine === 'local' && useLocal));
  };
  form.elements.summary_engine.addEventListener('change', syncEngine);
  form.elements.translator.addEventListener('change', syncEngine);
  $('#testCodexBtn').onclick = async () => {
    const btn = $('#testCodexBtn'), list = $('#codexDiag');
    btn.disabled = true; btn.textContent = '測試中…';
    list.hidden = false;
    list.innerHTML = '<li class="run"><span class="ic">…</span><span class="nm">呼叫 Codex 中，第一次可能要 10～60 秒</span><span></span></li>';
    let r;
    try { r = await api('/api/codex/test', { method: 'POST', body: { path: form.elements.codex_path.value.trim(), model: form.elements.codex_model.value.trim() } }); }
    catch (e) { r = { ok: false, steps: [{ name: '無法呼叫下載器', status: 'fail', detail: e.message }] }; }
    const icon = { ok: '✓', fail: '✕', warn: '!' };
    list.innerHTML = r.steps.map(st => `<li class="${st.status}"><span class="ic">${icon[st.status] || '·'}</span>
        <span class="nm">${esc(st.name)}</span><span class="ms">${st.ms != null ? st.ms + ' ms' : ''}</span>
        ${st.detail ? `<span class="dt">${esc(st.detail)}</span>` : ''}</li>`).join('')
      + `<li class="summary ${r.ok ? 'ok' : 'fail'}">${r.ok ? '✅ Codex 連線正常；記得按下方「儲存設定」，之後重點摘要就會改用 Codex' : '❌ 未通過：請依紅色項目的說明處理後再測試'}</li>`;
    $('#testCodexResult').textContent = `最後檢查：${new Date().toLocaleTimeString('zh-TW')}`;
    btn.disabled = false; btn.textContent = '測試 Codex 連線';
  };
  $('#updateBtn').onclick = async () => {
    const btn = $('#updateBtn'); btn.disabled = true; $('#updateResult').textContent = '更新中，請稍候…';
    try { const r = await api('/api/update-ytdlp', { method: 'POST' }); $('#updateResult').textContent = r.note; }
    catch (e) { $('#updateResult').textContent = e.message; }
    btn.disabled = false;
  };

  const FRONT_VERSION = '__APP_VERSION__';   // 由伺服器依 VERSION 檔填入
  api('/api/ping').then(r => {
    $('#appVer').textContent = 'v' + r.version;
    if (r.version !== FRONT_VERSION) {
      $('#appVer').style.color = 'var(--err)';
      $('#appVer').title = '伺服器版本與介面不一致';
      staleServer = r.version;
      const bar = document.createElement('div');
      bar.className = 'stale-bar'; bar.setAttribute('role', 'alert');
      bar.textContent = `程式已更新到 v${FRONT_VERSION}，但背景還在執行舊版 v${r.version}，新功能會無法使用。請關閉黑色視窗後重新執行 start.bat，再重新整理此頁。`;
      document.querySelector('.main').prepend(bar);
    }
  }).catch(() => {});
  api('/api/cookies/status').then(updatePill).catch(() => {});
  setInterval(() => api('/api/cookies/status').then(updatePill).catch(() => {}), 30000);

  /* ---------------------------------------------------- 重點摘要 */
  const smOv = $('#smOverlay');
  let smFile = null, smIdx = 0, smTimer = null, smLastFocus = null;
  const fmtT = s => { s = Math.floor(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = String(s % 60).padStart(2, '0'); return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`; };
  function renderSummary(s, file) {
    const has = s.has || {};
    const check = (k, lab) => `<li class="${has[k] ? 'on' : 'off'}"><span>${has[k] ? '✔' : '✕'}</span>${lab}</li>`;
    const im = libItems.find(i => i.file === file)?.meta || {};
    const ud = String(im.upload_date || s.upload_date || '');
    const info = [
      (im.duration || s.duration) ? ['影片長度', cardDur(im.duration || s.duration)] : null,
      /^\d{8}$/.test(ud) ? ['上架日期', `${ud.slice(0, 4)}/${ud.slice(4, 6)}/${ud.slice(6)}`] : null,
      (im.uploader || im.channel || s.uploader) ? ['頻道', im.uploader || im.channel || s.uploader] : null,
      (im.site || s.site) ? ['網站', im.site || s.site] : null,
    ].filter(Boolean);
    const infoHtml = info.length ? `<dl class="vinfo">${info.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>` : '';
    const slides = [`<section class="slide cover"><div class="kicker">這個影片主要講什麼</div>
      <h1>${esc(s.topic)}</h1>${infoHtml}
      ${(s.hashtags || []).length ? `<div class="tags">${s.hashtags.map(t => `<span>#${esc(t)}</span>`).join('')}</div>` : ''}
      <p class="outline">${esc(s.outline)}</p>
      <ul class="checks">${check('concept', '講概念')}${check('example', '有範例')}${check('code', '有程式')}</ul>
      ${(s.steps || []).length ? `<div class="steps"><div class="sl">步驟描述</div><ol>${s.steps.map(x => `<li>${esc(x)}</li>`).join('')}</ol></div>` : ''}</section>`];
    (s.points || []).forEach((p, i) => slides.push(`<section class="slide"><div class="kicker">重點 ${i + 1}${p.time != null ? `<button class="t" data-sec="${p.time}" title="關閉摘要並跳到影片這一段">▶ ${fmtT(p.time)}</button>` : ''}</div>
      <h2>${esc(p.title)}</h2><ul class="bullets">${(p.bullets || []).map(b => `<li>${esc(b)}</li>`).join('')}</ul></section>`));
    $('#smStage').innerHTML = slides.join('');
    const item = libItems.find(i => i.file === file);
    $('#smVideoTitle').textContent = item?.title || s.title || file;
    $('#smExport').href = '/api/summary-export/' + encodeURIComponent(file);
    const fl = s.filtered || [];
    $('#smWarn').hidden = !fl.length && !s.fallback;
    $('#smWarn').innerHTML = (s.fallback ? '⚠ AI 最後彙整時被 Azure 內容篩選擋下，這份摘要直接用各段重點筆記組成；可稍後按「重新產生」再試。' + (fl.length ? '<br>' : '') : '') + (fl.length ? `⚠ 以下 ${fl.length} 段字幕被 Azure 內容篩選擋下，摘要沒有包含：` +
      fl.map(d => d.sec != null ? `<button class="sm-wt" data-wsec="${d.sec}" title="${esc(d.text)}">▶ ${esc(d.time)}</button>` : `<span title="${esc(d.text)}">（時間不明）</span>`).join('') +
      `<span class="sm-wh">滑鼠移到時間上可看被擋的內容，詳細記錄在「錯誤紀錄」</span>` : '');
    $('#smMeta').textContent = `AI：${s.model || ''}　依據字幕：${s.source || ''}　產生於 ${new Date((s.generated || 0) * 1000).toLocaleString('zh-TW')}　｜　← → 換頁、Esc 關閉`;
    smGo(0);
  }
  function smGo(n) {
    const S = $$('.slide', $('#smStage'));
    if (!S.length) return;
    smIdx = Math.max(0, Math.min(S.length - 1, n));
    S.forEach((el, k) => el.classList.toggle('active', k === smIdx));
    smFit(S[smIdx]);
    $('#smPg').textContent = `${smIdx + 1} / ${S.length}`;
    $('#smPrev').disabled = smIdx === 0; $('#smNext').disabled = smIdx === S.length - 1;
  }
  // 內容放不下時逐步縮小字級（最小 18px），避免出現捲軸
  function smFit(el) {
    if (!el) return;
    let f = 1; el.style.setProperty('--fit', f);
    while (el.scrollHeight > el.clientHeight + 1 && f > 0.5) { f -= 0.05; el.style.setProperty('--fit', f.toFixed(2)); }
  }
  addEventListener('resize', () => { if (!smOv.hidden) smFit($$('.slide', $('#smStage'))[smIdx]); });
  async function openSummary(file) {
    let r;
    try { r = await api('/api/summary/' + encodeURIComponent(file)); }
    catch (e) {
      if (confirm('這部影片還沒有重點摘要，要現在用 AI 產生嗎？（沒有字幕的會先語音辨識）')) generateSummary(file);
      return;
    }
    smFile = file; smLastFocus = document.activeElement;
    renderSummary(r.summary, file);
    smOv.hidden = false;
    smGo(smIdx);                       // 顯示後才量得到高度，再調整一次字級
    if (!video.paused && current === file) video.pause();
    $('#smNext').focus();
  }
  function closeSummary() { smOv.hidden = true; smFile = null; smLastFocus?.focus?.(); }
  $('#smClose').onclick = closeSummary;
  smOv.addEventListener('click', e => { if (e.target === smOv) closeSummary(); });
  $('#smPrev').onclick = () => smGo(smIdx - 1);
  $('#smNext').onclick = () => smGo(smIdx + 1);
  $('#smWarn').addEventListener('click', e => {
    const b = e.target.closest('[data-wsec]');
    if (b) jumpTo(smFile, +b.dataset.wsec);
  });
  $('#smStage').addEventListener('click', e => {
    const b = e.target.closest('[data-sec]');
    if (b) jumpTo(smFile, +b.dataset.sec);
  });
  function jumpTo(file, sec) {
    closeSummary();
    if (!$('#view-library').classList.contains('active')) show('library');
    const seekPlay = () => { video.currentTime = sec; video.play().catch(() => {}); };
    if (current !== file) { playFile(file); video.addEventListener('loadedmetadata', seekPlay, { once: true }); }
    else { seekPlay(); $('#playerWrap').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  };
  // 燈箱開著時，鍵盤只控制摘要（不影響播放器快捷鍵）
  document.addEventListener('keydown', e => {
    if (smOv.hidden) return;
    const act = { ArrowRight: () => smGo(smIdx + 1), ArrowLeft: () => smGo(smIdx - 1), PageDown: () => smGo(smIdx + 1), PageUp: () => smGo(smIdx - 1),
      ' ': () => smGo(smIdx + 1), Escape: closeSummary, Home: () => smGo(0), End: () => smGo(99) }[e.key];
    if (e.key === 'Tab') {               // 焦點留在燈箱內
      const f = $$('button:not([disabled]), a[href]', smOv);
      if (!f.length) return;
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) { f[f.length - 1].focus(); e.preventDefault(); }
      else if (!e.shiftKey && i === f.length - 1) { f[0].focus(); e.preventDefault(); }
      e.stopImmediatePropagation(); return;
    }
    if (act && !(e.key === ' ' && document.activeElement?.tagName === 'BUTTON')) { e.preventDefault(); act(); }
    e.stopImmediatePropagation();
  }, true);
  let smX0 = null;
  $('#smStage').addEventListener('touchstart', e => smX0 = e.touches[0].clientX, { passive: true });
  $('#smStage').addEventListener('touchend', e => {
    if (smX0 === null) return;
    const d = e.changedTouches[0].clientX - smX0;
    if (Math.abs(d) > 50) smGo(smIdx + (d < 0 ? 1 : -1));
    smX0 = null;
  });
  $('#smRegen').onclick = () => {
    if (!smFile || !confirm('要請 AI 依字幕重新整理一次重點摘要嗎？（會覆蓋目前的摘要）')) return;
    const f = smFile; closeSummary(); generateSummary(f);
  };

  // 單一影片：在播放器下方顯示進度，完成後自動開啟
  function showSmTask(t, file) {
    const box = $('#smProgress');
    box.hidden = current !== file;
    const pct = t.status === 'done' ? 100 : Math.round(t.progress || 0);
    $('#smPBar').style.width = pct + '%';
    box.classList.toggle('error', t.status === 'error');
    $('#smPTitle').textContent = t.status === 'done' ? '重點摘要完成' : t.status === 'error' ? '重點摘要失敗' : '產生重點摘要中（AI）';
    $('#smPInfo').textContent = t.status === 'error' ? t.error : (t.detail || '');
    $('#smPPct').textContent = t.status === 'error' ? '' : pct + '%';
    $('#smPEta').textContent = `已經過 ${fmtSec(t.elapsed || 0)}`;
    $('#smPCancel').hidden = t.status !== 'running';
    $('#smPCancel').dataset.tid = t.id || '';
    $('#npSummary').textContent = t.status === 'running' ? `📝 摘要中 ${pct}%` : '📝 重點摘要';
    $('#npSummary').disabled = t.status === 'running';
  }
  async function generateSummary(file) {
    let r;
    try { r = await api('/api/library/summarize', { method: 'POST', body: { file } }); }
    catch (e) { toast(e.message, 8000); return; }
    if (current !== file) toast('已開始在背景產生重點摘要，完成後會自動開啟', 4000);
    clearInterval(smTimer);
    const tick = async () => {
      let t;
      try { t = await api('/api/tasks/' + r.task); } catch { return; }
      t.id = r.task;
      showSmTask(t, file);
      if (t.status === 'running') return;
      clearInterval(smTimer);
      if (t.status === 'done') {
        await loadLibrary();
        if (current === file) setTimeout(() => $('#smProgress').hidden = true, 3000);
        openSummary(file);
      } else toast('重點摘要失敗：' + t.error, 10000);
    };
    smTimer = setInterval(tick, 1200);
    tick();
  }
  $('#smPCancel').onclick = async e => { if (e.target.dataset.tid) await api(`/api/tasks/${e.target.dataset.tid}/cancel`, { method: 'POST' }); };
  $('#npSummary').onclick = () => {
    const it = curItem();
    if (!it) return;
    if (!it.summary && it.no_summary &&
        !confirm(`這部影片屬於訂閱「${it.no_summary}」，該訂閱設定為不做重點摘要。\n仍要為這一部產生摘要嗎？`)) return;
    it.summary ? openSummary(it.file) : generateSummary(it.file);
  };

  // 批次：整理模式勾選後產生
  let sumTimer = null, sumWasRunning = false;
  function renderSumBatch(b) {
    const card = $('#sumCard');
    if (b.running || sumWasRunning || (b.total && !card.hidden)) {
      card.hidden = false;
      const todo = b.total - b.skipped;
      const finished = b.done + b.failed;
      const cur = b.current;
      const pct = todo ? (finished + (cur ? (cur.progress || 0) / 100 : 0)) / todo * 100 : 100;
      $('#sumBar').style.width = (b.running ? pct : 100) + '%';
      $('#sumTitle').textContent = b.running ? '批次重點摘要進行中（可以繼續使用其他功能）' : '批次重點摘要已結束';
      $('#sumInfo').textContent = `完成 ${b.done}／共 ${todo} 部` + (b.skipped ? `（略過 ${b.skipped} 部：已有摘要或訂閱設定不做摘要）` : '') + (b.failed ? `，失敗 ${b.failed}` : '');
      let eta = `已經過 ${fmtSec(b.elapsed)}`;
      if (b.running && pct > 2) eta += `，約剩 ${fmtSec(b.elapsed * (100 - pct) / pct)}`;
      $('#sumEta').textContent = eta;
      $('#sumCur').textContent = cur ? `正在處理：${cur.file}　${cur.detail || ''}　${Math.round(cur.progress || 0)}%` : '';
      $('#sumCancel').hidden = !b.running; $('#sumClose').hidden = b.running;
      const e = $('#sumErr'); e.hidden = !b.errors.length;
      e.textContent = b.errors.map(x => `${x.file}：${x.error}`).join('\n');
    }
    if (b.running && b.done !== renderSumBatch.lastDone) { renderSumBatch.lastDone = b.done; if ($('#view-library').classList.contains('active')) loadLibrary(); }
    if (sumWasRunning && !b.running) { toast(`批次重點摘要結束：完成 ${b.done} 部` + (b.failed ? `，失敗 ${b.failed} 部` : ''), 6000); loadLibrary(); }
    sumWasRunning = b.running;
    clearInterval(sumTimer);
    if (b.running) sumTimer = setInterval(loadSumBatch, 1500);
  }
  async function loadSumBatch() { try { renderSumBatch(await api('/api/summary-batch')); } catch {} }
  $('#selSummary').onclick = async () => {
    const all = [...picked];
    if (!all.length) return;
    const blocked = libItems.filter(i => all.includes(i.file) && i.no_summary).length;
    const files = all.filter(f => !libItems.find(i => i.file === f)?.no_summary);   // 訂閱設定不做摘要的直接略過
    const note = blocked ? `（另有 ${blocked} 部屬於設定「不做摘要」的訂閱，會略過）` : '';
    if (!files.length) return toast('所選影片都屬於設定「不做摘要」的訂閱', 5000);
    const has = libItems.filter(i => files.includes(i.file) && i.summary).length;
    let force = false;
    if (has) force = confirm(`所選 ${files.length} 部中，有 ${has} 部已經有重點摘要。${note}\n\n按「確定」：全部重新產生\n按「取消」：只產生還沒有摘要的 ${files.length - has} 部`);
    else if (!confirm(`要用 AI 為所選的 ${files.length} 部影片產生重點摘要嗎？${note}沒有字幕的會先語音辨識，會在背景一部一部處理。`)) return;
    if (!force && has === files.length) return toast('所選影片都已經有重點摘要');
    try { renderSumBatch(await api('/api/summary-batch', { method: 'POST', body: { files, force } })); loadSumBatch(); }
    catch (e) { toast(e.message, 8000); }
  };
  $('#sumCancel').onclick = async () => { await api('/api/summary-batch/cancel', { method: 'POST' }); toast('正在停止，目前這部會中斷，尚未開始的不會處理'); };
  $('#sumClose').onclick = () => { $('#sumCard').hidden = true; };
  loadSumBatch();

  /* ---------------------------------------------------- 圖片摘要（影片標題01.png…） */
  const gOv = $('#gOverlay');
  let gList = [], gIdx = 0, gLastFocus = null;
  function openGallery(file) {
    const item = libItems.find(i => i.file === file);
    gList = item?.slides || [];
    if (!gList.length) return toast('這部影片沒有圖片摘要');
    gLastFocus = document.activeElement;
    $('#gTitle').textContent = item.title;
    gOv.hidden = false;
    if (current === file && !video.paused) video.pause();
    gGo(0); $('#gNext').focus();
  }
  function gGo(n) {
    gIdx = Math.max(0, Math.min(gList.length - 1, n));
    const src = '/simg/' + encodeURIComponent(gList[gIdx]);
    $('#gImg').src = src; $('#gOpen').href = src;
    $('#gImg').alt = `圖片摘要第 ${gIdx + 1} 張`;
    $('#gPg').textContent = `${gIdx + 1} / ${gList.length}`;
    $('#gPrev').disabled = gIdx === 0; $('#gNext').disabled = gIdx === gList.length - 1;
    if (gList[gIdx + 1]) new Image().src = '/simg/' + encodeURIComponent(gList[gIdx + 1]);   // 預先載入下一張
  }
  function closeGallery() { gOv.hidden = true; gLastFocus?.focus?.(); }
  $('#gClose').onclick = closeGallery;
  gOv.addEventListener('click', e => { if (e.target === gOv) closeGallery(); });
  $('#gPrev').onclick = () => gGo(gIdx - 1);
  $('#gNext').onclick = () => gGo(gIdx + 1);
  $('#gImg').onclick = e => { const r = e.target.getBoundingClientRect(); gGo(gIdx + (e.clientX - r.left < r.width / 3 ? -1 : 1)); };
  document.addEventListener('keydown', e => {
    if (gOv.hidden) return;
    const act = { ArrowRight: () => gGo(gIdx + 1), ArrowLeft: () => gGo(gIdx - 1), PageDown: () => gGo(gIdx + 1), PageUp: () => gGo(gIdx - 1),
      ' ': () => gGo(gIdx + 1), Home: () => gGo(0), End: () => gGo(gList.length - 1), Escape: closeGallery }[e.key];
    if (act && !(e.key === ' ' && document.activeElement?.tagName === 'BUTTON')) { e.preventDefault(); act(); }
    if (e.key !== 'Tab') e.stopImmediatePropagation();
  }, true);
  let gX0 = null;
  $('#gStage').addEventListener('touchstart', e => gX0 = e.touches[0].clientX, { passive: true });
  $('#gStage').addEventListener('touchend', e => {
    if (gX0 === null) return;
    const d = e.changedTouches[0].clientX - gX0;
    if (Math.abs(d) > 50) gGo(gIdx + (d < 0 ? 1 : -1));
    gX0 = null;
  });
  $('#npGallery').onclick = () => current && openGallery(current);

  /* ---------------------------------------------------- 錯誤紀錄 */
  let errItems = [];
  async function loadErrors() {
    let r;
    try { r = await api('/api/errors?limit=500'); } catch (e) { toast(e.message); return; }
    errItems = r.items;
    const sel = $('#errCat'), cur = sel.value;
    const cats = [...new Set([...r.categories, ...r.items.map(x => x.category)])];
    sel.innerHTML = '<option value="">全部類別</option>' + cats.map(c => {
      const n = r.items.filter(x => x.category === c).length;
      return `<option value="${esc(c)}">${esc(c)}${n ? `（${n}）` : ''}</option>`;
    }).join('');
    sel.value = cur;
    $('#errInfo').textContent = r.size ? `紀錄檔：${r.path}（${(r.size / 1024).toFixed(1)} KB），顯示最新 ${r.items.length} 筆` : '';
    if (r.items[0]) store.set('errSeen', r.items[0].time);
    updateErrBadge(r.items);
    renderErrors();
  }
  function renderErrors() {
    const cat = $('#errCat').value, lv = $('#errLevel').value, q = $('#errSearch').value.trim().toLowerCase();
    const list = errItems.filter(x => (!cat || x.category === cat) && (!lv || x.level === lv) &&
      (!q || x.message.toLowerCase().includes(q)));
    $('#errEmpty').hidden = list.length > 0;
    $('#errEmpty').textContent = errItems.length ? '沒有符合條件的紀錄' : '沒有錯誤紀錄';
    $('#errList').innerHTML = list.slice(0, 300).map(x => {
      const [first, ...rest] = x.message.split('\n');
      return `<details class="err-item ${x.level === '錯誤' ? 'lv-err' : 'lv-warn'}">
        <summary><span class="err-lv">${esc(x.level)}</span><span class="err-cat">${esc(x.category)}</span>
          <span class="err-time">${esc(x.time)}</span><span class="err-msg">${esc(first)}</span></summary>
        ${rest.length ? `<pre>${esc(rest.join('\n'))}</pre>` : ''}</details>`;
    }).join('');
  }
  function updateErrBadge(items) {
    const seen = store.get('errSeen', '');
    const n = items.filter(x => x.level === '錯誤' && x.time > seen).length;
    $('#errBadge').hidden = !n; $('#errBadge').textContent = n > 99 ? '99+' : n;
  }
  $('#errCat').onchange = renderErrors; $('#errLevel').onchange = renderErrors;
  $('#errSearch').oninput = renderErrors;
  $('#errRefresh').onclick = loadErrors;
  $('#errOpen').onclick = () => api('/api/errors/open', { method: 'POST' }).then(r => !r.ok && toast(r.error));
  $('#errClear').onclick = async () => {
    if (!confirm('確定清除所有錯誤紀錄？')) return;
    await api('/api/errors/clear', { method: 'POST' }); loadErrors();
  };
  const pollErrBadge = () => api('/api/errors?limit=200&level=錯誤').then(r => {
    if (store.get('view') === 'errors') return;
    updateErrBadge(r.items);
  }).catch(() => {});
  pollErrBadge(); setInterval(pollErrBadge, 30000);

  /* ---------------------------------------------------- 啟動 */
  const initial = (location.hash || '').slice(1);
  show(titles[initial] ? initial : store.get('view', 'queue'));
})();
