import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: Number(process.env.POLARIS_WEB_PORT || 5173),
    proxy: {
      '/api': {
        target: process.env.POLARIS_API || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
