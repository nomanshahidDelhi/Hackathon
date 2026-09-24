import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` proxies the API and the AG-UI stream to the local backend.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8080",
      "/agent": "http://localhost:8080",
    },
  },
});
