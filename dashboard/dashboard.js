// Read-only development dashboard. The token lives only in this closure (not in
// localStorage, cookies or the URL) and is sent as an Authorization header, so the
// page cannot be driven cross-site. All output is inserted as text (no innerHTML).
(function () {
  "use strict";
  let token = "";

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }

  function api(path) {
    return fetch(path, { headers: { Authorization: "Bearer " + token }, credentials: "omit" })
      .then((r) => {
        if (!r.ok) throw new Error(path + ": HTTP " + r.status);
        return r.json();
      });
  }

  function row(cells) {
    const tr = el("tr");
    cells.forEach((c) => tr.appendChild(c instanceof Node ? c : el("td", c)));
    return tr;
  }

  function showReport(id) {
    api("/v1/incidents/" + encodeURIComponent(id) + "/report").then((r) => {
      const title = document.getElementById("report-title");
      const pre = document.getElementById("report");
      title.hidden = pre.hidden = false;
      title.textContent = "Report (" + r.status + ")";
      pre.textContent = r.report ? r.report.body : "No report yet: " + r.status;
    }).catch((e) => { document.getElementById("error").textContent = e.message; });
  }

  function load() {
    document.getElementById("error").textContent = "";
    Promise.all([api("/v1/system/status"), api("/v1/incidents?limit=20")]).then(([st, inc]) => {
      document.getElementById("data").hidden = false;
      const overall = document.getElementById("overall");
      overall.textContent = st.overall;
      overall.className = st.overall === "healthy" ? "ok" : "degraded";
      document.getElementById("modes").textContent = st.modes.join(", ");
      const comps = document.getElementById("components");
      comps.replaceChildren();
      Object.entries(st.components).forEach(([name, c]) => {
        comps.appendChild(row([name, el("td", c.status, c.status === "ok" ? "ok" : "degraded"),
          JSON.stringify(Object.fromEntries(Object.entries(c).filter(([k]) => k !== "status")))]));
      });
      const body = document.getElementById("incidents");
      body.replaceChildren();
      inc.items.forEach((i) => {
        const btn = el("button", "view");
        btn.type = "button";
        btn.addEventListener("click", () => showReport(i.id));
        const td = el("td");
        td.appendChild(btn);
        body.appendChild(row([i.opened_at, i.incident_type, i.status, i.resolution || "", td]));
      });
    }).catch((e) => { document.getElementById("error").textContent = e.message; });
  }

  document.getElementById("auth").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = document.getElementById("token");
    token = input.value;
    input.value = "";
    load();
  });

  fetch("/health/ready")
    .then((r) => r.json().then((b) => ({ ok: r.ok, b })))
    .then(({ ok, b }) => {
      document.getElementById("ready").textContent =
        (ok ? "ready " : "not ready ") + JSON.stringify(b.checks || {});
    })
    .catch(() => { document.getElementById("ready").textContent = "unreachable"; });
})();
