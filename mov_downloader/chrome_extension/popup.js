const $ = id => document.getElementById(id);
const send = msg => new Promise(r => chrome.runtime.sendMessage(msg, r));
const say = (t, ok = true) => { $('msg').textContent = t; $('msg').style.color = ok ? '#16a34a' : '#dc2626'; };

async function refresh() {
  const { lastSync, lastCount, autoSync = true, lastPrivate } = await chrome.storage.local.get(['lastSync', 'lastCount', 'autoSync', 'lastPrivate']);
  $('lastPriv').textContent = lastPrivate ? new Date(lastPrivate).toLocaleString('zh-TW') : '尚未使用';
  $('last').textContent = lastSync ? `${new Date(lastSync).toLocaleString('zh-TW')}（${lastCount} 筆）` : '尚未同步';
  $('auto').checked = autoSync;
  const p = await send({ type: 'ping' });
  $('srv').textContent = p?.ok ? '執行中' : '未啟動（請執行 start.bat）';
  $('srv').className = p?.ok ? 'ok' : 'bad';
}
$('sync').onclick = async () => {
  say('同步中…');
  const r = await send({ type: 'sync' });
  r?.ok ? say(`已同步 ${r.count} 筆 Cookie`) : say('同步失敗：' + (r?.error || '下載器未啟動'), false);
  refresh();
};
$('syncPrivate').onclick = async () => {
  say('同步中…');
  const r = await send({ type: 'syncPrivate' });
  r?.ok ? say(`已同步無痕視窗的 YouTube 登入（${r.count} 筆），現在可以直接關閉無痕視窗`) : say(r?.error || '下載器未啟動', false);
  refresh();
};
$('add').onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url?.startsWith('http')) return say('此頁面無法下載', false);
  const r = await send({ type: 'enqueue', urls: [tab.url] });
  r?.ok ? say('已加入下載佇列 ✓') : say('加入失敗：' + (r?.error || '下載器未啟動'), false);
};
$('auto').onchange = e => chrome.storage.local.set({ autoSync: e.target.checked });
refresh();
