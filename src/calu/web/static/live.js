// The live view of one session. Everything arrives over one WebSocket, which the calu web
// server relays from the session's own protocol (PLAN.md D34); this file only renders it and
// sends commands back. No framework and no build step, as the project's rules ask.
(() => {
  const address = document.currentScript.dataset.socket.replace(/^http/, "ws");
  const socket = new WebSocket(address);
  const el = (id) => document.getElementById(id);
  const stream = el("stream");
  const escalations = new Map();
  const agents = new Map();
  let queued = 0;

  // Once the server has explained why there is nothing to attach to, the generic "connection
  // ended" of onclose must not overwrite that explanation.
  let explained = false;
  const say = (text) => { el("note").textContent = text; };

  function line(kind, text, extra) {
    const row = document.createElement("div");
    row.className = `line ${kind}`;
    row.textContent = text;
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

  // Deltas of one message are appended to the same line, so streaming text reads as text.
  const open = new Map();
  function chunk(id, kind, text) {
    const key = `${kind}:${id || "none"}`;
    let row = open.get(key);
    if (!row) { row = line(kind, ""); open.set(key, row); }
    row.textContent += text;
    stream.scrollTop = stream.scrollHeight;
  }

  function send(method, params) {
    socket.send(JSON.stringify({ method, params: params || {} }));
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
      const row = document.createElement("p");
      row.className = "row";
      const note = document.createElement("input");
      note.type = "text";
      note.placeholder = "note for Claude (optional)";
      const allow = document.createElement("button");
      allow.className = "primary";
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
      card.append(why, rendered, row);
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
        if (!open.has(`assistant:${params.message_id || "none"}`)) line("assistant", params.text);
        break;
      case "assistant.thinking": line("thinking", params.text); break;
      case "user.input": line("user", params.text); break;
      case "tool.use": line("tool", `${params.name} ${JSON.stringify(params.input).slice(0, 300)}`); break;
      case "check.result":
        line("check", `${params.route} checker: ${params.decision} ${params.step_summary || ""}`,
             params.comment || params.reason || "");
        break;
      case "tool.result": line("result", (params.content || "").slice(0, 2000)); break;
      case "edit.diff": line("diff", `${params.path}: ${params.before.length} → ${params.after.length} characters`); break;
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
      case "turn.result": line("turn", `turn ended: ${params.subtype || "success"}`); break;
      case "log": line("log", `${params.level}: ${params.message}`); break;
      default: break;
    }
  }

  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.type === "snapshot") {
      onState(data.snapshot.state);
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
  };
  socket.onclose = () => {
    if (!explained) { say("the connection to the session ended"); onState("not attached"); }
  };
  socket.onerror = () => { if (!explained) say("the connection to the session failed"); };

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
