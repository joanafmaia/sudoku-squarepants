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
      // Local stand-ins for Discord Activity URL Mappings
      "/pyscript": {
        target: "https://pyscript.net",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/pyscript/, ""),
      },
      "/jsdelivr": {
        target: "https://cdn.jsdelivr.net",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/jsdelivr/, ""),
      },
      "/gfonts": {
        target: "https://fonts.googleapis.com",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/gfonts/, ""),
      },
      "/gstatic": {
        target: "https://fonts.gstatic.com",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/gstatic/, ""),
      },
    },
    // Useful when tunneling (cloudflared / ngrok) into Discord Activity URL Mapping
    hmr: {
      clientPort: 443,
    },
  },
});
