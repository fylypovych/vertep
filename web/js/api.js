(() => {
  "use strict";

  async function api(url, opt = {}) {
    if (!opt || typeof opt !== "object" || Array.isArray(opt)) opt = {};
    const csrf = document.cookie
      .split("; ")
      .find((x) => x.startsWith("vertep_csrf="))
      ?.split("=")[1];
    opt.headers = {
      ...(opt.headers || {}),
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    };
    const r = await fetch(url, opt);
    if (!r.ok)
      throw new Error(
        (await r.json().catch(() => ({}))).detail || r.statusText,
      );
    return r.json();
  }

  window.api = api;
})();
