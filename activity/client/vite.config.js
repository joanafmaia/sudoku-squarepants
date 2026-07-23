import { defineConfig } from "vite";

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
    },
    hmr: {
      clientPort: 443,
    },
  },
});
