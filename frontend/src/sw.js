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
  new NetworkFirst({ cacheName: 'pages', networkTimeoutSeconds: 5 }),
)
