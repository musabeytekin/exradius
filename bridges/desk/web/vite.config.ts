/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The Desk service (uv run exradius-desk), for /api and /auth on the same origin.
const desk = process.env.DESK_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": { target: desk, changeOrigin: false },
      "/auth": { target: desk, changeOrigin: false },
    },
  },
  build: {
    // The service serves the build from its package (Dockerfile).
    outDir: "dist",
    chunkSizeWarningLimit: 600,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
