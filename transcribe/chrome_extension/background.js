// 逐字稿登入同步：背景程式（每 30 分鐘自動同步、Chrome 啟動時同步、右鍵選單）
import { syncCookies, recordSync, enqueue } from "./common.js";

async function doSync(source) {
  try {
    const r = await syncCookies(source);
    return await recordSync(true, { count: r.count, domains: r.domains });
  } catch (e) {
    return await recordSync(false, { error: e.message });
  }
}

function setup() {
  chrome.alarms.create("sync", { periodInMinutes: 30 });
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "tx-page", title: "轉逐字稿：這個頁面的影片", contexts: ["page", "video"],
                                 documentUrlPatterns: ["http://*/*", "https://*/*"] });
    chrome.contextMenus.create({ id: "tx-link", title: "轉逐字稿：這個連結", contexts: ["link"],
                                 targetUrlPatterns: ["http://*/*", "https://*/*"] });
  });
}

chrome.runtime.onInstalled.addListener(() => { setup(); doSync("install"); });
chrome.runtime.onStartup.addListener(() => { setup(); doSync("startup"); });
chrome.alarms.onAlarm.addListener(a => { if (a.name === "sync") doSync("auto"); });

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png", title, message });
}

chrome.contextMenus.onClicked.addListener(async (info) => {
  const url = info.menuItemId === "tx-link" ? info.linkUrl : info.pageUrl;
  if (!url) return;
  await doSync("menu");  // 先同步，確保登入資料是最新的
  try {
    const r = await enqueue([url]);
    const x = r.results?.[0];
    if (x?.ok) notify("已加入轉逐字稿", x.name);
    else notify("無法加入轉逐字稿", x?.error || "未知錯誤");
  } catch (e) {
    notify("無法加入轉逐字稿", e.message);
  }
});
