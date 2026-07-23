/** Load PyScript after CDN patches are applied. */
const PYSCRIPT_CSS = "/pyscript/releases/2026.7.2/core.css";
const PYSCRIPT_JS = "/pyscript/releases/2026.7.2/core.js";

export function loadPyScript() {
  if (!document.querySelector(`link[href="${PYSCRIPT_CSS}"]`)) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = PYSCRIPT_CSS;
    document.head.appendChild(link);
  }
  if (!document.querySelector(`script[src="${PYSCRIPT_JS}"]`)) {
    const script = document.createElement("script");
    script.type = "module";
    script.src = PYSCRIPT_JS;
    script.crossOrigin = "";
    document.head.appendChild(script);
  }
}
