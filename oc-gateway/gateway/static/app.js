(function () {
  "use strict";
  var token = sessionStorage.getItem("adm") || "";
  var $ = function (id) { return document.getElementById(id); };
  function h(tag, attrs) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") e.className = attrs[k]; else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]); else e.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) { var c = arguments[i]; if (c == null) continue; e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c); }
    return e;
  }
  function api(method, path, body) {
    return fetch(path, { method: method, headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { if (r.status === 401) { logout(); throw new Error("unauthorized"); } return r.json().then(function (j) { j._status = r.status; return j; }); });
  }
  function say(t) { $("msg").textContent = t; }
  function ago(ts) { if (!ts) return "never"; var s = Math.round(Date.now() / 1000 - ts); return s < 90 ? s + "s ago" : s < 5400 ? Math.round(s / 60) + "m ago" : Math.round(s / 3600) + "h ago"; }
  function cls(state) { return state === "healthy" ? "ok" : (state === "rate_limited" || state === "recovering") ? "warn" : "bad"; }
  function logout() { token = ""; sessionStorage.removeItem("adm"); $("login").hidden = false; $("main").hidden = true; }

  $("loginform").addEventListener("submit", function (e) { e.preventDefault(); token = $("tok").value.trim(); $("tok").value = ""; sessionStorage.setItem("adm", token); load(); });

  function provCard(p) {
    var hs = p.health, q = p.quota, lim = q.documented_limits_estimated || {}, v = q.verified_from_provider;
    var quotaTxt = [];
    if (v) quotaTxt.push("VERIFIED (provider headers, " + ago(v.observed_at) + "): requests left " + (v.remaining_requests != null ? v.remaining_requests : "?") + ", tokens left " + (v.remaining_tokens != null ? v.remaining_tokens : "?"));
    else quotaTxt.push("verified remaining quota: not exposed by this provider");
    quotaTxt.push("ESTIMATED today: " + q.estimated_requests_today + " requests" + (lim.rpd ? " of documented " + lim.rpd + "/day" : "") + (lim.rpm ? " (documented " + lim.rpm + "/min)" : ""));
    var card = h("div", { "class": "card" });
    card.appendChild(h("h2", {}, p.name + "  ", h("span", { "class": cls(hs.state) }, hs.available ? hs.state : hs.state + " / " + hs.reason),
      hs.cooldown_remaining_s ? h("span", { "class": "warn" }, "  cooldown " + hs.cooldown_remaining_s + "s") : null));
    card.appendChild(h("div", { "class": "mut" }, p.base_url + " | priority " + p.priority + " | " + (p.enabled ? "enabled" : "DISABLED")));
    card.appendChild(h("div", {}, "Last success: " + ago(hs.last_success) + " | last failure: " + ago(hs.last_failure) + (hs.last_error_kind ? " (" + hs.last_error_kind + ": " + (hs.last_error || "").slice(0, 120) + ")" : "")));
    card.appendChild(h("div", {}, quotaTxt.join(" | ")));
    card.appendChild(h("div", {}, "Today: " + p.today.req + " req, " + p.today.ok + " ok, " + p.today.rl + " rate-limited, " + p.today.qe + " quota events, " + p.today.er + " errors, tokens " + (p.today.pt + p.today.ct) +
      (p.today.te ? " (partly estimated)" : " (provider-reported)") + ", est. cost $" + p.today.cost.toFixed(4) + " | all-time: " + p.totals.req + " req, " + p.totals.rl + " rate-limit, " + p.totals.qe + " quota"));
    var mt = h("table", {}, h("tr", {}, h("th", {}, "Model"), h("th", {}, "Context"), h("th", {}, "chat"), h("th", {}, "stream"), h("th", {}, "tools")));
    p.models.forEach(function (m) {
      mt.appendChild(h("tr", {}, h("td", {}, m.id + (m.model_available ? "" : "  (cooling down)")), h("td", {}, m.context || "unknown"),
        ["chat", "stream", "tools"].map(function (c) { var s = m.caps[c]; return h("td", { "class": s === "verified" ? "ok" : s === "failed" ? "bad" : "mut" }, s); })));
    });
    card.appendChild(h("div", { "class": "wrap" }, mt));
    var kt = h("table", {}, h("tr", {}, h("th", {}, "Key"), h("th", {}, "Label"), h("th", {}, "Status"), h("th", {}, "Last used"), h("th", {}, "")));
    p.keys.forEach(function (k) {
      kt.appendChild(h("tr", {}, h("td", {}, k.masked), h("td", {}, k.label), h("td", { "class": k.status === "active" && k.enabled ? "ok" : "bad" }, (k.enabled ? k.status : "disabled") + (k.reason ? " (" + k.reason + ")" : "")), h("td", {}, ago(k.last_used)),
        h("td", {}, h("button", { onclick: function () { api("POST", "/admin/api/keys/" + k.id, { enabled: !k.enabled }).then(load); } }, k.enabled ? "Disable" : "Enable"),
          h("button", { onclick: function () { if (confirm("Delete this key?")) api("DELETE", "/admin/api/keys/" + k.id).then(load); } }, "Delete"))));
    });
    card.appendChild(h("div", { "class": "wrap" }, kt));
    var key = h("input", { type: "password", placeholder: "new API key", autocomplete: "off", size: "28" });
    var label = h("input", { placeholder: "label", size: "10", value: "key" });
    var prio = h("input", { type: "number", value: p.priority, style: "width:70px" });
    var cd = h("input", { type: "number", placeholder: "rate-limit cooldown s", style: "width:150px" });
    card.appendChild(h("div", { "class": "row" },
      key, label, h("button", { onclick: function () { api("POST", "/admin/api/keys", { provider: p.name, label: label.value, key: key.value }).then(function (r) { key.value = ""; say(r.ok ? "key saved (" + r.masked + ")" : JSON.stringify(r)); load(); }); } }, "Save / replace key"),
      h("button", { onclick: function () { api("POST", "/admin/api/providers/" + p.name, { enabled: !p.enabled }).then(load); } }, p.enabled ? "Disable provider" : "Enable provider"),
      "priority", prio, h("button", { onclick: function () { api("POST", "/admin/api/providers/" + p.name, { priority: parseInt(prio.value, 10) }).then(load); } }, "Set"),
      cd, h("button", { onclick: function () { var s = parseFloat(cd.value); if (!isNaN(s)) api("POST", "/admin/api/providers/" + p.name, { cooldowns: { rate_limit: s } }).then(function (r) { say(JSON.stringify(r)); load(); }); } }, "Set cooldown"),
      h("button", { onclick: function () { say("testing..."); api("POST", "/admin/api/test/" + p.name).then(function (r) { say(p.name + " connection test: " + JSON.stringify(r)); }); } }, "Test connection"),
      h("button", { onclick: function () { if (confirm("Probe sends up to 3 real requests per model and uses a little provider quota. Continue?")) { say("probing..."); api("POST", "/admin/api/probe/" + p.name).then(function (r) { say(JSON.stringify(r, null, 1)); load(); }); } } }, "Probe capabilities")));
    return card;
  }

  function table(el, head, rows) {
    el.textContent = "";
    el.appendChild(h("tr", {}, head.map(function (x) { return h("th", {}, x); })));
    rows.forEach(function (r) { el.appendChild(h("tr", {}, r.map(function (c) { return h("td", {}, c == null ? "" : String(c)); }))); });
  }

  function load() {
    if (!token) return;
    api("GET", "/admin/api/status").then(function (s) {
      $("login").hidden = true; $("main").hidden = false; $("ver").textContent = " v" + s.version;
      var sel = $("strategy"); sel.textContent = "";
      s.strategies.forEach(function (x) { var o = h("option", { value: x }, x); if (x === s.strategy) o.selected = true; sel.appendChild(o); });
      var box = $("providers"); box.textContent = "";
      s.providers.forEach(function (p) { box.appendChild(provCard(p)); });
      table($("journal"), ["time", "alias", "provider", "model", "stream", "status", "detail"], s.recent_journal.map(function (j) { return [new Date(j.ts * 1000).toLocaleTimeString(), j.alias, j.provider, j.model, j.stream ? "yes" : "no", j.status, j.detail]; }));
      return api("GET", "/admin/api/events");
    }).then(function (ev) { if (ev) table($("events"), ["time", "provider", "kind", "detail"], ev.map(function (e) { return [new Date(e.ts * 1000).toLocaleString(), e.provider, e.kind, e.detail]; })); })
      .catch(function (e) { say(String(e)); });
  }
  $("setstrat").addEventListener("click", function () { api("POST", "/admin/api/strategy", { strategy: $("strategy").value }).then(function (r) { say("strategy: " + (r.strategy || JSON.stringify(r))); load(); }); });
  $("refresh").addEventListener("click", load);
  $("backup").addEventListener("click", function () { api("POST", "/admin/api/backup").then(function (r) { say("backup: " + JSON.stringify(r)); }); });
  $("addprov").addEventListener("click", function () {
    var ids = $("np_models").value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    api("POST", "/admin/api/providers", { name: $("np_name").value.trim(), base_url: $("np_url").value.trim(), priority: parseInt($("np_prio").value, 10) || 100, alias: $("np_alias").value.trim() || undefined,
      models: ids.map(function (i) { return { id: i, claims: ["chat", "stream", "tools"] }; }) }).then(function (r) { say(JSON.stringify(r)); load(); });
  });
  if (token) load();
  setInterval(function () { if (token && !document.hidden) load(); }, 10000);
})();
