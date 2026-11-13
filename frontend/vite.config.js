import { defineConfig } from "vite";
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      // The landing page is plain HTML so it loads fast and search engines can read it.
      // The app is app.html, which Flask serves under /app.
      input: {
        landing: resolve(import.meta.dirname, "index.html"),
        app: resolve(import.meta.dirname, "app.html"),
      },
    },
  },
  server: {
    host: "0.0.0.0", // Listen on the local network
    port: 5175,
    proxy: {
      "/api": "http://localhost:5001", // Flask backend during development
    },
  },
});
