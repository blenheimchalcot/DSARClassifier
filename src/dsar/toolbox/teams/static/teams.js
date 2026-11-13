/* Teams viewer. Vanilla JS, no external libraries. */
"use strict";
(() => {
  const {esc, get, post, deliver, busy, fmtDate} = window.DSAR;
  const $ = id => document.getElementById(id);
  const root = $("teamsapp");
  const IS_CASE = root.dataset.case === "1";
  const SUBJECT = (root.dataset.subject || "").toLowerCase().split(/\s+/).filter(w => w.length > 2);
  const COLOURS = ["#2f5d8a", "#b5651d", "#2e7d4f", "#8a4b9c", "#1f7a8c", "#6b5a1f"];
  const S = {m: null, convs: [], people: [], tab: "convs", key: null};

  const status = (msg, isErr) => { $("status").textContent = msg || ""; $("status").classList.toggle("err", !!isErr); };
  const isSubject = name => SUBJECT.length && SUBJECT.some(w => name.toLowerCase().includes(w));
  /* Long rosters (meeting chats can have hundreds): the first few names, the subject if present, then a count. */
  const names = list => {
    if (list.length <= 6) return list.join(", ");
    const shown = list.slice(0, 5);
    const subj = list.find(isSubject);
    if (subj && !shown.includes(subj)) shown.push(subj);
    return `${shown.join(", ")} and ${list.length - shown.length} more`;
  };
  const KIND = {"1:1": "1:1 chat", group: "group or meeting chat", channel: "channel", self: "self"};

  async function openMailbox(id) {
    busy(true, IS_CASE ? "Checking the file against the manifest, then finding Teams messages…" : "Finding Teams messages…");
    let r;
    try { r = await post("api/open", {id}); } finally { busy(false); }
    if (!r.success) { status(r.error, true); return; }
    S.m = id; S.convs = r.conversations; S.people = r.participants; S.key = null;
    status(`${r.source.label} · ${r.messages} Teams message(s)` + (r.sha256 ? ` · SHA-256 matches manifest` : ""));
    $("n-convs").textContent = `(${S.convs.length})`;
    $("n-people").textContent = `(${S.people.length})`;
    $("filter").disabled = false;
    $("conv").innerHTML = `<p class="mv-hint">${S.convs.length ? "Choose a conversation." : "No Teams chat messages in this mailbox."}</p>`;
    $("pdf-btn").disabled = true;
    side();
  }

  function side() {
    const f = $("filter").value.trim().toLowerCase();
    const box = $("side");
    box.innerHTML = "";
    if (S.tab === "convs") {
      const list = S.convs.filter(c => !f || c.participants.some(p => p.toLowerCase().includes(f)));
      list.forEach(c => {
        const el = document.createElement("div");
        el.className = "tv-item" + (c.key === S.key ? " on" : "") + (c.participants.some(isSubject) ? " subj" : "");
        el.innerHTML = `<div class="who">${esc(names(c.participants) || "(no participants)")}</div>` +
          `<div class="sub"><span>${esc(KIND[c.kind] || c.kind)}</span><span>${c.participants.length} people</span><span>${c.count} message(s)</span>` +
          `<span>${esc(fmtDate(c.first))} – ${esc(fmtDate(c.last))}</span></div>`;
        el.onclick = () => openConv(c.key);
        box.appendChild(el);
      });
      if (!list.length) box.innerHTML = '<p class="mv-hint">No conversations match.</p>';
    } else {
      const list = S.people.filter(p => !f || p.name.toLowerCase().includes(f));
      list.forEach(p => {
        const el = document.createElement("div");
        el.className = "tv-item" + (isSubject(p.name) ? " subj" : "");
        el.innerHTML = `<div class="who">${esc(p.name)}</div><div class="sub"><span>${p.total} in</span>` +
          `<span>${p.sent} sent</span><span>${p.partners} 1:1 partner(s)</span>${p.email ? `<span>${esc(p.email)}</span>` : ""}</div>`;
        el.onclick = () => { $("filter").value = p.name; tab("convs"); };
        box.appendChild(el);
      });
      if (!list.length) box.innerHTML = '<p class="mv-hint">No people match.</p>';
    }
  }

  function tab(t) {
    S.tab = t;
    $("tab-convs").classList.toggle("on", t === "convs");
    $("tab-people").classList.toggle("on", t === "people");
    side();
  }
  $("tab-convs").onclick = () => tab("convs");
  $("tab-people").onclick = () => tab("people");
  $("filter").addEventListener("input", side);

  async function openConv(key) {
    busy(true, "Reading messages…");
    let r;
    try { r = await get(`api/conversation?${new URLSearchParams({m: S.m, key})}`); } finally { busy(false); }
    if (!r.success) { status(r.error, true); return; }
    S.key = key;
    side();
    $("saved").innerHTML = "";
    const people = r.meta.participants;
    const colour = name => COLOURS[Math.max(0, people.findIndex(p => p.toLowerCase() === name.toLowerCase())) % COLOURS.length];
    $("conv-title").textContent = names(people);
    let day = null, html = `<div class="tv-chat"><div class="tv-meta">${esc(KIND[r.meta.kind] || "chat")}, ` +
      `${r.meta.count} message(s), ${esc(fmtDate(r.meta.first, true))} to ${esc(fmtDate(r.meta.last, true))}.` +
      (r.meta.thread ? ` Thread <span class="mono">${esc(r.meta.thread)}</span>.` : " No thread ID: grouped by exact participants.") +
      (people.length > 6 ? `<details><summary>All ${people.length} participants</summary>${esc(people.join(", "))}</details>` : "") + `</div>`;
    for (const m of r.messages) {
      const d = m.date ? new Date(m.date).toDateString() : "Undated";
      if (d !== day) { day = d; html += `<div class="tv-day">${esc(d)}</div>`; }
      const alt = people.length === 2 && m.sender.toLowerCase() === people[1].toLowerCase();
      html += `<div class="tv-msg${alt ? " alt" : ""}" style="--c:${colour(m.sender)}"><div class="h"><b>${esc(m.sender)}</b>` +
        `<span>${esc(fmtDate(m.date, true))}</span></div><div class="x">${esc(m.text || "(no text: may be an attachment, image, call or reaction)")}</div></div>`;
    }
    $("conv").innerHTML = html + "</div>";
    $("conv").scrollTop = 0;
    $("pdf-btn").disabled = false;
  }

  $("pdf-btn").onclick = async () => {
    busy(true, "Building the PDF…");
    let r;
    try { r = await deliver("api/pdf", {m: S.m, key: S.key}); } finally { busy(false); }
    if (!r.success) { status(r.error, true); return; }
    if (r.saved) $("saved").innerHTML = `<div class="mv-saved">Saved read-only: ${esc(r.saved)}<br>SHA-256 ${esc(r.sha256)}</div>`;
    else status(`Downloaded ${r.downloaded}`);
  };

  window.DSAR.mailboxPicker(openMailbox, status);
})();
