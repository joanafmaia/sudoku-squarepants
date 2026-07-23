/**
 * Must run before PyScript so Discord can reach Pyodide via URL mappings.
 * Absolute CDN `import()` is also remapped by the importmap in index.html.
 */
import { patchUrlMappings } from "@discord/embedded-app-sdk";

try {
  patchUrlMappings(
    [
      { prefix: "/pyscript", target: "pyscript.net" },
      { prefix: "/jsdelivr", target: "cdn.jsdelivr.net" },
    ],
    {
      patchFetch: true,
      patchWebSocket: true,
      patchXhr: true,
      patchSrcAttributes: true,
    }
  );
} catch (err) {
  console.warn("[Thcoku] patchUrlMappings skipped", err);
}
