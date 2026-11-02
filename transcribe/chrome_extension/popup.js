// 逐字稿登入同步：小視窗
import { GROUPS, SERVER, getEnabled, ping, syncCookies, recordSync } from "./common.js";
const $ = s => document.querySelector(s);

async function showServer() {
  try {
    const r = await ping();
    $("#server").innerHTML = `<span class="ok">執行中</span>（v${r.version}）`;
  } catch (e) {
    $("#server").innerHTML = `<span class="bad">未開啟</span>`;
    $("#err").textContent = e.message; $("#err").hidden = false;
  }
}

async function showLast() {
  const { last } = await chrome.storage.local.get("last");
  if (!last) { $("#last").textContent = "尚未同步"; return; }
  $("#last").innerHTML = last.ok
    ? `<span class="ok">✓</span> ${last.time}<br>${last.count} 筆（${(last.domains || []).join("、")}）`
    : `<span class="bad">✗</span> ${last.time}`;
  if (!last.ok && last.error) { $("#err").textContent = last.error; $("#err").hidden = false; }
}

async function showGroups() {
  const enabled = await getEnabled();
  const box = $("#groups");
  box.innerHTML = "";
  for (const [k, g] of Object.entries(GROUPS)) {
    let on = !!enabled[k];
    if (on && g.optional) on = await chrome.permissions.contains({ origins: g.optional });
    const lab = document.createElement("label");
    lab.className = "grp";
    lab.innerHTML = `<input type="checkbox" ${on ? "checked" : ""}> ${g.label}`;
    lab.querySelector("input").addEventListener("change", async e => {
      let v = e.target.checked;
      if (v && g.optional) v = await chrome.permissions.request({ origins: g.optional });  // 需要你同意才讀取這個網站
      e.target.checked = v;
      const cur = await getEnabled(); cur[k] = v;
      await chrome.storage.local.set({ enabled: cur });
    });
    box.appendChild(lab);
  }
}

$("#sync").addEventListener("click", async () => {
  const b = $("#sync"); b.disabled = true; b.textContent = "同步中…"; $("#err").hidden = true;
  try {
    const r = await syncCookies("manual");
    await recordSync(true, { count: r.count, domains: r.domains });
  } catch (e) {
    await recordSync(false, { error: e.message });
  }
  b.disabled = false; b.textContent = "立即同步";
  showLast(); showServer();
});
$("#open").addEventListener("click", () => chrome.tabs.create({ url: SERVER }));

showServer(); showLast(); showGroups();
