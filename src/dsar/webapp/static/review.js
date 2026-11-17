/* Review item page: render the text with evidence and selected passages, record a decision. */
"use strict";
(() => {
  const data = JSON.parse(document.getElementById("item-data").textContent);
  const text = data.content;
  const box = document.getElementById("text");
  const $ = id => document.getElementById(id);
  let spans = [];          // {kind: "extract"|"redact", start, end, reason}

  // Start from the current decision when it was made here on this exact text.
  const cur = data.current;
  if (cur && cur.source === "app" && cur.content_sha256 === data.sha) {
    spans = cur.spans.map(s => ({...s}));
    const radio = document.querySelector(`input[name=decision][value="${CSS.escape(cur.decision)}"]`);
    if (radio) radio.checked = true;
    $("reason").value = cur.reason || "";
    $("notes").value = cur.notes || "";
    $("assessment").value = cur.assessment || "";
    $("adjusted").value = cur.adjusted_score ?? "";
  }

  function decision() {
    const r = document.querySelector("input[name=decision]:checked");
    return r ? r.value : "";
  }

  // The rendered text must equal the content exactly, so selection offsets are character offsets.
  function render() {
    const marks = [];
    for (const [a, b] of data.evidence) marks.push({a, b, cls: "ev"});
    for (const s of spans) if (s.kind === kindFor(decision())) marks.push({a: s.start, b: s.end, cls: s.kind === "extract" ? "ex" : "rd"});
    const cuts = new Set([0, text.length]);
    for (const m of marks) { cuts.add(m.a); cuts.add(m.b); }
    const pts = [...cuts].sort((x, y) => x - y);
    box.textContent = "";
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      const cls = marks.filter(m => m.a <= a && m.b >= b).map(m => m.cls);
      const node = document.createTextNode(text.slice(a, b));
      if (cls.length) {
        const span = document.createElement("span");
        span.className = cls.join(" ");
        span.appendChild(node);
        box.appendChild(span);
      } else {
        box.appendChild(node);
      }
    }
    const list = $("spans");
    list.innerHTML = "";
    spans.filter(s => s.kind === kindFor(decision())).sort((x, y) => x.start - y.start).forEach(s => {
      const li = document.createElement("li");
      const snippet = text.slice(s.start, s.end).replace(/\s+/g, " ");
      li.innerHTML = `<span class="lg ${s.kind === "extract" ? "ex" : "rd"}">${s.kind === "extract" ? "Extract" : "Redact"}</span> ` +
        `${DSAR.esc(snippet.length > 160 ? snippet.slice(0, 160) + "…" : snippet)}` +
        (s.reason ? ` <span class="muted">(${DSAR.esc(s.reason)})</span>` : "") + " ";
      const rm = document.createElement("button");
      rm.className = "btn sm"; rm.type = "button"; rm.textContent = "Remove";
      rm.onclick = () => { spans = spans.filter(x => x !== s); render(); };
      li.appendChild(rm);
      list.appendChild(li);
    });
  }

  function kindFor(d) {
    return d === data.EXTRACT ? "extract" : d === data.RELEASE_REDACTED ? "redact" : "";
  }

  function offset(node, off) {
    const r = document.createRange();
    r.setStart(box, 0);
    r.setEnd(node, off);
    return r.toString().length;
  }

  function selection() {
    const sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return null;
    const r = sel.getRangeAt(0);
    if (!box.contains(r.startContainer) || !box.contains(r.endContainer)) return null;
    let a = offset(r.startContainer, r.startOffset), b = offset(r.endContainer, r.endOffset);
    while (a < b && /\s/.test(text[a])) a++;
    while (b > a && /\s/.test(text[b - 1])) b--;
    sel.removeAllRanges();
    return b > a ? [a, b] : null;
  }

  function add(kind, a, b, reason) {
    // Merge with overlapping passages of the same kind.
    const same = spans.filter(s => s.kind === kind && s.start < b && s.end > a && (kind === "extract" || s.reason === reason));
    if (spans.some(s => s.kind === kind && s.start < b && s.end > a && !same.includes(s))) {
      msg("That overlaps a passage with a different reason; remove it first.", true);
      return;
    }
    for (const s of same) { a = Math.min(a, s.start); b = Math.max(b, s.end); }
    spans = spans.filter(s => !same.includes(s));
    spans.push({kind, start: a, end: b, reason: reason || ""});
    msg("");
    render();
  }

  function msg(t, bad) {
    $("msg").innerHTML = t ? `<div class="${bad ? "err" : "note"}">${DSAR.esc(t)}</div>` : "";
  }

  function refresh() {
    const d = decision();
    $("extract-tools").hidden = d !== data.EXTRACT;
    $("redact-tools").hidden = d !== data.RELEASE_REDACTED;
    $("reason-row").hidden = d !== data.WITHHOLD;
    render();
  }

  document.querySelectorAll("input[name=decision]").forEach(r => r.addEventListener("change", refresh));
  $("btn-extract").onclick = () => { const s = selection(); s ? add("extract", s[0], s[1]) : msg("Select some of the text first.", true); };
  $("btn-whole").onclick = () => { spans = spans.filter(s => s.kind !== "extract"); add("extract", 0, text.length); };
  $("btn-redact").onclick = () => { const s = selection(); s ? add("redact", s[0], s[1], $("redact-reason").value) : msg("Select some of the text first.", true); };
  $("save").onclick = async () => {
    const d = decision();
    const body = {decision: d, content_sha256: data.sha, reason: d === data.WITHHOLD ? $("reason").value : "",
                  notes: $("notes").value, assessment: $("assessment").value, adjusted_score: $("adjusted").value,
                  spans: spans.filter(s => s.kind === kindFor(d))};
    $("save").disabled = true;
    try {
      const res = await fetch(data.decide_url, {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRF": document.querySelector('meta[name="csrf"]').content},
        body: JSON.stringify(body)});
      const type = res.headers.get("Content-Type") || "";
      const j = type.includes("json") ? await res.json() : {success: false, error: await res.text()};
      if (!j.success) { msg(j.error || "Not saved.", true); return; }
      window.location = j.next;
    } finally {
      $("save").disabled = false;
    }
  };
  refresh();
})();
