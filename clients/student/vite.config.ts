import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/**
 * The API is proxied rather than called cross-origin on purpose.
 *
 * A dev server on :5173 talking directly to FastAPI on :8080 is a cross-origin
 * request, and the usual fix is to add permissive CORS to the backend. That
 * would be a real widening of a service that serves children's schoolwork, made
 * for the convenience of a dev server — and permissive CORS added "just for
 * local" is the kind of thing that ships. Proxying keeps the browser seeing one
 * origin and leaves the backend's posture untouched.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/student': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8080', changeOrigin: true },
    },
  },
})
