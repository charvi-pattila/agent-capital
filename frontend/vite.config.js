import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // listen on LAN so the phone can reach the dev server too
    proxy: {
      '/api': 'http://127.0.0.1:8888',
    },
  },
})
