'use strict';

self.addEventListener('push', function (event) {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = { title: 'Home Automation', body: event.data ? event.data.text() : '' };
  }
  const title = payload.title || 'Home Automation';
  const options = {
    body: payload.body || '',
    icon: '/static/icon-180.png',
    badge: '/static/icon-180.png',
    data: { url: payload.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : '/';
  event.waitUntil(clients.openWindow(url));
});
