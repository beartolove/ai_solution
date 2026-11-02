// 逐字稿登入同步：設定與共用函式（背景程式與小視窗共用）
export const SERVER = "http://127.0.0.1:8766";

// 網站群組：cookies 要抓的網域；def＝預設勾選；optional＝勾選時才向使用者要權限
export const GROUPS = {
  youtube:   { label: "YouTube（含 Google 登入）", domains: ["youtube.com", "google.com"], def: true },
  facebook:  { label: "Facebook", domains: ["facebook.com"], def: true },
  instagram: { label: "Instagram", domains: ["instagram.com"], def: true },
  threads:   { label: "Threads", domains: ["threads.net", "threads.com"], def: true },
  x:         { label: "X（Twitter）", domains: ["x.com", "twitter.com"], def: false, optional: ["*://*.x.com/*", "*://*.twitter.com/*"] },
  bilibili:  { label: "Bilibili", domains: ["bilibili.com"], def: false, optional: ["*://*.bilibili.com/*"] },
};

export async function getEnabled() {
  const { enabled } = await chrome.storage.local.get("enabled");
  if (enabled) return enabled;
  return Object.fromEntries(Object.entries(GROUPS).map(([k, g]) => [k, !!g.def]));
}

async function api(path, opt = {}) {
  let r;
  try {
    r = await fetch(SERVER + path, opt);
  } catch {
    throw new Error("轉逐字稿程式沒有開啟（請雙擊 start.cmd）");
  }
  let data = {};
  try { data = await r.json(); } catch { }
  if (!r.ok) throw new Error(data.detail || `程式回應 ${r.status}`);
  return data;
}

export const ping = () => api("/api/ping");

// 讀取勾選網站的 cookies，送到本機程式（只送到 127.0.0.1，不經過網路）
export async function syncCookies(source = "manual") {
  const enabled = await getEnabled();
  const cookies = [];
  const seen = new Set();
  for (const [k, g] of Object.entries(GROUPS)) {
    if (!enabled[k]) continue;
    if (g.optional && !(await chrome.permissions.contains({ origins: g.optional }))) continue;
    for (const d of g.domains) {
      for (const c of await chrome.cookies.getAll({ domain: d })) {
        const key = `${c.domain}|${c.path}|${c.name}|${c.storeId}`;
        if (seen.has(key)) continue;
        seen.add(key);
        cookies.push({ domain: c.domain, hostOnly: c.hostOnly, path: c.path, secure: c.secure, session: c.session,
                       expirationDate: c.expirationDate, name: c.name, value: c.value });
      }
    }
  }
  const res = await api("/api/cookies", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookies, source }),
  });
  return res;
}

export const enqueue = urls => api("/api/enqueue", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ urls }),
});

// 同步結果記錄（小視窗顯示）；失敗時在圖示上顯示紅色「!」
export async function recordSync(ok, info) {
  const last = { time: new Date().toLocaleString("zh-TW", { hour12: false }), ok, ...info };
  await chrome.storage.local.set({ last });
  await chrome.action.setBadgeText({ text: ok ? "" : "!" });
  if (!ok) await chrome.action.setBadgeBackgroundColor({ color: "#B3261E" });
  return last;
}
