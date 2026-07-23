import { defineConfig } from "vite";

const PYSCRIPT_CSS = "/pyscript/releases/2026.7.2/core.css";
const PYSCRIPT_JS = "/pyscript/releases/2026.7.2/core.js";

function injectPyscriptPlugin() {
  return {
    name: "thcoku-inject-pyscript",
    transformIndexHtml(html) {
      // Ensure CDN tags survive the build (Vite may strip unresolved module scripts).
      const tags = [];
      if (!html.includes(PYSCRIPT_CSS)) {
        tags.push({
          tag: "link",
          attrs: { rel: "stylesheet", href: PYSCRIPT_CSS },
          injectTo: "head",
        });
      }
      if (!html.includes(PYSCRIPT_JS)) {
        tags.push({
          tag: "script",
          attrs: { type: "module", src: PYSCRIPT_JS, crossorigin: "" },
          injectTo: "head",
        });
      }
      return { html, tags };
    },
  };
}

export default defineConfig({
  envDir: "../",
  plugins: [injectPyscriptPlugin()],
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
