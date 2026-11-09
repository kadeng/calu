// Triage mode: answer what comes, one escalation at a time, across every session. It polls the
// database through the web server (an escalation is a row, not an event, so polling is the
// honest mechanism here) and never reloads the page while someone is typing a note.
(() => {
  const api = document.currentScript.dataset.api;
  const el = (id) => document.getElementById(id);
  let queue = [];
  let shown = null;      // the id currently on screen
  let skipped = new Set();
  let busy = false;

  async function poll() {
    if (busy) return;
    try {
      const answer = await fetch(api, { headers: { accept: "application/json" } });
      queue = (await answer.json()).escalations || [];
    } catch (error) {
      return;  // the server went away; the next tick tries again
    }
    render();
  }

  function next() {
    return queue.find((entry) => !skipped.has(entry.id)) || null;
  }

  function render() {
    el("count").textContent = queue.length ? `· ${queue.length} waiting` : "· nothing waiting";
    const entry = next();
    if (!entry) {
      el("current").innerHTML = '<p class="empty">Nothing is waiting. Sessions run on.</p>';
      shown = null;
    } else if (entry.id !== shown) {
      show(entry);
      if (document.hidden && window.Notification && Notification.permission === "granted") {
        new Notification("calu: a step waits for you", { body: `${entry.where}\n${entry.reason}` });
      }
    }
    const rest = queue.filter((other) => !shown || other.id !== shown);
    el("queue-count").textContent = rest.length ? `(${rest.length})` : "(none)";
    el("queue").innerHTML = "";
    for (const other of rest) {
      const row = document.createElement("p");
      row.className = "muted";
      row.textContent = `${other.where} — ${other.reason}`;
      if (skipped.has(other.id)) row.textContent += " (skipped)";
      el("queue").appendChild(row);
    }
  }

  function show(entry) {
    shown = entry.id;
    const card = document.createElement("div");
    card.className = "card escalation";
    const where = document.createElement("p");
    where.className = "muted";
    const link = document.createElement("a");
    link.href = entry.session_url;
    link.textContent = entry.where;
    where.append(`escalation ${entry.id} · `, link, ` · since ${entry.created_at.slice(11, 19)}`);
    const why = document.createElement("p");
    why.textContent = entry.reason;
    const rendered = document.createElement("pre");
    rendered.textContent = entry.rendered || "";
    const row = document.createElement("p");
    row.className = "row";
    const note = document.createElement("input");
    note.type = "text";
    note.id = "note";
    note.placeholder = "note for Claude (optional)";
    const allow = document.createElement("button");
    allow.className = "primary";
    allow.textContent = "Allow (a)";
    allow.onclick = () => answer("allow");
    const deny = document.createElement("button");
    deny.className = "danger";
    deny.textContent = "Deny (d)";
    deny.onclick = () => answer("deny");
    const skip = document.createElement("button");
    skip.textContent = "Skip (s)";
    skip.onclick = () => { skipped.add(entry.id); shown = null; render(); };
    row.append(note, allow, deny, skip);
    card.append(where, why, rendered, row);
    if (entry.command) {
      const remember = document.createElement("p");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.id = "remember";
      const label = document.createElement("label");
      label.htmlFor = "remember";
      label.textContent = ` and remember exactly this command: ${entry.command}`;
      remember.append(box, label);
      card.appendChild(remember);
    }
    el("current").innerHTML = "";
    el("current").appendChild(card);
    note.focus();
  }

  async function answer(decision) {
    const entry = next();
    if (!entry || busy) return;
    busy = true;
    const note = el("note");
    const remember = el("remember");
    try {
      const answered = await fetch(`${api}/${entry.id}/resolve`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          decision,
          note: note ? note.value : "",
          remember: remember && remember.checked ? entry.command : "",
        }),
      });
      const result = await answered.json();
      if (!result.ok) { alert(`not answered: ${result.message}`); }
      queue = queue.filter((other) => other.id !== entry.id);
      shown = null;
    } finally {
      busy = false;
    }
    render();
    poll();
  }

  el("notify").onclick = async () => {
    if (!window.Notification) return;
    const permission = await Notification.requestPermission();
    el("notify-row").textContent = permission === "granted"
      ? "Browser notifications are on."
      : "Browser notifications were refused.";
  };
  if (window.Notification && Notification.permission === "granted") {
    el("notify-row").textContent = "Browser notifications are on.";
  }

  addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const typing = document.activeElement && document.activeElement.id === "note";
    if (event.key === "Enter" && typing) { answer("allow"); return; }
    if (typing && event.key !== "Escape") return;
    if (event.key === "a") { event.preventDefault(); answer("allow"); }
    if (event.key === "d") { event.preventDefault(); answer("deny"); }
    if (event.key === "s") {
      event.preventDefault();
      const entry = next();
      if (entry) { skipped.add(entry.id); shown = null; render(); }
    }
    if (event.key === "n") { event.preventDefault(); if (el("note")) el("note").focus(); }
    if (event.key === "Escape" && el("note")) el("note").blur();
  });

  poll();
  setInterval(poll, 2000);
})();
