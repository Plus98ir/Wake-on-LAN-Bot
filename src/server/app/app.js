// wolbot phone app: log in with a passkey, then every protected button asks for the fingerprint again.
"use strict";

const T = {
  fa: {
    lang: "English", install: "نصب اپ", logout: "خروج",
    loginT: "ورود با اثر انگشت", loginD: "روی دایره بزن و انگشتت را روی حسگر بگذار.",
    hello: n => `سلام ${n}`, left: m => `ورود تا ${m} دقیقه دیگر`,
    locked: "🛑 کیل‌سوییچ روشن است: همه‌ی دستورها بسته‌اند. برگشت: روی کامپیوتر wol-unkill.ps1 و روی سرور wolbot unlock.",
    pair: "➕ افزودن کامپیوتر", kill: "🛑 کیل‌سوییچ", events: "رویدادها و هشدارها",
    online: "روشن", offline: "خاموش", noagent: "فقط روشن کردن (بدون Agent)", none: "هنوز کامپیوتری نیست یا به تو دسترسی داده نشده.",
    apps: "برنامه‌ها", yes: "بله", no: "نه", ok: "باشه", fp: "اثر انگشت...", wait: "در حال اجرا...",
    rescue: n => `🆘 ${n} می‌خواهد بعد از کیل‌سوییچ باز شود (رمز بازیابی پشت کامپیوتر زده شد)`, rescueBtn: "🔓 باز کردن قفل (اثر انگشت)",
    inc: "تغییر روی کامپیوتر - صفحه قفل شد", approve: "✅ تأیید (اثر انگشت)", deny: "⛔ رد", killIn: "بی‌جواب = کیل‌سوییچ",
    gate: "🧱 خود کامپیوتر تأیید می‌کند",
    kinds: {admins: "ادمین‌ها تغییر کردند", users: "کاربر جدید یا تغییر کاربر", firewall: "فایروال تغییر کرد", uac: "UAC تغییر کرد",
            rdp: "ریموت دسکتاپ تغییر کرد", keys: "کلیدهای SSH یا تنظیمات sshd تغییر کرد", trusted: "Passkeyهای مورد اعتماد تغییر کرد",
            remote: "یک ابزار ریموت اجرا شد", failed: "تلاش ناموفق ورود به ویندوز", services: "سرویس جدید یا حذف‌شده", tasks: "تسک زمان‌بندی‌شده‌ی جدید"},
    sure: {R: "ری‌استارت شود؟", H: "خاموش شود؟", D: "از لیست حذف شود؟", dn: "این تغییر رد شود؟ کیل‌سوییچ این کامپیوتر زده می‌شود.", x: "کیل‌سوییچ: همه‌ی کامپیوترها قفل و از شبکه جدا می‌شوند و همه‌ی دستورها بسته می‌شوند. مطمئنی؟"},
    cancel: "لغو شد", noPk: "مرورگر Passkey ندارد. با Chrome یا Safari باز کن.", out: "زمان ورود تمام شد، دوباره وارد شو.",
    b: {w: "⚡ روشن کردن", s: "🔄 تازه‌سازی", c: "🤖 Claude Remote", k: "⏹ توقف Claude", l: "🔒 قفل", u: "👤 چه کسی؟",
        R: "🔁 ری‌استارت", H: "⏻ خاموش", v: "💻 VS Code", y: "📡 رله", D: "🗑 حذف"}
  },
  en: {
    lang: "فارسی", install: "Install app", logout: "Log out",
    loginT: "Log in with your fingerprint", loginD: "Tap the circle and touch the sensor.",
    hello: n => `Hi ${n}`, left: m => `Logged in for ${m} more min`,
    locked: "🛑 Kill switch is ON: every action is blocked. Undo: wol-unkill.ps1 on the PC, then wolbot unlock on the server.",
    pair: "➕ Add PC", kill: "🛑 Kill switch", events: "Events and alerts",
    online: "online", offline: "offline", noagent: "wake only (no agent)", none: "No machines yet, or none given to you.",
    apps: "Apps", yes: "Yes", no: "No", ok: "OK", fp: "Fingerprint...", wait: "Working...",
    rescue: n => `🆘 ${n} asks to unlock after the kill switch (recovery password typed at the PC)`, rescueBtn: "🔓 Unlock (fingerprint)",
    inc: "Change on the PC - screen locked", approve: "✅ Approve (fingerprint)", deny: "⛔ Deny", killIn: "no answer = kill switch",
    gate: "🧱 the PC approves itself",
    kinds: {admins: "Administrators changed", users: "New or changed user", firewall: "Firewall changed", uac: "UAC changed",
            rdp: "Remote Desktop changed", keys: "SSH keys or sshd settings changed", trusted: "Trusted passkeys changed",
            remote: "A remote-control tool is running", failed: "Failed Windows logon", services: "Service added or removed", tasks: "New scheduled task"},
    sure: {R: "Restart it?", H: "Shut it down?", D: "Remove it from the list?", dn: "Deny this change? This PC's kill switch starts.", x: "Kill switch: every PC locks and goes offline and every action is blocked. Sure?"},
    cancel: "Cancelled", noPk: "This browser has no passkeys. Open it in Chrome or Safari.", out: "Your login ended, log in again.",
    b: {w: "⚡ Wake", s: "🔄 Refresh", c: "🤖 Claude Remote", k: "⏹ Stop Claude", l: "🔒 Lock", u: "👤 Who",
        R: "🔁 Restart", H: "⏻ Shut down", v: "💻 VS Code", y: "📡 Relay", D: "🗑 Remove"}
  }
};
let L = "fa";
try { L = localStorage.getItem("lang") || (navigator.language.startsWith("fa") ? "fa" : "en"); } catch (e) {}
const $ = id => document.getElementById(id);
let state = null, timer = null, installEvt = null;
const notes = {};          // machine index -> last result text

