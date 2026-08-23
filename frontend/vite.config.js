import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base 必须是 '/trip/'：部署在 alonuniverse.com/trip 子路径下
export default defineConfig({
  base: '/trip/',
  plugins: [vue()],
})
