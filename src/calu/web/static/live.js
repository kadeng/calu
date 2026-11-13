// The live view of one session. Everything arrives over one WebSocket, which the calu web
// server relays from the session's own protocol (PLAN.md D34); this file only renders it and
// sends commands back. No framework and no build step, as the project's rules ask.
(() => {
  const address = document.currentScript.dataset.socket.replace(/^http/, "ws");
  // The key of this browser's login (login.js). A WebSocket cannot carry a header, so the key
  // travels as an offered subprotocol; the server answers with the other one, "calu".
  let key = "";
  try { key = localStorage.getItem("calu-key") || ""; } catch (e) { /* storage blocked */ }
  let socket = null;
  const el = (id) => document.getElementById(id);
  const stream = el("stream");
  const escalations = new Map();
  const agents = new Map();
  let queued = 0;

  // Once the server has explained why there is nothing to attach to, the generic "connection
  // ended" of onclose must not overwrite that explanation.
  let explained = false;
  const say = (text) => { el("note").textContent = text; };

  // `tag` is an optional badge in front of the text: the tool's name, or a checker's decision
  // with the same colours as on the other pages (grey PASS, orange ESCALATE, red DENY).
  function line(kind, text, extra, tag) {
    const row = document.createElement("div");
    row.className = `line ${kind}`;
    if (tag) {
      const badge = document.createElement("span");
      badge.className = tag.className;
      badge.textContent = tag.text;
      row.append(badge, " ");
    }
    row.append(text);
    if (extra) {
      const more = document.createElement("span");
      more.className = "muted";
      more.textContent = ` ${extra}`;
      row.appendChild(more);
    }
    stream.appendChild(row);
    stream.scrollTop = stream.scrollHeight;
    return row;
  }

  // The part of a tool's input a person reads first: the command, the path or the pattern.
  // Anything else is shown as JSON, shortened.
  const MAIN_FIELD = ["command", "file_path", "path", "pattern", "url", "query", "description", "prompt"];
  function toolText(input) {
    const field = MAIN_FIELD.find((key) => typeof (input || {})[key] === "string");
    return field ? input[field].slice(0, 500) : JSON.stringify(input).slice(0, 300);
  }

  // Claude's text is Markdown: it is rendered as such (markdown.js builds nodes, never HTML
  // from text). The source is kept with the row, because a streamed message grows.
  const sources = new WeakMap();
  function markdown(row, source) {
    sources.set(row, source);
    row.classList.add("md");
    row.replaceChildren(window.caluMarkdown.render(source));
  }

  // Deltas of one message are appended to the same line, so streaming text reads as text.
  const open = new Map();
  function chunk(id, kind, text) {
    const key = `${kind}:${id || "none"}`;
    let row = open.get(key);
    if (!row) { row = line(kind, ""); open.set(key, row); }
    markdown(row, (sources.get(row) || "") + text);
    stream.scrollTop = stream.scrollHeight;
  }

  // An image file Claude reads is shown small under the tool call, so that one sees what
  // Claude looked at; a click opens it in full. The server serves only image files inside
  // the session's project directory; anything else leaves no thumbnail.
  const IMAGE = /\.(png|jpe?g|gif|webp)$/i;
  function thumbnail(path) {
    if (typeof path !== "string" || !IMAGE.test(path)) return;
    const base = location.pathname.replace(/\/live\/?$/, "");
    const address = `${base}/file?path=${encodeURIComponent(path)}`;
    const link = document.createElement("a");
    link.className = "line thumb";
    link.href = address;
    link.target = "_blank";
    link.rel = "noopener";
    const image = document.createElement("img");
    image.loading = "lazy";
    image.alt = path;
    image.src = address;
    image.addEventListener("error", () => link.remove());
    link.appendChild(image);
    stream.appendChild(link);
    stream.scrollTop = stream.scrollHeight;
  }

  function send(method, params) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      say(`${method} was not sent: this page is not attached to the session`);
      return;
    }
    socket.send(JSON.stringify({ method, params: params || {} }));
  }

  // The code honesty state is the session's, in memory: it arrives with the snapshot and with
  // every `honesty.update`. Certifying HEAD, asking for a review and clearing a mark are
  // requests to the session.
  function renderHonesty(info) {
    const box = el("marks");
    box.replaceChildren();
    if (!info || !info.mode || info.mode === "off") return;
    const marks = info.marks || [];
    const card = document.createElement("div");
    card.className = "card";
    card.id = "honesty";
    const title = document.createElement("h3");
    title.textContent = `Code honesty (${info.mode}): ${info.clean ? "clean" : "not clean"}`;
    card.appendChild(title);
    const state = document.createElement("div");
    state.className = "row";
    const summary = document.createElement("span");
    const baseline = info.baseline
      ? `baseline ${info.baseline.slice(0, 12)} (${info.baseline_source})`
      : "no baseline";
    const unreviewed = info.unreviewed_all
      ? "every file unreviewed"
      : `${info.unreviewed_count || 0} unreviewed file(s)`;
    summary.textContent = [
      baseline,
      `${marks.length} open mark(s)`,
      unreviewed,
      info.review_required ? "review required" : "",
      info.review_running ? "a review is running" : "",
    ].filter(Boolean).join(" · ");
    const trust = document.createElement("button");
    trust.textContent = "Trust HEAD";
    trust.title = "Certify the commit HEAD points at as honest";
    trust.onclick = () => {
      if (window.confirm("Certify the commit HEAD points at as honest?")) {
        send("honesty.trust", { rev: "HEAD" });
      }
    };
    const review = document.createElement("button");
    review.textContent = "Review now";
    review.title = "Run an honesty review of the unreviewed and marked files";
    review.onclick = () => send("honesty.review", {});
    state.append(summary, trust, review);
    card.appendChild(state);
    if ((info.unreviewed || []).length) {
      const names = document.createElement("div");
      names.className = "muted";
      const more = (info.unreviewed_count || 0) - info.unreviewed.length;
      names.textContent = `unreviewed: ${info.unreviewed.join(", ")}` +
        (more > 0 ? `, and ${more} more` : "");
      card.appendChild(names);
    }
    for (const mark of marks) {
      const row = document.createElement("div");
      row.className = "row";
      const text = document.createElement("span");
      text.textContent = `${mark.path}${mark.symbol ? ":" + mark.symbol : ""} -- ${mark.reason}`;
      const clear = document.createElement("button");
      clear.textContent = "Clear";
      clear.title = "Override the checker: this code is honest";
      clear.onclick = () => send("honesty.clear", { mark_id: mark.id });
      row.append(text, clear);
      card.appendChild(row);
    }
    box.appendChild(card);
  }

  function renderEscalations() {
    const box = el("escalations");
    box.innerHTML = "";
    for (const [id, request] of escalations) {
      const card = document.createElement("div");
      card.className = "card escalation";
      const why = document.createElement("p");
      why.textContent = `${request.tool_name}: ${request.reason}`;
      const rendered = document.createElement("pre");
      rendered.textContent = request.rendered || "";
      card.append(why, rendered);
      // A question of the agent is answered, not allowed or denied: its options become buttons
      // and the answer travels back as the result of the call (PLAN.md D35).
      if ((request.questions || []).length) {
        for (const question of request.questions) {
          const asked = document.createElement("p");
          asked.textContent = question.question || question.header || "a question";
          card.appendChild(asked);
          const choices = document.createElement("p");
          choices.className = "row";
          for (const option of question.options || []) {
            const button = document.createElement("button");
            button.className = "option";
            button.textContent = option.label;
            button.title = option.description || "";
            button.onclick = () => {
              send("escalation.decide", { escalation_id: id, answer: option.label });
              escalations.delete(id);
              renderEscalations();
            };
            choices.appendChild(button);
          }
          const free = document.createElement("input");
          free.type = "text";
          free.placeholder = "or answer in your own words, then Enter";
          free.onkeydown = (event) => {
            if (event.key !== "Enter" || !free.value.trim()) return;
            send("escalation.decide", { escalation_id: id, answer: free.value.trim() });
            escalations.delete(id);
            renderEscalations();
          };
          choices.appendChild(free);
          card.appendChild(choices);
        }
        box.appendChild(card);
        continue;
      }
      const row = document.createElement("p");
      row.className = "row";
      const note = document.createElement("input");
      note.type = "text";
      note.placeholder = "note for Claude (optional)";
      const allow = document.createElement("button");
      allow.className = "allow";
      allow.textContent = "Allow";
      const deny = document.createElement("button");
      deny.className = "danger";
      deny.textContent = "Deny";
      const decide = (decision) => () => {
        send("escalation.decide", { escalation_id: id, decision, note: note.value || null });
        escalations.delete(id);
        renderEscalations();
      };
      allow.onclick = decide("allow");
      deny.onclick = decide("deny");
      row.append(note, allow, deny);
      card.appendChild(row);
      box.appendChild(card);
    }
    document.title = escalations.size ? `(${escalations.size}) waiting · calu` : "Live · calu";
  }

  function renderAgents() {
    const select = el("to");
    const chosen = select.value;
    select.innerHTML = '<option value="main">the main agent</option>';
    for (const [id, info] of agents) {
      if (!info.background || info.status !== "running") continue;
      const option = document.createElement("option");
      option.value = id;
      option.textContent = `subagent: ${info.description || id}`;
      select.appendChild(option);
    }
    select.value = chosen;
  }

  function onState(state) {
    el("state").textContent = state;
    el("state").className = `state ${state}`;
  }

  function onEvent(method, params) {
    switch (method) {
      case "session.state": onState(params.state); break;
      case "assistant.delta": chunk(params.message_id, "assistant", params.text); break;
      case "assistant.text":
        if (!open.has(`assistant:${params.message_id || "none"}`)) {
          markdown(line("assistant", ""), params.text);
        }
        break;
      case "assistant.thinking": line("thinking", params.text); break;
      case "user.input": line("user", params.text); break;
      case "tool.use":
        line("tool", toolText(params.input), "", { className: "tag", text: params.name });
        if (params.name === "Read") thumbnail((params.input || {}).file_path);
        break;
      case "check.result":
        line("check", `${params.route} checker: ${params.step_summary || ""}`,
             params.comment || params.reason || "",
             { className: `decision ${String(params.decision).toLowerCase()}`, text: params.decision });
        break;
      case "tool.result": line("result", (params.content || "").slice(0, 2000)); break;
      case "edit.diff": line("diff", `${params.path}: ${params.before.length} → ${params.after.length} characters`); break;
      case "honesty.update": renderHonesty(params); break;
      case "escalation.request":
        escalations.set(params.escalation_id, params);
        renderEscalations();
        break;
      case "escalation.resolved":
        escalations.delete(params.escalation_id);
        renderEscalations();
        line("check", `escalation ${params.escalation_id}: ${params.decision} ${params.note || ""}`);
        break;
      case "chat.queue":
        queued = (params.messages || []).length;
        el("queue").textContent = queued ? `${queued} message(s) waiting for Claude` : "";
        break;
      case "agents.update":
        agents.clear();
        for (const info of params.agents || []) agents.set(info.agent_id, info);
        renderAgents();
        break;
      case "turn.result": {
        // Background tasks still running: Claude Code continues by itself when they end (D158).
        const waiting = params.background_tasks
          ? `; ${params.background_tasks} background task(s) still running, Claude continues when they finish`
          : "";
        line("turn", `turn ended: ${params.subtype || "success"}${waiting}`);
        break;
      }
      case "log": line("log", `${params.level}: ${params.message}`); break;
      default: break;
    }
  }

  // What the page shows of the session, emptied before a snapshot is applied: attaching again
  // replays the events the session still holds, and they must not appear twice.
  function reset() {
    stream.replaceChildren();
    open.clear();
    escalations.clear();
    agents.clear();
  }

  function onMessage(message) {
    const data = JSON.parse(message.data);
    if (data.type === "snapshot") {
      reset();
      retryAfter = 1000;
      onState(data.snapshot.state);
      renderHonesty(data.snapshot.honesty);
      say("attached");
      for (const request of data.snapshot.escalations || []) {
        escalations.set(request.escalation_id, request);
      }
      for (const info of data.snapshot.agents || []) agents.set(info.agent_id, info);
      renderEscalations();
      renderAgents();
      // No events.subscribe from here: the server subscribed when it attached, and every event
      // since then is relayed. The browser only sends commands.
    } else if (data.type === "event") {
      onEvent(data.method, data.params);
    } else if (data.type === "refused") {
      say(`${data.method} was refused: ${data.message}`);
    } else if (data.type === "closed") {
      say(data.message);
      explained = true;
      onState("not attached");
    }
  }

  // The connection to calu web can end without an explanation: calu web was restarted, a
  // tunnel dropped, the laptop slept. Then the page says that it is not attached and tries
  // again, after 1 s, 2 s, 4 s ... up to 30 s. An explained end ("closed": the session is gone
  // or serves no clients) is final; trying again would only repeat the explanation.
  let retryAfter = 1000;
  function attach() {
    socket = key ? new WebSocket(address, ["calu", `calu.key.${key}`]) : new WebSocket(address);
    socket.onmessage = onMessage;
    socket.onclose = () => {
      if (explained) return;
      const seconds = Math.round(retryAfter / 1000);
      say(`the connection to calu web ended; trying again in ${seconds} s. If this stays, ` +
          "open the address that `calu web` printed again.");
      onState("not attached");
      setTimeout(attach, retryAfter);
      retryAfter = Math.min(retryAfter * 2, 30000);
    };
  }
  attach();

  const message = (delivery) => {
    const text = el("text").value.trim();
    if (!text) return;
    send("chat.send", { text, delivery, to: el("to").value });
    el("text").value = "";
  };
  el("send").onclick = () => message("queued");
  el("steer").onclick = () => message("inject");
  el("interrupt").onclick = () => send("interrupt", {});
  el("stop").onclick = () => send("session.stop", {});
  el("start").onclick = () => {
    const params = {};
    if (el("model").value) params.model = el("model").value;
    if (el("effort").value) params.effort = el("effort").value;
    send("session.start", params);
  };
  el("text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      message(event.shiftKey ? "inject" : "queued");
    }
  });
})();
