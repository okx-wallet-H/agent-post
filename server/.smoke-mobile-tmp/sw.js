/* AgentPost 群聊 Service Worker（#36）：缓存壳；离线时页面提示「离线，恢复后自动同步」 */
var CACHE = 'agentpost-chat-v1';
var SHELL = ['./chat'];
self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () {
    return self.skipWaiting();
  }));
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) {
      return caches.delete(k);
    }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') { return; }
  var url = new URL(e.request.url);
  if (url.origin !== location.origin) { return; }          // 数据接口（api/v1 等）不缓存
  if (url.pathname.indexOf('/chat') !== 0 && url.pathname !== '/chat') { return; }
  e.respondWith(
    fetch(e.request).then(function (res) {
      var copy = res.clone();
      caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
      return res;
    }).catch(function () {
      return caches.match(e.request).then(function (hit) { return hit || caches.match('./chat'); });
    })
  );
});
self.addEventListener('message', function (e) {
  if (e.data === 'SKIP_WAITING') { self.skipWaiting(); }
});
