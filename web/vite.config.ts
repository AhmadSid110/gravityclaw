import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/auth": "http://127.0.0.1:8787",
      "/health": "http://127.0.0.1:8787",
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
      "/conversations": "http://127.0.0.1:8787",
      "/runs": "http://127.0.0.1:8787",
      "/memories": "http://127.0.0.1:8787",
    },
  },
});
