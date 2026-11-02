// 影片下載器同步 — 背景服務
const SERVER = 'http://127.0.0.1:8765';
const DOMAINS = [
  'youtube.com', 'google.com', 'facebook.com', 'fb.watch', 'instagram.com',
  'bilibili.com', 'b23.tv', 'x.com', 'twitter.com', 'tiktok.com',
  'threads.net', 'threads.com', 'vimeo.com',
];

async function collectCookies(domains = DOMAINS, storeId) {
  const all = [];
  for (const domain of domains) {
    try { all.push(...await chrome.cookies.getAll(storeId ? { domain, storeId } : { domain })); } catch (e) { /* 忽略 */ }
  }
  // 去除重複
  const seen = new Set();
  return all.filter(c => {
    const k = `${c.domain}|${c.path}|${c.name}`;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
}

async function syncCookies() {
  const cookies = await collectCookies();
  const r = await fetch(`${SERVER}/api/cookies`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookies }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'HTTP ' + r.status);
  await chrome.storage.local.set({ lastSync: Date.now(), lastCount: data.count });
  return data.count;
}

// 從無痕視窗同步 YouTube 登入：無痕視窗關閉後，這份 Cookie 不會再被 YouTube 輪替而失效
async function syncPrivate() {
  if (!(await chrome.extension.isAllowedIncognitoAccess())) {
    throw new Error('請先到 chrome://extensions →「影片下載器同步」→「詳細資料」，開啟「允許在無痕模式下執行」');
  }
  const stores = await chrome.cookies.getAllCookieStores();
  const incog = stores.find(s => s.id !== '0');
  if (!incog) throw new Error('找不到無痕視窗：請先開一個無痕視窗（Ctrl+Shift+N），登入 YouTube 會員帳號後再按');
  const cookies = await collectCookies(['youtube.com', 'google.com'], incog.id);
  const r = await fetch(`${SERVER}/api/cookies`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookies, source: 'private' }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'HTTP ' + r.status);
  await chrome.storage.local.set({ lastPrivate: Date.now() });
  return data.count;
}

async function enqueue(urls) {
  const r = await fetch(`${SERVER}/api/enqueue`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'HTTP ' + r.status);
  return data.added?.length || 0;
}

function badge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(() => chrome.action.setBadgeText({ text: '' }), 3000);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create('autoSync', { periodInMinutes: 30 });
  chrome.contextMenus.create({ id: 'dlPage', title: '用影片下載器下載此頁影片', contexts: ['page'] });
  chrome.contextMenus.create({ id: 'dlLink', title: '用影片下載器下載此連結', contexts: ['link'] });
  syncCookies().catch(() => {});
});
chrome.runtime.onStartup.addListener(() => syncCookies().catch(() => {}));

chrome.alarms.onAlarm.addListener(async a => {
  if (a.name !== 'autoSync') return;
  const { autoSync = true } = await chrome.storage.local.get('autoSync');
  if (autoSync) syncCookies().catch(() => {});
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.menuItemId === 'dlLink' ? info.linkUrl : (info.pageUrl || tab?.url);
  try {
    await syncCookies().catch(() => {});
    await enqueue([url]);
    badge('+1', '#16a34a');
  } catch (e) {
    badge('!', '#dc2626');
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    try {
      if (msg.type === 'sync') reply({ ok: true, count: await syncCookies() });
      else if (msg.type === 'syncPrivate') reply({ ok: true, count: await syncPrivate() });
      else if (msg.type === 'enqueue') { await syncCookies().catch(() => {}); reply({ ok: true, added: await enqueue(msg.urls) }); }
      else if (msg.type === 'ping') {
        const r = await fetch(`${SERVER}/api/ping`); reply({ ok: r.ok });
      }
    } catch (e) { reply({ ok: false, error: String(e.message || e) }); }
  })();
  return true;
});
