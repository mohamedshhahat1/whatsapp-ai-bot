import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The app is served by FastAPI under /dashboard in production, so every asset
// URL has to be built with that prefix.
export default defineConfig({
  plugins: [react()],
  base: "/dashboard/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // `npm run dev` talks to the API running on :8000 without CORS surprises.
    proxy: {
      "/admin": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
})
