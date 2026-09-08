import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    // Service worker so the app can be installed ("Add to Home Screen" on iOS,
    // "Add to Dock" in Safari on macOS). The manifest is a static file in
    // public/ and is linked from index.html, so the plugin doesn't generate one.
    // The worker itself is src/sw.js (injectManifest) so we control exactly what
    // it intercepts: /api/ (including the SSE terminal streams) is never touched.
    VitePWA({
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.js',
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      manifest: false,
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,png,svg,webmanifest}'],
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024, // exceljs/xlsx chunks are ~1MB
      },
    }),
  ],
  server: {
    host: true, // listen on LAN so the phone can reach the dev server too
    proxy: {
      '/api': 'http://127.0.0.1:8888',
    },
  },
})
