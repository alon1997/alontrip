import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base must be '/trip/': deployed under the alonuniverse.com/trip subpath
export default defineConfig({
  base: '/trip/',
  plugins: [vue()],
  // dev-only convenience: local `npm run dev` proxies API + agent calls to the
  // FastAPI backend on :5003. Production is same-origin behind Nginx.
  server: {
    proxy: {
      '/api/trip': 'http://127.0.0.1:5003',
    },
  },
})
