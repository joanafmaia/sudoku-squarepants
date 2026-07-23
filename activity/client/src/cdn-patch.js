/**
 * Must run before PyScript/core.js so Discord CSP allows Pyodide (jsdelivr).
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

(function patchCdnToSameOrigin() {
  const rewrite = (value) =>
    String(value)
      .replace(/^https?:\/\/pyscript\.net/gi, "/pyscript")
      .replace(/^https?:\/\/cdn\.jsdelivr\.net/gi, "/jsdelivr");

  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (typeof input === "string") {
      return origFetch(rewrite(input), init);
    }
    if (input instanceof Request) {
      const url = rewrite(input.url);
      if (url !== input.url) {
        return origFetch(new Request(url, input), init);
      }
    }
    return origFetch(input, init);
  };

  const XHROpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function patchedOpen(method, url, ...rest) {
    return XHROpen.call(this, method, rewrite(url), ...rest);
  };
})();
