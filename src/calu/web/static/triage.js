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
  let asking = false;    // the one on screen is a question of the agent, not a step to judge
  let failed = 0;        // polls that failed in a row
  let key = "";          // the key of this browser's login (login.js), sent with every answer
  try { key = localStorage.getItem("calu-key") || ""; } catch (e) { /* storage blocked */ }

  // A poll that fails leaves the last queue on screen, which may be out of date: sessions can
  // wait that are not shown. One failure can be a restart; from the second on the page says so,
  // until a poll succeeds again.
  function stale(on) {
    const box = el("stale");
    if (!on) { box.replaceChildren(); return; }
    if (box.firstChild) return;
    const card = document.createElement("div");
    card.className = "card escalation";
    card.textContent =
      "calu web or its database cannot be reached: this list may be out of date, and " +
      "sessions may wait that are not shown here. The page keeps trying.";
    box.appendChild(card);
  }

  async function poll() {
    if (busy) return;
    try {
      const answer = await fetch(api, { headers: { accept: "application/json" } });
      if (!answer.ok) throw new Error(`status ${answer.status}`);
      queue = (await answer.json()).escalations || [];
    } catch (error) {
      failed += 1;
      stale(failed >= 2);
      return;
    }
    failed = 0;
    stale(false);
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
    // The session's process stopped saying it is there, so a decision would be recorded and
    // never acted on: say so above the buttons, not after one was pressed (D56, D57).
    let gone = null;
    if (entry.unanswerable) {
      gone = document.createElement("p");
      gone.className = "decision deny";
      gone.textContent =
        "The session's process is gone: an answer is recorded for the history, but the step " +
        "will not run and the session will not continue.";
    }
    const parts = gone ? [where, gone, why] : [where, why];
    const rendered = document.createElement("pre");
    rendered.textContent = entry.rendered || "";
    if ((entry.questions || []).length) {
      // A question of the agent: the options are the answer (PLAN.md D35).
      card.append(...parts);
      for (const question of entry.questions) {
        const asked = document.createElement("p");
        asked.textContent = question.question || question.header || "a question";
        const choices = document.createElement("p");
        choices.className = "row";
        for (const option of question.options || []) {
          const button = document.createElement("button");
          button.className = "option";
          button.textContent = option.label;
          button.title = option.description || "";
          button.onclick = () => answer("answer", option.label);
          choices.appendChild(button);
        }
        const free = document.createElement("input");
        free.type = "text";
        free.id = "note";
        free.placeholder = "or answer in your own words, then Enter";
        choices.appendChild(free);
        card.append(asked, choices);
      }
      el("current").innerHTML = "";
      el("current").appendChild(card);
      const free = el("note");
      if (free) free.focus();
      asking = true;
      return;
    }
    asking = false;
    const row = document.createElement("p");
    row.className = "row";
    const note = document.createElement("input");
    note.type = "text";
    note.id = "note";
    note.placeholder = "note for Claude (optional)";
    const allow = document.createElement("button");
    allow.className = "allow";
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
    card.append(...parts, rendered, row);
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

  async function answer(decision, text) {
    const entry = next();
    if (!entry || busy) return;
    busy = true;
    const note = el("note");
    const remember = el("remember");
    const given = text !== undefined ? text : (note ? note.value : "");
    try {
      const answered = await fetch(`${api}/${entry.id}/resolve`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-calu-key": key },
        body: JSON.stringify({
          decision,
          note: decision === "answer" ? "" : given,
          answer: decision === "answer" ? given : "",
          remember: remember && remember.checked ? entry.command : "",
        }),
      });
      // 401 (no key, or calu web was restarted) and 503 (no database) do not answer in the
      // shape below, and the escalation is still open: it stays on screen.
      let result = null;
      try { result = await answered.json(); } catch (error) { result = null; }
      if (!answered.ok && !(result && result.message)) {
        alert(answered.status === 401
          ? "not answered: this browser is not logged in to calu web any more. Open the " +
            "address that `calu web` printed again."
          : `not answered: calu web answered with status ${answered.status}`);
        return;
      }
      if (!result.ok) { alert(`not answered: ${result.message}`); }
      if (answered.status === 503) return;
      queue = queue.filter((other) => other.id !== entry.id);
      shown = null;
    } catch (error) {
      alert("not answered: calu web cannot be reached");
      return;
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
    if (event.key === "Enter" && typing) {
      answer(asking ? "answer" : "allow");
      return;
    }
    if (asking) return;  // a, d and s mean nothing for a question: an answer is needed
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
