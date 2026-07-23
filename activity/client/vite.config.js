import { defineConfig } from "vite";

// https://vitejs.dev/config/
export default defineConfig({
  envDir: "../",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:3001",
        changeOrigin: true,
        secure: false,
        ws: true,
      },
    },
    // Useful when tunneling (cloudflared / ngrok) into Discord Activity URL Mapping
    hmr: {
      clientPort: 443,
    },
  },
});
