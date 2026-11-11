// What every page of the web interface carries: the attention badge and the jump palette.
//
// The badge answers "does anything need me?" without opening a page, because the point of the
// interface is many sessions at once; the palette answers "take me to that session" in two
// keystrokes (Ctrl/Cmd+K). Both read /api/sessions, which also counts the waiting escalations,
// so one poll serves both. No framework, no build step.
(() => {
  const api = document.body.dataset.sessionsApi;
  const triage = document.body.dataset.triageUrl;
  if (!api) return;
  const badge = document.getElementById("attention");
  const base = document.title;
  let sessions = [];
  let waiting = 0;
  let seen = Number(sessionStorage.getItem("calu-waiting") || "0");

  async function poll() {
    let data;
    try {
      const answer = await fetch(api, { headers: { accept: "application/json" } });
      data = await answer.json();
    } catch (error) {
      return;  // the server went away; the next tick tries again
    }
    sessions = data.sessions || [];
    waiting = data.waiting || 0;
    render();
  }

  function render() {
    document.title = waiting ? `(${waiting}) ${base}` : base;
    if (!badge) return;
    if (!waiting) {
      badge.textContent = "";
      badge.removeAttribute("title");
      return;
    }
    badge.innerHTML = "";
    const link = document.createElement("a");
    link.href = triage || "#";
    link.textContent = `${waiting} waiting`;
    badge.appendChild(link);
    badge.title = "sessions that cannot go on without a human";
    // A rising count while the tab is in the background is worth a notification; the triage
    // page asks for the permission, this only uses it if it was given.
    if (waiting > seen && document.hidden && window.Notification
        && Notification.permission === "granted") {
      new Notification("calu: a step waits for you", { body: `${waiting} waiting` });
    }
    seen = waiting;
    sessionStorage.setItem("calu-waiting", String(waiting));
  }

  // ---------------------------------------------------------------- the jump palette
  let palette = null;
  let chosen = 0;

  function open() {
    if (palette) return;
    palette = document.createElement("div");
    palette.id = "palette";
    const box = document.createElement("div");
    box.className = "palette-box";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "jump to a session: host, directory or state";
    const list = document.createElement("div");
    list.className = "palette-list";
    box.append(input, list);
    palette.appendChild(box);
    palette.onclick = (event) => { if (event.target === palette) close(); };
    document.body.appendChild(palette);
    chosen = 0;
    const draw = () => {
      const needle = input.value.trim().toLowerCase();
      const found = sessions.filter((s) =>
        !needle || `${s.where} ${s.state} ${s.id}`.toLowerCase().includes(needle));
      chosen = Math.max(0, Math.min(chosen, found.length - 1));
      list.innerHTML = "";
      found.slice(0, 12).forEach((s, index) => {
        const row = document.createElement("a");
        row.href = s.live || s.url;
        row.className = index === chosen ? "palette-row chosen" : "palette-row";
        const waits = s.open_escalations ? ` · ${s.open_escalations} waiting` : "";
        row.textContent = `${s.where} — ${s.state}${waits}`;
        row.onmouseenter = () => { chosen = index; draw(); };
        list.appendChild(row);
      });
      if (!found.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = sessions.length ? "no session matches" : "no session is running";
        list.appendChild(empty);
      }
      return found;
    };
    let found = draw();
    input.oninput = () => { chosen = 0; found = draw(); };
    input.onkeydown = (event) => {
      if (event.key === "ArrowDown") { chosen += 1; found = draw(); event.preventDefault(); }
      if (event.key === "ArrowUp") { chosen -= 1; found = draw(); event.preventDefault(); }
      if (event.key === "Enter" && found[chosen]) {
        location.href = found[chosen].live || found[chosen].url;
      }
      if (event.key === "Escape") close();
    };
    input.focus();
  }

  function close() {
    if (palette) { palette.remove(); palette = null; }
  }

  addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "k") {
      event.preventDefault();
      palette ? close() : open();
    }
  });

  poll();
  setInterval(poll, 4000);
})();

// The theme switch in the header: the system's theme, then light, then dark, then the system's
// again. The choice lives in this browser only (localStorage); the inline script in base.html
// applies it before the stylesheet, so a reload never flashes the other theme.
(() => {
  const button = document.getElementById("theme-switch");
  if (!button) return;
  const order = ["system", "light", "dark"];
  const labels = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" };
  const read = () => {
    try {
      const stored = localStorage.getItem("calu-theme");
      return order.includes(stored) ? stored : "system";
    } catch (e) {
      return "system";
    }
  };
  const apply = (theme) => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    button.textContent = labels[theme];
    try {
      if (theme === "system") localStorage.removeItem("calu-theme");
      else localStorage.setItem("calu-theme", theme);
    } catch (e) { /* storage blocked: the choice lasts for this page only */ }
  };
  // Kept here rather than re-read on each click: with storage blocked, re-reading would answer
  // "system" every time and the switch would never get past "light".
  let current = read();
  apply(current);
  button.addEventListener("click", () => {
    current = order[(order.indexOf(current) + 1) % order.length];
    apply(current);
  });
})();

// Copy buttons: a command line to paste into a terminal is what the session page offers, so
// copying it should not depend on selecting a line that scrolls sideways.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button.copy[data-copy]");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    button.textContent = "Copied";
    button.classList.add("done");
    setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1500);
  } catch (e) {
    button.textContent = "Select and copy";
  }
});
