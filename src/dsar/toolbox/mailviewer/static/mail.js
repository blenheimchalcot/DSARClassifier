/* Mail viewer. Vanilla JS, no external libraries. */
"use strict";
(() => {
  const {esc, get, post, deliver, busy, fmtDate, fmtBytes} = window.DSAR;
  const $ = id => document.getElementById(id);
  const IS_CASE = $("mailapp").dataset.case === "1";
  const VIEWABLE = /\.(pdf|png|jpe?g|gif|txt|csv)$/i;
  const S = {m: null, f: null, page: 1, per: 50, total: 0, sel: null};

  function status(msg, isErr) {
    const el = $("status");
    el.textContent = msg || "";
    el.classList.toggle("err", !!isErr);
  }

  async function openMailbox(id) {
    busy(true, IS_CASE ? "Checking the file against the manifest and opening it…" : "Opening…");
    let r;
    try { r = await post("api/open", {id}); } finally { busy(false); }
    if (!r.success) { status(r.error, true); return; }
    S.m = id; S.f = null; S.sel = null;
    status(`${r.source.label}` + (r.sha256 ? ` · SHA-256 matches manifest (${r.sha256.slice(0, 12)}…)` : ""));
    $("xlsx-btn").disabled = false;
    $("search-input").disabled = false;
    $("search-input").value = "";
    $("message-list").innerHTML = '<p class="mv-hint">Choose a folder.</p>';
    $("message-detail").innerHTML = '<p class="mv-hint">Choose a message to read it.</p>';
    $("pager").hidden = true;
    const tree = $("folder-tree");
    tree.innerHTML = "";
    tree.appendChild(folderNode(r.tree, true));
  }

  function folderNode(node, open) {
    const wrap = document.createElement("div");
    const row = document.createElement("div");
    row.className = "mv-folder";
    row.dataset.ix = node.ix;
    const kids = node.children || [];
    row.innerHTML = `<span class="tg">${kids.length ? (open ? "▼" : "▶") : ""}</span><span class="nm">${esc(node.name)}</span>` +
      (node.count ? `<span class="ct">${node.count}</span>` : "");
    wrap.appendChild(row);
    let box = null;
    if (kids.length) {
      box = document.createElement("div");
      box.className = "mv-kids" + (open ? "" : " shut");
      kids.forEach(k => box.appendChild(folderNode(k, false)));
      wrap.appendChild(box);
    }
    row.onclick = e => {
      e.stopPropagation();
      if (box) { const shut = box.classList.toggle("shut"); row.querySelector(".tg").textContent = shut ? "▶" : "▼"; }
      document.querySelectorAll(".mv-folder.on").forEach(x => x.classList.remove("on"));
      row.classList.add("on");
      S.f = node.ix; S.page = 1;
      $("list-title").textContent = node.name;
      $("search-input").value = "";
      loadList();
    };
    return wrap;
  }

  async function loadList() {
    const q = new URLSearchParams({m: S.m, f: S.f, page: S.page, per_page: S.per});
    const r = await get(`api/messages?${q}`);
    const list = $("message-list");
    if (!r.success) { list.innerHTML = `<p class="mv-hint">${esc(r.error)}</p>`; return; }
    S.total = r.total;
    list.innerHTML = r.messages.length ? "" : '<p class="mv-hint">No messages in this folder.</p>';
    r.messages.forEach(m => list.appendChild(row(m, S.f)));
    const pages = Math.ceil(r.total / S.per);
    $("pager").hidden = pages <= 1;
    $("page-info").textContent = `Page ${r.page} of ${pages} (${r.total})`;
    $("prev-btn").disabled = r.page <= 1;
    $("next-btn").disabled = r.page >= pages;
  }

  function row(m, folder, folderName) {
    const el = document.createElement("div");
    el.className = "mv-row";
    el.innerHTML = `<div class="top"><span class="from">${esc(m.sender || "(no sender)")}</span>` +
      `<span class="dt">${esc(fmtDate(m.date))}</span></div>` +
      `<div class="subj">${m.attachments ? "📎 " : ""}${esc(m.subject || "(no subject)")}</div>` +
      (folderName ? `<div class="fld">${esc(folderName)}</div>` : "");
    el.onclick = () => {
      document.querySelectorAll(".mv-row.on").forEach(x => x.classList.remove("on"));
      el.classList.add("on");
      openMessage(folder, m.index);
    };
    return el;
  }

  $("prev-btn").onclick = () => { if (S.page > 1) { S.page--; loadList(); } };
  $("next-btn").onclick = () => { S.page++; loadList(); };

  async function openMessage(f, i) {
    const r = await get(`api/message?${new URLSearchParams({m: S.m, f, i})}`);
    const box = $("message-detail");
    if (!r.success) { box.innerHTML = `<p class="mv-hint">${esc(r.error)}</p>`; return; }
    S.sel = {f, i};
    const line = (k, v) => v ? `<tr><td>${k}</td><td>${esc(v)}</td></tr>` : "";
    const atts = r.attachments.map((a, n) => {
      const view = VIEWABLE.test(a.name) ? `<button class="btn sm" data-act="view" data-a="${n}">View</button>` : "";
      const open = !IS_CASE ? `<button class="btn sm" data-act="open" data-a="${n}">Open</button>` : "";
      return `<span class="mv-att">📎 ${esc(a.name)} <span class="sz">${esc(fmtBytes(a.size))}</span>${view}${open}` +
        `<button class="btn sm" data-act="save" data-a="${n}">${IS_CASE ? "Save to case" : "Download"}</button></span>`;
    }).join("");
    let body;
    if (r.html_body) {
      body = '<iframe class="mv-frame" sandbox title="Message body"></iframe>';
    } else if (r.plain_body) {
      body = `<div class="mv-plain">${esc(r.plain_body)}</div>`;
    } else {
      body = '<p class="mv-hint">No message body.</p>';
    }
    box.innerHTML = `<div class="mv-msg"><h2>${esc(r.subject || "(no subject)")}</h2>
      <table class="mv-meta">${line("From", r.sender)}${line("To", r.to)}${line("Cc", r.cc)}${line("Bcc", r.bcc)}
      ${line("Date", fmtDate(r.date, true))}${line("Folder", r.folder_name)}
      ${r.message_class && r.message_class !== "IPM.Note" ? line("Type", r.message_class) : ""}</table>
      <div class="mv-actions"><button class="btn sm" data-act="eml">${IS_CASE ? "Save message to case (.eml)" : "Download .eml"}</button></div>
      <div id="saved"></div>
      ${atts ? `<div class="mv-atts">${atts}</div>` : ""}${body}</div>`;
    if (r.html_body) box.querySelector("iframe").srcdoc = r.html_body;   // sandboxed; the page CSP blocks remote content
    box.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(b.dataset.act, b.dataset.a));
  }

  function saved(r) {
    const el = $("saved") || $("status");
    if (!r.success) { status(r.error, true); return; }
    if (r.saved) el.innerHTML = `<div class="mv-saved">Saved read-only: ${esc(r.saved)}<br>SHA-256 ${esc(r.sha256)}</div>`;
    else if (r.downloaded) status(`Downloaded ${r.downloaded}`);
  }

  async function act(what, a) {
    const p = {m: S.m, f: S.sel.f, i: S.sel.i};
    if (what === "view") {
      window.open(`api/attachment/view?${new URLSearchParams({...p, a})}`, "_blank", "noopener");
    } else if (what === "open") {
      const body = new URLSearchParams({...p, a});
      const res = await fetch("api/attachment/open", {method: "POST", headers: {"X-CSRF": window.DSAR.csrf}, body});
      const r = await res.json();
      if (!r.success) status(r.error, true);
    } else if (what === "save") {
      saved(await deliver("api/save", {...p, a, kind: "attachment"}));
    } else if (what === "eml") {
      saved(await deliver("api/save", {...p, kind: "eml"}));
    }
  }

  $("xlsx-btn").onclick = async () => {
    busy(true, "Exporting every message to Excel… large mailboxes take a while");
    try {
      const r = await deliver("api/save", {m: S.m, kind: "xlsx"});
      if (r.saved) status(`Saved read-only: ${r.saved} (SHA-256 ${r.sha256.slice(0, 12)}…)`);
      else saved(r);
    } finally { busy(false); }
  };

  let timer = null;
  $("search-input").addEventListener("input", () => {
    clearTimeout(timer);
    const q = $("search-input").value.trim();
    if (!q) { if (S.f !== null) loadList(); return; }
    timer = setTimeout(async () => {
      busy(true, "Searching…");
      let r;
      try { r = await get(`api/search?${new URLSearchParams({m: S.m, q})}`); } finally { busy(false); }
      const list = $("message-list");
      $("pager").hidden = true;
      $("list-title").textContent = `Search: ${q}`;
      if (!r.success) { list.innerHTML = `<p class="mv-hint">${esc(r.error)}</p>`; return; }
      list.innerHTML = r.results.length ? "" : '<p class="mv-hint">No matches.</p>';
      r.results.forEach(m => list.appendChild(row(m, m.folder, m.folder_name)));
      if (r.limited) list.insertAdjacentHTML("beforeend", '<p class="mv-hint">First 200 matches shown.</p>');
    }, 400);
  });

  window.DSAR.mailboxPicker(openMailbox, status);
})();
