/* Service worker for Agent Capital (built by vite-plugin-pwa, injectManifest).
 *
 * Rules:
 *  - /api/**  : NOT intercepted at all. No route matches, so the browser talks to
 *               the backend directly — the SSE terminal streams must never pass
 *               through (or be cached by) the worker.
 *  - navigation (page loads): NetworkFirst, so a rebuilt frontend is picked up on
 *               the next load; the cached copy is only used when offline.
 *  - built assets (hashed js/css, icons, manifest): precached, revision-tracked.
 *  - skipWaiting + clientsClaim: a new build's worker takes over immediately
 *               instead of waiting for every tab to close (the stale-tab problem).
 *  - push       : shows the "<project> needs your response" alert the backend
 *               sends (server.py notify_phone); tapping it opens that project.
 */
import { clientsClaim } from 'workbox-core'
import { cleanupOutdatedCaches, precacheAndRoute } from 'workbox-precaching'
import { registerRoute } from 'workbox-routing'
import { NetworkFirst } from 'workbox-strategies'

self.skipWaiting()
clientsClaim()

cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST)

const isApi = (url) => url.pathname.startsWith('/api/')

registerRoute(
  ({ request, url }) => request.mode === 'navigate' && !isApi(url),
  new NetworkFirst({
    cacheName: 'pages',
    networkTimeoutSeconds: 5,
    // Don't cache the login redirect as if it were the app page.
    plugins: [{ cacheWillUpdate: async ({ response }) => (response && response.status === 200 && !response.redirected ? response : null) }],
  }),
)

// ── Phone notifications ──────────────────────────────────────────────────────
// Payload comes from backend push_payload(): { title, body, tag, url }.
// iOS requires every push event to show a notification, so this never skips.
self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = { body: event.data ? event.data.text() : '' }
  }
  const title = data.title || 'Agent Capital'
  const options = {
    body: data.body || '',
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    tag: data.tag || 'agent-capital',
    renotify: true,
    data: { url: data.url || '/' },
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = (event.notification.data && event.notification.data.url) || '/'
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (list) => {
      const client = list.find((c) => 'focus' in c)
      if (!client) return self.clients.openWindow(url)
      const focused = await client.focus()
      // Tell the running app to route there (App.jsx listens); WindowClient.navigate
      // would reload the whole app and isn't available everywhere.
      focused.postMessage({ type: 'navigate', url })
      return focused
    }),
  )
})
