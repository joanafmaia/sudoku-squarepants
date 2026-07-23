import { defineConfig } from "vite";

function externalCdnPlugin() {
  return {
    name: "thcoku-external-cdn",
    enforce: "pre",
    resolveId(id) {
      if (
        id.startsWith("/pyscript/") ||
        id.startsWith("/jsdelivr/") ||
        id.includes("pyscript.net") ||
        id.includes("cdn.jsdelivr.net")
      ) {
        return { id, external: true };
      }
      return null;
    },
  };
}

export default defineConfig({
  envDir: "../",
  plugins: [externalCdnPlugin()],
  build: {
    rollupOptions: {
      external: [/\/pyscript\//, /\/jsdelivr\//, /pyscript\.net/, /cdn\.jsdelivr\.net/],
    },
  },
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
