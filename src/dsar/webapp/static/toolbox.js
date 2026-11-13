/* Helpers shared by the toolbox pages. Vanilla JS, local only. */
"use strict";
window.DSAR = (() => {
  const csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  async function get(url) {
    const res = await fetch(url, {credentials: "same-origin"});
    return res.json();
  }

  async function post(url, data) {
    const res = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF": csrf},
      body: JSON.stringify(data || {}),
    });
    return res.json();
  }

  /* POST form data to a save endpoint. A case-bound server answers with JSON
     ({saved, sha256}); a free-standing one sends the file, which is downloaded. */
  async function deliver(url, data) {
    const body = new URLSearchParams(data);
    const res = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"X-CSRF": csrf}, body});
    const type = res.headers.get("Content-Type") || "";
    if (type.includes("application/json")) return res.json();
    if (!res.ok) return {success: false, error: res.statusText};
    const blob = await res.blob();
    const name = ((res.headers.get("Content-Disposition") || "").match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/) || [])[1];
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name ? decodeURIComponent(name) : "download";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    return {success: true, downloaded: a.download};
  }

  function busy(on, text) {
    const el = document.getElementById("busy");
    if (!el) return;
    el.hidden = !on;
    const t = document.getElementById("busy-text");
    if (t && text) t.textContent = text;
  }

  function fmtDate(iso, full) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso.slice(0, 16).replace("T", " ");
    return full ? d.toLocaleString() : d.toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"});
  }

  function fmtBytes(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  /* Fill the mailbox picker: manifest list in a case, a path box when free-standing.
     onOpen(sourceId) is called when the operator opens one. */
  async function mailboxPicker(onOpen, setStatus) {
    const isCase = document.getElementById("pick-case") && document.querySelector("[data-case]").dataset.case === "1";
    const pickCase = document.getElementById("pick-case"), pickFree = document.getElementById("pick-free");
    if (isCase) {
      pickCase.hidden = false;
      const sel = document.getElementById("source-select");
      const data = await get("api/sources");
      if (!data.success) { setStatus(data.error, true); return; }
      sel.innerHTML = "";
      if (!data.sources.length) {
        sel.innerHTML = "<option value=''>No PST or MBOX files under input/</option>";
        setStatus(data.manifest ? "" : "No input manifest yet: run dsar manifest", !data.manifest);
      }
      for (const s of data.sources) {
        const o = document.createElement("option");
        o.value = s.id;
        o.disabled = !s.in_manifest;
        o.textContent = `${s.label} (${fmtBytes(s.size)})` + (s.in_manifest ? "" : " — not in manifest");
        sel.appendChild(o);
      }
      document.getElementById("open-btn").onclick = () => sel.value && onOpen(sel.value);
    } else {
      pickFree.hidden = false;
      const input = document.getElementById("path-input");
      const add = async () => {
        if (!input.value.trim()) { setStatus("Enter a path first", true); return; }
        const r = await post("api/add", {path: input.value});
        if (!r.success) { setStatus(r.error, true); return; }
        onOpen(r.source.id);
      };
      document.getElementById("add-btn").onclick = add;
      input.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
      document.getElementById("browse-btn").onclick = async () => {
        const start = await post("api/browse/start");
        if (!start.success) { setStatus(start.error, true); return; }
        setStatus("Choose a file in the Finder window…");
        let r;
        do { await new Promise(ok => setTimeout(ok, 500)); r = await get(`api/browse/poll/${start.job_id}`); }
        while (r.status !== "done");
        setStatus("");
        if (r.success) { input.value = r.path; add(); }
        else if (!r.cancelled) setStatus(r.error, true);
      };
    }
  }

  return {csrf, esc, get, post, deliver, busy, fmtDate, fmtBytes, mailboxPicker};
})();