// ---------- helpers ----------
const enc = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const dec = s => { s = s.replace(/-/g, "+").replace(/_/g, "/"); return Uint8Array.from(atob(s + "=".repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0)); };
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const link = s => esc(s).replace(/https:\/\/[^\s<]+/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {credentials: "same-origin"} : {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Wolbot": "1"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { showLogin(); throw new Error(T[L].out); }
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

async function fingerprint(challenge, rpId, allow) {
  if (!window.PublicKeyCredential) throw new Error(T[L].noPk);
  const pk = {challenge: dec(challenge), rpId, userVerification: "required", timeout: 60000};
  if (allow) pk.allowCredentials = allow.map(id => ({type: "public-key", id: dec(id)}));
  let c;
  try { c = await navigator.credentials.get({publicKey: pk}); }
  catch (e) { throw new Error(e.name === "NotAllowedError" ? T[L].cancel : e.message); }
  return {id: c.id, clientDataJSON: enc(c.response.clientDataJSON),
          authenticatorData: enc(c.response.authenticatorData), signature: enc(c.response.signature)};
}

function toast(text, err) {
  const t = $("toast");
  t.textContent = text; t.className = err ? "err" : "";
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hide"), 3500);
}

function modal(text, buttons) {
  return new Promise(res => {
    $("mtxt").innerHTML = link(text);
    $("mrow").innerHTML = "";
    buttons.forEach(([label, val, cls]) => {
      const b = document.createElement("button");
      b.className = cls || "sec2"; b.textContent = label;
      b.onclick = () => { $("modal").classList.add("hide"); res(val); };
      $("mrow").appendChild(b);
    });
    $("modal").classList.remove("hide");
  });
}

// ---------- views ----------
function paintStatic() {
  const t = T[L];
  document.documentElement.lang = L; document.documentElement.dir = L === "fa" ? "rtl" : "ltr";
  document.querySelectorAll("[data-t]").forEach(e => e.textContent = t[e.dataset.t]);
  $("lang").textContent = t.lang; $("install").textContent = t.install; $("logout").textContent = t.logout;
}

function showLogin() {
  state = null; clearInterval(timer); timer = null;
  $("app").classList.add("hide"); $("logout").classList.add("hide"); $("who").textContent = "";
  $("login").classList.remove("hide");
}

function render() {
  const t = T[L], s = state;
  $("login").classList.add("hide"); $("app").classList.remove("hide"); $("logout").classList.remove("hide");
  $("hello").textContent = t.hello(s.name);
  $("who").textContent = t.left(Math.max(0, Math.ceil(s.left / 60)));
  $("locked").classList.toggle("hide", !s.locked);
  $("pair").classList.toggle("hide", !s.can_pair);
  $("kill").classList.toggle("hide", !s.can_kill);
  const box = $("machines");
  box.innerHTML = s.machines.length ? "" : `<div class="empty">${esc(t.none)}</div>`;
  for (const r of s.rescue || []) {
    const c = document.createElement("div");
    c.className = "card";
    c.innerHTML = `<div class="inc"><b>${esc(t.rescue(r.name))}</b><small>${new Date(r.ts * 1000).toLocaleTimeString(L === "fa" ? "fa-IR" : "en-GB")}</small>
      <div class="kb"><button class="wide" data-op="rs" data-i="${r.i}">${esc(t.rescueBtn)}</button></div></div>`;
    box.appendChild(c);
  }
  for (const m of s.machines) {
    const st = m.online === null ? `<span class="dot"></span>${esc(t.noagent)}`
      : m.online ? `<span class="dot on"></span>${esc(t.online)}` : `<span class="dot off"></span>${esc(t.offline)}`;
    const lk = op => s.protect.includes(op) ? '<span class="lk">🔐</span>' : "";
    const b = (op, label, extra = "", j) =>
      `<button data-op="${op}" data-i="${m.i}"${j !== undefined ? ` data-j="${j}"` : ""} class="${extra}">${lk(op)}${esc(label)}</button>`;
    let kb = "";
    const order = m.btns.filter(o => o !== "a");
    if (order.includes("w")) kb += b("w", t.b.w) + b("s", t.b.s);
    else kb += b("s", t.b.s, "wide");
    if (m.apps.length) {
      kb += `<div class="sec">${esc(t.apps)}</div>`;
      m.apps.forEach((n, j) => kb += b("a", "▶️ " + n, m.apps.length % 2 && j === m.apps.length - 1 ? "wide" : "", j));
    }
    const rest = order.filter(o => o !== "w");
    rest.forEach((o, k) => kb += b(o, t.b[o], rest.length % 2 && k === rest.length - 1 ? "wide" : ""));
    let inc = "";
    (m.incidents || []).forEach((p, j) => {
      inc += `<div class="inc"><b>⚠️ ${esc(t.kinds[p.kind] || p.kind)}</b>
        <small>${esc(t.inc)} · ${new Date(p.ts * 1000).toLocaleTimeString(L === "fa" ? "fa-IR" : "en-GB")}${p.kill ? " · " + esc(t.killIn) : ""}</small>
        <div class="kb"><button data-op="ap" data-i="${m.i}" data-j="${j}">${esc(t.approve)}</button>
        <button data-op="dn" data-i="${m.i}" data-j="${j}">${esc(t.deny)}</button></div></div>`;
    });
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `${inc}<div class="status"><div class="nm">🖥 ${esc(m.name)}</div><div class="mac">${esc(m.mac)}${m.gate ? " · " + esc(t.gate) : ""}</div>
      <div>${st}</div>${notes[m.i] ? `<div class="note">${link(notes[m.i])}</div>` : ""}</div><div class="kb">${kb}</div>`;
    box.appendChild(card);
  }
  const ev = $("events");
  ev.classList.toggle("hide", !s.admin);
  if (s.admin) {
    $("evlist").innerHTML = (s.events || []).map(e =>
      `<div class="ev"><time>${new Date(e.t * 1000).toLocaleString(L === "fa" ? "fa-IR" : "en-GB", {dateStyle: "short", timeStyle: "short"})}</time><span>${esc(e.text)}</span></div>`).join("");
  }
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
    if (!timer) timer = setInterval(() => { if (!document.hidden) refresh(); }, 5000);
  } catch (e) { /* 401 already showed the login */ }
}

// ---------- actions ----------
async function login() {
  $("fp").disabled = true;
  try {
    const b = await api("/api/login/begin", {});
    const cred = await fingerprint(b.challenge, b.rpId, null);
    await api("/api/login/finish", {id: b.id, cred});
    await refresh();
  } catch (e) { toast(e.message, true); }
  $("fp").disabled = false;
}

async function press(op, i, j, el) {
  const t = T[L];
  if (t.sure[op]) {
    const name = i !== undefined && state ? (state.machines.find(m => m.i === i) || {}).name : "";
    if (!await modal((name ? name + "\n\n" : "") + t.sure[op], [[t.no, false], [t.yes, true, op === "x" || op === "D" ? "pri red" : "pri"]])) return;
  }
  if (el) el.classList.add("busy");
  try {
    let r = await api("/api/begin", {op, i, j});
    if (r.need === "fp") {
      toast(t.fp);
      const cred = await fingerprint(r.challenge, r.rpId, r.allow);
      toast(t.wait);
      r = await api("/api/do", {id: r.id, cred});
    }
    if (r.note) {
      if (["p", "y", "v", "u"].includes(op) || i === undefined) await modal(r.note, [[t.ok, true, "pri"]]);
      else notes[i] = r.note;
    }
    if (op === "c") claudeWatch(i);
    if (op === "D") delete notes[i];
  } catch (e) { toast(e.message, true); }
  if (el) el.classList.remove("busy");
  refresh();
}

async function claudeWatch(i) {  // Claude prints its link a few seconds after it starts
  for (let n = 0; n < 10; n++) {
    await new Promise(r => setTimeout(r, 4000));
    try {
      const r = await api("/api/begin", {op: "cs", i});
      notes[i] = r.note; if (state) render();
      if (/https:\/\/claude\.ai|running|error|not/i.test(r.note)) return;
    } catch (e) { return; }
  }
}

// ---------- wiring ----------
$("fp").onclick = login;
$("lang").onclick = () => { L = L === "fa" ? "en" : "fa"; try { localStorage.setItem("lang", L); } catch (e) {} paintStatic(); if (state) render(); };
$("logout").onclick = async () => { try { await api("/api/logout", {}); } catch (e) {} showLogin(); };
$("pair").onclick = e => press("p", undefined, undefined, e.target);
$("kill").onclick = e => press("x", undefined, undefined, e.target);
$("machines").onclick = e => {
  const b = e.target.closest("button[data-op]");
  if (!b) return;
  const op = b.dataset.op, i = +b.dataset.i, j = b.dataset.j !== undefined ? +b.dataset.j : undefined;
  press(op, i, j, b);
};
document.addEventListener("visibilitychange", () => { if (!document.hidden && state) refresh(); });
window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); installEvt = e; $("install").classList.remove("hide"); });
$("install").onclick = async () => { if (!installEvt) return; installEvt.prompt(); await installEvt.userChoice; installEvt = null; $("install").classList.add("hide"); };
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

paintStatic();
refresh();
