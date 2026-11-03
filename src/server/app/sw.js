// wolbot app shell: open instantly from the home screen; /api always goes to the server.
const CACHE = "wolbot-v1";
const SHELL = ["/", "/app.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  // network first so updates show at once; the cache only helps when the network is down
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request)));
});

// An alert from the server. The phone plays its own notification sound: the web can't choose another
// one or sound through silent mode.
self.addEventListener("push", e => {
  let m = {};
  try { m = e.data.json(); } catch (x) { m = {body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(m.title || "wolbot", {
    body: m.body || "", icon: "/icon-192.png", badge: "/icon-192.png", dir: "auto", tag: m.tag || "wolbot", renotify: true,
    silent: false, requireInteraction: !!m.urgent, vibrate: [300, 150, 300, 150, 600], data: {url: "/"}}));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(l => {
    for (const c of l) if ("focus" in c) return c.focus();
    return clients.openWindow("/");
  }));
});
