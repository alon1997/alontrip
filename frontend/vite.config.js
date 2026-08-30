import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base must be '/trip/': deployed under the alonuniverse.com/trip subpath
export default defineConfig({
  base: '/trip/',
  plugins: [vue()],
})
