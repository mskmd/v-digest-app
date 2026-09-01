// Minimal service worker — required by Chrome to show the "Install app" prompt.
// Deliberately does NOT cache API responses (digest data must always be fresh),
// just passes requests straight through.
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => self.clients.claim());
self.addEventListener('fetch', (e) => {
  e.respondWith(fetch(e.request));
});
