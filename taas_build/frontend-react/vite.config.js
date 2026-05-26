import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Builds to ../server/static so the Python backend can serve the React app.
// During dev, proxies API calls to the FastAPI server on :8080.
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../server/static', emptyOutDir: true },
  server: {
    proxy: {
      '/run': 'http://localhost:8080',
      '/ai': 'http://localhost:8080',
      '/runs': 'http://localhost:8080',
      '/health': 'http://localhost:8080',
      '/templates': 'http://localhost:8080',
      '/upload': 'http://localhost:8080',
    },
  },
})
