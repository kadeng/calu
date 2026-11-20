// The live view of one session. Everything arrives over one WebSocket, which the calu web
// server relays from the session's own protocol (agent/records/PLAN.md D34); this file only renders it and
// sends commands back. No framework and no build step, as the project's rules ask.
//
//     #controls      start, interrupt, stop, what always waits for the user, the usage
//     #escalations   one card per open decision or question, each decided on its own
//     #marks         the code honesty state
//     #agents        the main agent and every subagent: state, and a click to follow one
//     #earlier       the recorded turns of the conversation (from the database)
//     #stream        what arrives, each line labelled with the subagent it belongs to
//     #usage-panel   the usage panel (`usage.panel`): limits, Claude Code's /usage, cost
//     #side          side questions (`side.ask`): asked and answered in this browser only
(() => {
  const script = document.currentScript;
  const address = script.dataset.socket.replace(/^http/, "ws");
  const pageTitle = script.dataset.title || "Live";
  const { visible, questionForm } = window.caluShared;
  // The key of this browser's login (login.js). A WebSocket cannot carry a header, so the key
  // travels as an offered subprotocol; the server answers with the other one, "calu".
  let key = "";
  try { key = localStorage.getItem("calu-key") || ""; } catch (e) { /* storage blocked */ }
  let socket = null;
  const el = (id) => document.getElementById(id);
  const stream = el("stream");
  // This script writes the tab's title itself (what waits here); calu.js leaves it alone.
  document.body.dataset.ownTitle = "1";

  const MAIN = "main";
  const escalations = new Map();   // escalation id -> {request, card, sent}
  const agents = new Map();        // agent id -> AgentInfo of the session
  const numbers = new Map();       // agent id -> 1, 2, 3 ... in the order of first sight
  const activity = new Map();      // agent id or MAIN -> what it does now, in words
  const stepCount = new Map();     // agent id or MAIN -> tool calls seen
  let following = "";              // "" shows every agent, else MAIN or an agent id
  let sessionState = "connecting";
  let attached = false;

  // Once the server has explained why there is nothing to attach to, the generic "connection
  // ended" of onclose must not overwrite that explanation.
  let explained = false;
  const say = (text) => { el("note").textContent = visible(text); };

  // ------------------------------------------------------------------ the stream

  // Whether the stream is scrolled to its end. New lines keep it there; a reader who scrolled
  // back is left where they are and gets a button that names what arrived meanwhile.
  let atEnd = true;
  let unseen = 0;
  let scrollQueued = false;
  const MAX_LINES = 6000;
  let dropped = 0;

  // Scrolls the stream to its end with the next frame (one layout for many appended lines).
  function toEnd() {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      stream.scrollTop = stream.scrollHeight;
    });
  }

  // Shows or hides the "jump to latest" button with the number of lines that arrived below.
  function showUnseen() {
    const button = el("latest");
    button.hidden = !unseen;
    button.textContent = `${unseen} new line${unseen === 1 ? "" : "s"} below · jump to latest`;
  }

  // Called after a row was added or grew: follows the end, or counts the row as unseen.
  function grew(row, isNew) {
    if (row.hidden) return;
    if (atEnd) { toEnd(); return; }
    if (isNew) { unseen += 1; showUnseen(); }
  }

  stream.addEventListener("scroll", () => {
    if (scrollQueued) return;
    atEnd = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 48;
    if (atEnd && unseen) { unseen = 0; showUnseen(); }
  });
  el("latest").onclick = () => { atEnd = true; unseen = 0; showUnseen(); toEnd(); };

  // The number a subagent is called by on this page (#1, #2 ...), given at first sight.
  function numberOf(id) {
    if (!numbers.has(id)) numbers.set(id, numbers.size + 1);
    return numbers.get(id);
  }

  // The name of an agent for a label: "main agent", or "#3 Publish the package".
  function nameOf(id, limit) {
    if (!id || id === MAIN) return "main agent";
    const info = agents.get(id);
    const described = visible((info && info.description) || id);
    const short = limit && described.length > limit ? `${described.slice(0, limit - 1)}…` : described;
    return `#${numberOf(id)} ${short}`;
  }

  // Appends a row to the stream and returns it. Rows of the oldest lines are removed beyond
  // MAX_LINES, with a note at the top that says so.
  function place(row, agentId) {
    row.dataset.agent = agentId || MAIN;
    row.hidden = Boolean(following) && row.dataset.agent !== following;
    stream.appendChild(row);
    if (stream.childElementCount > MAX_LINES) {
      const first = stream.firstElementChild;
      const note = first && first.classList.contains("dropped") ? first : null;
      const oldest = note ? note.nextElementSibling : first;
      if (oldest) oldest.remove();
      dropped += 1;
      const top = note || document.createElement("div");
      top.className = "line dropped";
      top.textContent = `${dropped} older lines are not shown here any more; the recorded history has them.`;
      if (!note) stream.prepend(top);
    }
    grew(row, true);
    return row;
  }

  // One line of the stream. `tag` is an optional badge in front of the text: the tool's name,
  // or a checker's decision with the same colours as on the other pages (grey PASS, orange
  // ESCALATE, red DENY). `agentId` names the subagent the line belongs to (none: main agent);
  // such a line starts with the subagent's label.
  function line(kind, text, extra, tag, agentId) {
    const row = document.createElement("div");
    row.className = `line ${kind}`;
    if (agentId) {
      const who = document.createElement("span");
      who.className = `who agent-c${numberOf(agentId) % 8}`;
      who.dataset.who = agentId;
      who.textContent = nameOf(agentId, 28);
      who.title = nameOf(agentId);
      row.append(who, " ");
    }
    if (tag) {
      const badge = document.createElement("span");
      badge.className = tag.className;
      badge.textContent = visible(tag.text);
      row.append(badge, " ");
    }
    const body = document.createElement("span");
    body.className = "body";
    body.textContent = visible(text);
    row.appendChild(body);
    if (extra) {
      const more = document.createElement("span");
      more.className = "muted";
      more.textContent = ` ${visible(extra)}`;
      row.appendChild(more);
    }
    return place(row, agentId);
  }

  // The part of a tool's input a person reads first: the command, the path or the pattern.
  // Anything else is shown as JSON, shortened.
  const MAIN_FIELD = ["command", "file_path", "path", "pattern", "url", "query", "description", "prompt"];
  function toolText(input) {
    const field = MAIN_FIELD.find((name) => typeof (input || {})[name] === "string");
    return field ? input[field].slice(0, 500) : JSON.stringify(input).slice(0, 300);
  }

  // Claude's text is Markdown: it is rendered as such (markdown.js builds nodes, never HTML
  // from text). The source is kept with the row, because a streamed message grows.
  const sources = new WeakMap();
  function markdown(row, source) {
    sources.set(row, source);
    const body = row.querySelector(".body");
    body.classList.add("md");
    body.replaceChildren(window.caluMarkdown.render(visible(source)));
  }

  // Deltas of one message are appended to the same line, so streaming text reads as text.
  const open = new Map();
  function chunk(id, kind, text, agentId) {
    const name = `${kind}:${id || "none"}:${agentId || MAIN}`;
    let row = open.get(name);
    if (!row) { row = line(kind, "", "", null, agentId); open.set(name, row); }
    markdown(row, (sources.get(row) || "") + text);
    grew(row, false);
  }

  // An image file Claude reads is shown small under the tool call, so that one sees what
  // Claude looked at; a click opens it in full. The server serves only image files inside
  // the session's project directory; anything else leaves no thumbnail.
  const IMAGE = /\.(png|jpe?g|gif|webp)$/i;
  function thumbnail(path, agentId) {
    if (typeof path !== "string" || !IMAGE.test(path)) return;
    const base = location.pathname.replace(/\/live\/?$/, "");
    const source = `${base}/file?path=${encodeURIComponent(path)}`;
    const link = document.createElement("a");
    link.className = "line thumb";
    link.href = source;
    link.target = "_blank";
    link.rel = "noopener";
    const image = document.createElement("img");
    image.loading = "lazy";
    image.alt = visible(path);
    image.src = source;
    image.addEventListener("error", () => link.remove());
    link.appendChild(image);
    place(link, agentId);
  }

  // What an edit changed, as the removed and the added lines between the unchanged start and
  // end of the file (two lines of context), folded under a line that names the file.
  function diff(params) {
    const before = String(params.before || "").split("\n");
    const after = String(params.after || "").split("\n");
    let head = 0;
    while (head < before.length && head < after.length && before[head] === after[head]) head += 1;
    let tail = 0;
    while (tail < before.length - head && tail < after.length - head
           && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail += 1;
    const removed = before.slice(head, before.length - tail);
    const added = after.slice(head, after.length - tail);
    const row = document.createElement("details");
    row.className = "line diff";
    const summary = document.createElement("summary");
    if (params.agent_id) {
      const who = document.createElement("span");
      who.className = `who agent-c${numberOf(params.agent_id) % 8}`;
      who.dataset.who = params.agent_id;
      who.textContent = nameOf(params.agent_id, 28);
      summary.append(who, " ");
    }
    summary.append(`${visible(params.path)}: ${removed.length} line(s) removed, ${added.length} added` +
      (params.before ? "" : " (new file)"));
    row.appendChild(summary);
    const body = document.createElement("pre");
    const put = (className, sign, texts) => {
      for (const text of texts.slice(0, 400)) {
        const part = document.createElement("span");
        part.className = className;
        part.textContent = `${sign} ${visible(text)}\n`;
        body.appendChild(part);
      }
      if (texts.length > 400) body.append(`  … ${texts.length - 400} more line(s)\n`);
    };
    put("context", " ", before.slice(Math.max(0, head - 2), head));
    put("removed", "-", removed);
    put("added", "+", added);
    put("context", " ", after.slice(after.length - tail, after.length - tail + 2));
    row.appendChild(body);
    place(row, params.agent_id);
  }

  function send(method, params) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      say(`${method} was not sent: this page is not attached to the session`);
      return false;
    }
    socket.send(JSON.stringify({ method, params: params || {} }));
    return true;
  }

  // ------------------------------------------------------------------ code honesty

  // The code honesty state is the session's, in memory: it arrives with the snapshot and with
  // every `honesty.update`. Certifying HEAD, asking for a review and clearing a mark are
  // requests to the session.
  // The honesty checker's result of one step (`check.result.honesty`, `honesty_escalation`):
  // one line per file under the step's check, tagged "honesty" and never with a decision
  // class, because the honesty checker approves or refuses nothing (agent/records/PLAN.md D224).
  function honestyResult(params, agent) {
    for (const status of params.honesty || []) {
      const dishonest = status.verdict !== "HONEST";
      const units = (status.units || []).filter((unit) => !unit.honest)
        .map((unit) => `${unit.symbol}: ${unit.reason}`).join(" · ");
      line(dishonest ? "honesty dishonest" : "honesty",
           `${status.path} ${dishonest ? "dishonest" : "honest"}`,
           dishonest ? [status.reason, units].filter(Boolean).join(" — ") : "",
           { className: `tag honesty${dishonest ? " dishonest" : ""}`, text: "honesty" }, agent);
    }
    if (params.honesty_escalation) {
      line("honesty dishonest", `the honesty checker asks a person to look: ${params.honesty_escalation}`, "",
           { className: "tag honesty dishonest", text: "honesty" }, agent);
    }
  }

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
    review.textContent = info.review_running ? "Review running…" : "Review now";
    review.disabled = Boolean(info.review_running);
    review.title = "Run an honesty review of the unreviewed and marked files";
    review.onclick = () => send("honesty.review", {});
    state.append(summary, trust, review);
    card.appendChild(state);
    if ((info.unreviewed || []).length) {
      const names = document.createElement("div");
      names.className = "muted";
      const more = (info.unreviewed_count || 0) - info.unreviewed.length;
      names.textContent = `unreviewed: ${info.unreviewed.map(visible).join(", ")}` +
        (more > 0 ? `, and ${more} more` : "");
      card.appendChild(names);
    }
    for (const mark of marks) {
      const row = document.createElement("div");
      row.className = "row";
      const text = document.createElement("span");
      text.textContent = visible(`${mark.path}${mark.symbol ? ":" + mark.symbol : ""} -- ${mark.reason}`);
      const clear = document.createElement("button");
      clear.textContent = "Clear";
      clear.title = "Override the checker: this code is honest";
      clear.onclick = () => send("honesty.clear", { mark_id: mark.id });
      row.append(text, clear);
      card.appendChild(row);
    }
    box.appendChild(card);
  }

  // ------------------------------------------------------------------ decisions

  // Writes the tab's title: what waits here comes first, so a background tab says it.
  function retitle() {
    const waits = escalations.size;
    const prefix = waits ? `(${waits}) ` : (attached ? "" : "(not attached) ");
    document.title = `${prefix}${pageTitle} · calu`;
  }

  // Sends the decision of one card and marks the card as sent; the card goes when the session
  // reports the escalation as resolved, and comes back to life if the session refuses.
  function decide(id, params) {
    const entry = escalations.get(id);
    if (!entry || entry.sent) return;
    if (!send("escalation.decide", { escalation_id: id, ...params })) {
      entry.status.textContent = "not sent: this page is not attached to the session";
      return;
    }
    entry.sent = true;
    entry.card.classList.add("sent");
    for (const control of entry.card.querySelectorAll("button, input")) control.disabled = true;
    entry.status.textContent = "sent; waiting for the session to confirm";
  }

  // Builds the card of one escalation. Cards are added and removed one by one, never rebuilt,
  // so a note that is being typed in one card survives the arrival of another.
  function addEscalation(request) {
    const id = request.escalation_id;
    if (escalations.has(id)) return;
    const card = document.createElement("div");
    card.className = "card escalation";
    card.dataset.escalation = id;
    const isQuestion = (request.questions || []).length > 0;
    const head = document.createElement("p");
    head.className = "muted escalation-head";
    const position = document.createElement("span");
    position.className = "position";
    const who = document.createElement("strong");
    who.dataset.who = request.agent_id || MAIN;
    who.textContent = request.agent_id ? `subagent ${nameOf(request.agent_id)}` : "the main agent";
    const since = request.opened_at
      ? ` · waiting since ${new Date(request.opened_at).toLocaleTimeString()}` : "";
    head.append(position, isQuestion ? " · a question of " : " · asked for ", who, since);
    if (request.agent_id) {
      const follow = document.createElement("button");
      follow.type = "button";
      follow.className = "link";
      follow.textContent = "show only this subagent";
      follow.onclick = () => follow_(request.agent_id);
      head.append(" · ", follow);
    }
    if (/^\d+$/.test(String(id))) {
      // The escalation's own page: the recent conversation and each checker's verdict.
      const why = document.createElement("a");
      why.href = `/escalations/${id}`;
      why.target = "_blank";
      why.rel = "noopener";
      why.textContent = "what the checker saw";
      head.append(" · ", why);
    }
    card.appendChild(head);
    const status = document.createElement("span");
    status.className = "muted status";
    if (isQuestion) {
      card.appendChild(questionForm(request.questions, (answer) => decide(id, { answer })));
      const raw = document.createElement("details");
      const label = document.createElement("summary");
      label.className = "muted";
      label.textContent = "the call as the checker saw it";
      const rendered = document.createElement("pre");
      rendered.textContent = visible(request.rendered || "");
      raw.append(label, rendered);
      card.append(status, raw);
    } else {
      const why = document.createElement("p");
      why.className = "why";
      why.textContent = visible(`${request.tool_name}: ${request.reason}`);
      const rendered = document.createElement("pre");
      rendered.textContent = visible(request.rendered || "");
      const row = document.createElement("p");
      row.className = "row";
      const note = document.createElement("input");
      note.type = "text";
      note.placeholder = "note for Claude (optional)";
      note.setAttribute("aria-label", "note for Claude (optional)");
      const allow = document.createElement("button");
      allow.className = "allow";
      allow.textContent = "Allow";
      const deny = document.createElement("button");
      deny.className = "danger";
      deny.textContent = "Deny";
      allow.onclick = () => decide(id, { decision: "allow", note: note.value || null });
      deny.onclick = () => decide(id, { decision: "deny", note: note.value || null });
      row.append(note, allow, deny, status);
      card.append(why, rendered, row);
    }
    escalations.set(id, { request, card, status, sent: false });
    el("escalations").appendChild(card);
    activity.set(request.agent_id || MAIN, isQuestion ? "asks you a question" : "waits for your decision");
    countEscalations();
  }

  // The user's own words in the note of `escalation.resolved`, which for a denied step is the
  // whole text Claude received: "The user denied this step." and, if the user wrote one,
  // "Note from the user: <note>" (calu.escalations; calu.tui.model.own_words does the same).
  // "" for a denial without a note; any other text unchanged.
  function ownWords(note) {
    const text = note.trim();
    const denied = "The user denied this step.";
    const prefix = "Note from the user:";
    if (!text.startsWith(denied)) return text;
    const rest = text.slice(denied.length).trim();
    return rest.startsWith(prefix) ? rest.slice(prefix.length).trim() : rest;
  }

  // Removes the card of an escalation that was decided, here or by another client.
  function dropEscalation(id) {
    const entry = escalations.get(id);
    if (!entry) return;
    entry.card.remove();
    escalations.delete(id);
    const agent = entry.request.agent_id || MAIN;
    if (![...escalations.values()].some((other) => (other.request.agent_id || MAIN) === agent)) {
      activity.set(agent, "working");
    }
    countEscalations();
  }

  // Updates what depends on the number of open cards: "decision 1 of 2" on each, the line
  // above them that says what waits and what that holds back, the title and the agent list.
  function countEscalations() {
    const entries = [...escalations.values()];
    entries.forEach((entry, index) => {
      const kind = (entry.request.questions || []).length ? "Question" : "Decision";
      entry.card.querySelector(".position").textContent =
        entries.length > 1 ? `${kind} ${index + 1} of ${entries.length}` : kind;
    });
    // Many cards at once (a decision left open for minutes makes every held step ask as
    // well): each shows less of its step, so that the list stays readable.
    el("escalations").classList.toggle("many", entries.length > 3);
    const box = el("waiting");
    box.replaceChildren();
    const decisions = entries.filter((entry) => !(entry.request.questions || []).length).length;
    if (entries.length) {
      const text = document.createElement("p");
      text.className = "notice";
      const what = [];
      if (decisions) what.push(`${decisions} decision${decisions === 1 ? "" : "s"}`);
      if (entries.length - decisions) {
        const questions = entries.length - decisions;
        what.push(`${questions} question${questions === 1 ? "" : "s"}`);
      }
      const verb = entries.length === 1 ? "waits" : "wait";
      text.textContent = `${what.join(" and ")} ${verb} for you. ` + (decisions
        ? "While a decision is open, calu approves no other step that needs a check: every " +
          "other agent stops at its next such step and goes on when all decisions here are made."
        : "A question holds nothing back: the other agents keep working.");
      box.appendChild(text);
    }
    retitle();
    renderAgents();
  }

  // ------------------------------------------------------------------ agents

  // Shows only one agent's lines in the stream ("" shows every agent again).
  function follow_(id) {
    following = following === id ? "" : id;
    for (const row of stream.children) {
      row.hidden = Boolean(following) && row.dataset.agent !== following && !row.classList.contains("dropped");
    }
    const label = el("following");
    label.replaceChildren();
    if (following) {
      const all = document.createElement("button");
      all.type = "button";
      all.className = "link";
      all.textContent = "show every agent";
      all.onclick = () => follow_(following);
      label.append(`· only ${following === MAIN ? "the main agent" : `subagent ${nameOf(following)}`} · `, all);
    }
    renderAgents();
    atEnd = true; unseen = 0; showUnseen(); toEnd();
  }

  // What the agent list says about an agent that has an open card: a decision outranks a
  // question, because a decision holds the other agents back.
  function waitWords(id) {
    const own = [...escalations.values()].filter((entry) => (entry.request.agent_id || MAIN) === id);
    return own.some((entry) => !(entry.request.questions || []).length)
      ? "waits for your decision" : "asks you a question";
  }

  // One entry of the agent list: a button that follows the agent, with its state in words.
  function agentChip(id, label, state, className) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `agent ${className}`;
    chip.dataset.agent = id;
    chip.setAttribute("aria-pressed", String(following === id));
    const name = document.createElement("span");
    name.className = "agent-name";
    name.textContent = label;
    const what = document.createElement("span");
    what.className = "agent-state";
    what.textContent = state;
    chip.append(name, what);
    chip.title = `${label} — ${state}. Click to show only its lines; click again for every agent.`;
    chip.onclick = () => follow_(id);
    return chip;
  }

  // The list of agents above the stream, and the "who reads it" choice of the message form.
  // Agents that wait for the user come first, then the working ones; the finished ones are
  // folded away once there are more than four of them.
  function renderAgents() {
    const select = el("to");
    const chosen = select.value;
    select.replaceChildren();
    const main = document.createElement("option");
    main.value = MAIN;
    main.textContent = "the main agent";
    select.appendChild(main);
    for (const [id, info] of agents) {
      if (!info.background || info.status !== "running") continue;
      const option = document.createElement("option");
      option.value = id;
      option.textContent = `subagent ${nameOf(id, 50)}`;
      select.appendChild(option);
    }
    select.value = [...select.options].some((option) => option.value === chosen) ? chosen : MAIN;

    const box = el("agents");
    box.replaceChildren();
    if (!agents.size) { box.hidden = true; return; }
    box.hidden = false;
    const waitingFor = new Set([...escalations.values()].map((entry) => entry.request.agent_id || MAIN));
    const live = [];
    const done = [];
    for (const [id, info] of agents) {
      const running = info.status === "running";
      const steps = stepCount.get(id) || 0;
      const state = waitingFor.has(id)
        ? waitWords(id)
        : running ? (activity.get(id) || "working") + (info.background ? " (background)" : "")
        : info.status;
      const className = waitingFor.has(id) ? "waits" : running ? "running" : info.status;
      const chip = agentChip(id, nameOf(id, 44), `${state} · ${steps} step${steps === 1 ? "" : "s"}`, className);
      (running || waitingFor.has(id) ? live : done).push([waitingFor.has(id) ? 0 : 1, chip]);
    }
    live.sort((a, b) => a[0] - b[0]);
    // Each subagent is counted once: one that waits for the user is not also counted as
    // running, so that the three numbers add up to the number of subagents.
    const waitingAgents = [...agents.keys()].filter((id) => waitingFor.has(id)).length;
    const working = [...agents.entries()]
      .filter(([id, info]) => info.status === "running" && !waitingFor.has(id)).length;
    const title = document.createElement("p");
    title.className = "muted agents-title";
    title.textContent = `${agents.size} subagent${agents.size === 1 ? "" : "s"}: ${working} running` +
      (waitingAgents ? `, ${waitingAgents} waiting for you` : "") +
      `, ${agents.size - working - waitingAgents} finished. Each line of the stream starts with its subagent's number.`;
    const list = document.createElement("div");
    list.className = "agent-list";
    const mainState = waitingFor.has(MAIN) ? waitWords(MAIN) : sessionState;
    list.appendChild(agentChip(MAIN, "main agent", mainState, waitingFor.has(MAIN) ? "waits" : "running"));
    for (const [_order, chip] of live) list.appendChild(chip);
    box.append(title, list);
    if (done.length > 4) {
      const folded = document.createElement("details");
      folded.className = "agents-done";
      folded.open = done.some(([_order, chip]) => chip.dataset.agent === following);
      const summary = document.createElement("summary");
      summary.className = "muted";
      summary.textContent = `${done.length} finished subagents`;
      const inner = document.createElement("div");
      inner.className = "agent-list";
      for (const [_order, chip] of done) inner.appendChild(chip);
      folded.append(summary, inner);
      box.appendChild(folded);
    } else {
      for (const [_order, chip] of done) list.appendChild(chip);
    }
  }

  // A subagent's description can arrive after its first lines: the labels are written again.
  function relabel() {
    for (const who of document.querySelectorAll("[data-who]")) {
      const id = who.dataset.who;
      if (id === MAIN) continue;
      if (who.classList.contains("who")) {
        who.textContent = nameOf(id, 28);
        who.title = nameOf(id);
      } else {
        who.textContent = `subagent ${nameOf(id)}`;
      }
    }
  }

  // ------------------------------------------------------------------ state and controls

  let queuedCount = 0;  // messages waiting for Claude, as of the last `chat.queue`
  const BUSY = ["running", "checking", "tool_call", "escalated"];
  const ENDED = ["stopped", "failed"];
  const STATE_WORDS = {
    connecting: "Connecting to the session…",
    "not attached": "",
    starting: "The session is starting.",
    idle: "Claude waits for your message.",
    running: "Claude is working.",
    checking: "A checker is judging a step.",
    tool_call: "A tool call is running.",
    escalated: "The session waits for you: see the card above the stream.",
    stopped: "No Claude session is running here. Start one with “Start”, or open the recorded history.",
    failed: "The Claude session ended with an error. “Start” starts a new one.",
  };

  // Shows the session's state and enables exactly the controls that can act in it.
  function onState(state) {
    if (attached && ENDED.includes(state) && !ENDED.includes(sessionState) && sessionState !== "connecting") {
      // Said in the stream as well: what was queued for Claude is gone with the session.
      line("turn", `the Claude session ${state === "failed" ? "ended with an error" : "was stopped"}` +
        (queuedCount ? `; ${queuedCount} queued message(s) were not delivered` : ""));
    }
    sessionState = state;
    el("state").textContent = state;
    el("state").className = `state ${state.replace(/\s+/g, "-")}`;
    el("words").textContent = attached ? (STATE_WORDS[state] || "") : "";
    el("start").disabled = !attached || !ENDED.includes(state);
    el("model").disabled = el("start").disabled;
    el("effort").disabled = el("start").disabled;
    el("interrupt").disabled = !attached || !BUSY.includes(state);
    el("stop").disabled = !attached || ENDED.includes(state);
    for (const id of ["send", "steer", "text", "images", "to"]) {
      el(id).disabled = !attached || ENDED.includes(state);
    }
    el("steer").disabled = el("steer").disabled || !BUSY.includes(state);
    for (const box of document.querySelectorAll("#supervision input")) box.disabled = !attached;
    el("permission-mode").disabled = !attached || ENDED.includes(state) || !permissionMode;
    el("usage-open").disabled = !attached;
    sideControls();
    retitle();
    if (agents.size) renderAgents();
  }

  // The three categories whose steps always wait for the user (`auto_escalate`).
  function renderSupervision(settings) {
    for (const box of document.querySelectorAll("#supervision input")) {
      box.checked = Boolean((settings || {})[box.value]);
    }
  }
  for (const box of document.querySelectorAll("#supervision input")) {
    box.onchange = () => {
      const settings = {};
      for (const other of document.querySelectorAll("#supervision input")) {
        settings[other.value] = other.checked;
      }
      send("auto_escalate.set", settings);
    };
  }

  // Claude Code's permission mode as the session reports it (`permission_mode.changed`). The
  // select shows the reported mode only: a choice is sent, and the select follows the event
  // that comes back, so a refused choice falls back to the mode Claude Code runs in.
  let permissionMode = null;
  function renderPermission(mode, client) {
    permissionMode = mode || null;
    const select = el("permission-mode");
    for (const option of [...select.options]) {
      if (option.dataset.reported) option.remove();
    }
    if (mode && ![...select.options].some((option) => option.value === mode)) {
      // A mode calu does not offer (plan mode, entered by a tool call) is shown, not offered.
      const option = new Option(mode, mode);
      option.dataset.reported = "1";
      option.disabled = true;
      select.add(option);
    }
    select.value = mode || "";
    el("permission-note").textContent = !mode ? "not reported yet"
      : client ? `switched by ${visible(client)}` : "";
    select.disabled = !attached || ENDED.includes(sessionState) || !mode;
  }
  el("permission-mode").onchange = () => {
    const wanted = el("permission-mode").value;
    el("permission-mode").value = permissionMode || "";
    if (wanted && wanted !== permissionMode) send("permission_mode.set", { mode: wanted });
  };

  // What the session knows about the usage: the context window and the account's limits.
  let limits = [];
  let context = null;
  function renderUsage() {
    const parts = [];
    if (context) {
      parts.push(`context ${Math.round(context.tokens / 1000)}k of ${Math.round(context.max_tokens / 1000)}k tokens (${Math.round(context.percent)} %)`);
    }
    for (const limit of limits) {
      if (limit.utilization === null || limit.utilization === undefined) continue;
      const name = (limit.type || "usage").replace(/_/g, " ");
      const warn = limit.status === "allowed" ? "" : ` — ${limit.status.replace(/_/g, " ")}`;
      parts.push(`${name} limit ${Math.round(limit.utilization * 100)} % used${warn}`);
    }
    el("usage").textContent = parts.join(" · ");
  }

  // The recorded turns of the conversation, from the database: what a fork took over, and
  // what happened before this page (or the session's process) started. Folded by default.
  function renderHistory(history) {
    const box = el("earlier");
    box.replaceChildren();
    // A turn that has not ended is the one running now: the stream below shows it.
    const turns = ((history && history.turns) || []).filter((turn) => turn.ended_at);
    box.hidden = !turns.length;
    if (!turns.length) return;
    const summary = document.createElement("summary");
    summary.textContent = `Recorded turns of this conversation (${turns.length})`;
    box.appendChild(summary);
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "As stored in the database: your messages, one line per tool call and " +
      "Claude's answer, for the turns that have ended. The stream below shows what arrives now.";
    box.appendChild(note);
    for (const turn of turns) {
      const block = document.createElement("div");
      block.className = "earlier-turn";
      const head = document.createElement("p");
      head.className = "muted";
      head.textContent = `Turn ${turn.index}`;
      block.appendChild(head);
      for (const text of turn.user_inputs || []) {
        const said = document.createElement("p");
        said.className = "user";
        said.textContent = visible(text);
        block.appendChild(said);
      }
      const steps = turn.steps || [];
      if (steps.length) {
        const count = document.createElement("p");
        count.className = "muted";
        count.textContent = `${steps.length} tool call${steps.length === 1 ? "" : "s"}`;
        block.appendChild(count);
      }
      const answer = turn.answer || turn.answer_narration || "";
      if (answer) {
        const said = document.createElement("div");
        said.className = "md";
        said.appendChild(window.caluMarkdown.render(visible(answer)));
        block.appendChild(said);
      }
      box.appendChild(block);
    }
  }

  // ------------------------------------------------------------------ events

  // "Bash of subagent <id> waits: ..." is what the session logs for a step that is held back
  // by an open escalation: the agent list says so, and the stream keeps one line per agent.
  const HELD = /^\S+ of (?:subagent (\S+)|the main agent) waits: /;
  const heldLines = new Map();
  const answeredCalls = new Set();  // tool_use_id of every question answered while attached

  function onEvent(method, params) {
    const agent = params.agent_id || null;
    switch (method) {
      case "session.state": onState(params.state); break;
      // A fragment of thinking streams into a thinking line, which `assistant.thinking`
      // then completes; it is not shown as Claude's text.
      case "assistant.delta":
        chunk(params.message_id, params.thinking ? "thinking" : "assistant", params.text, agent);
        break;
      case "assistant.text":
        if (!open.has(`assistant:${params.message_id || "none"}:${agent || MAIN}`)) {
          markdown(line("assistant", "", "", null, agent), params.text);
        }
        break;
      case "assistant.thinking": {
        const streamed = open.get(`thinking:${params.message_id || "none"}:${agent || MAIN}`);
        if (streamed) {
          markdown(streamed, params.text);
          open.delete(`thinking:${params.message_id || "none"}:${agent || MAIN}`);
        } else {
          line("thinking", params.text, "", null, agent);
        }
        break;
      }
      case "user.input": {
        const source = params.source && params.source !== "user"
          ? (params.source === "system" ? "calu or Claude Code" : "another session") : "You";
        const to = params.to && params.to !== MAIN ? ` → subagent ${nameOf(params.to, 40)}` : "";
        line("user", params.text, "", { className: "tag", text: `${source}${to}` });
        // What the user attached, small; a click shows it large.
        // (A browser does not open a `data:` address in a new tab, so the click enlarges it
        // in place instead.)
        // One row holds all images of the message.
        if ((params.images || []).length) {
          const holder = document.createElement("div");
          holder.className = "line thumb set";
          for (const image of params.images) {
            const shown = picture(image, "");
            shown.title = "click to enlarge";
            shown.tabIndex = 0;
            shown.onclick = () => shown.classList.toggle("large");
            shown.onkeydown = (event) => { if (event.key === "Enter") shown.classList.toggle("large"); };
            holder.appendChild(shown);
          }
          place(holder, null);
        }
        break;
      }
      case "tool.use":
        stepCount.set(agent || MAIN, (stepCount.get(agent || MAIN) || 0) + 1);
        activity.set(agent || MAIN, `runs ${params.name}`);
        line("tool", toolText(params.input), "", { className: "tag", text: params.name }, agent);
        if (params.name === "Read") thumbnail((params.input || {}).file_path, agent);
        if (agents.size) renderAgents();
        break;
      case "check.result":
        line("check", `${params.route} checker: ${params.step_summary || ""}`,
             params.comment || params.reason || "",
             { className: `decision ${String(params.decision).toLowerCase()}`, text: params.decision },
             agent);
        honestyResult(params, agent);
        break;
      case "tool.result":
        if (answeredCalls.has(params.tool_use_id)) {
          line("result", (params.content || "").slice(0, 2000), "", null, agent);
          break;
        }
        line(params.is_error ? "result failed" : "result", (params.content || "").slice(0, 2000),
             (params.content || "").length > 2000 ? `… (${params.content.length} characters; the recorded history has the summary)` : "",
             params.is_error ? { className: "decision deny", text: "failed" } : null, agent);
        break;
      case "edit.diff": diff(params); break;
      case "honesty.update": renderHonesty(params); break;
      case "escalation.request":
        addEscalation(params);
        break;
      case "escalation.resolved": {
        const entry = escalations.get(params.escalation_id);
        const about = entry ? entry.request.agent_id : null;
        dropEscalation(params.escalation_id);
        for (const row of heldLines.values()) row.remove();
        heldLines.clear();
        for (const [id, words] of activity) if (words.startsWith("held")) activity.set(id, "working");
        if (entry && (entry.request.questions || []).length && params.decision !== "cancelled") {
          // An answered question travels as a refusal of the call (D35): it is shown as the
          // answer it is, and the call's "failed" result that follows is left out.
          answeredCalls.add(entry.request.tool_use_id);
          line("check", "the question was answered; Claude receives the answer as the result of its call", "",
               { className: "decision allow", text: "answered" }, about);
          break;
        }
        const words = ownWords(params.note || "");
        line("check", params.decision === "cancelled"
               ? "the open decision was withdrawn (stop or interrupt); the step did not run"
               : `a person decided the escalation${words ? ` — note: ${words}` : ""}`, "",
             { className: `decision ${params.decision === "allow" ? "allow" : "deny"}`, text: params.decision }, about);
        break;
      }
      case "chat.queue": {
        const messages = params.messages || [];
        if (!ENDED.includes(sessionState)) queuedCount = messages.length;
        const box = el("queue");
        box.replaceChildren();
        if (messages.length) {
          const head = document.createElement("p");
          head.className = "muted";
          head.textContent = `${messages.length} message${messages.length === 1 ? "" : "s"} waiting for Claude:`;
          box.appendChild(head);
          for (const message of messages) {
            const row = document.createElement("p");
            row.className = "queued";
            const how = message.delivery === "inject" ? "steer" : "queued";
            const to = message.to && message.to !== MAIN ? ` → subagent ${nameOf(message.to, 30)}` : "";
            row.textContent = `${how}${to}: ${visible(message.text).slice(0, 200)}`;
            box.appendChild(row);
          }
        }
        break;
      }
      case "agents.update":
        agents.clear();
        for (const info of params.agents || []) { agents.set(info.agent_id, info); numberOf(info.agent_id); }
        relabel();
        renderAgents();
        break;
      case "auto_escalate.changed": renderSupervision(params.settings); break;
      case "permission_mode.changed": renderPermission(params.mode, params.client); break;
      case "usage.update": limits = params.rate_limits || []; renderUsage(); break;
      case "turn.result": {
        // Background tasks still running: Claude Code continues by itself when they end (D158).
        const waiting = params.background_tasks
          ? `; ${params.background_tasks} background task(s) still running, Claude continues when they finish`
          : "";
        line("turn", `turn ended: ${params.subtype || "success"}${waiting}`);
        send("usage.get", {});
        // The turn is recorded now: the list of recorded turns is read again.
        setTimeout(() => { if (attached) send("session.history", { limit: 1 }); }, 1500);
        break;
      }
      case "log": {
        const held = HELD.exec(params.message || "");
        if (held && params.level === "info") {
          const who = held[1] || MAIN;
          activity.set(who, "held until the open decision is made");
          if (heldLines.has(who)) break;
          heldLines.set(who, line("log held", "held: its next step waits until the open decision is made", "", null, held[1] || null));
          if (agents.size) renderAgents();
          break;
        }
        line(`log ${params.level}`, `${params.level}: ${params.message}`);
        break;
      }
      default: break;
    }
  }

  // What the page shows of the session, emptied before a snapshot is applied: attaching again
  // replays the events the session still holds, and they must not appear twice.
  function reset() {
    stream.replaceChildren();
    open.clear();
    for (const entry of escalations.values()) entry.card.remove();
    escalations.clear();
    agents.clear();
    activity.clear();
    stepCount.clear();
    heldLines.clear();
    dropped = 0;
    unseen = 0;
    atEnd = true;
    showUnseen();
  }

  function onResult(method, result) {
    if (method === "session.history") renderHistory(result);
    else if (method === "honesty.review") {
      say(result.ran
        ? `the honesty review ran${result.problem ? `: ${result.problem}` : "; its result is in the code honesty card"}`
        : `the honesty review did not run: ${result.problem || "nothing to review"}`);
    } else if (method === "honesty.trust") {
      const what = result.commit_id ? `commit ${String(result.commit_id).slice(0, 12)}` : `file ${result.path || ""}`;
      say(result.recorded === false ? `${what} was trusted already` : `${what} is recorded as trusted`);
    }
    else if (method === "usage.get") {
      context = result.context || null;
      if ((result.rate_limits || []).length) limits = result.rate_limits;
      renderUsage();
    }
    else if (method === "usage.panel") renderUsagePanel(result);
    else if (method === "side.ask") sideAnswered(result.answer || "");
  }

  function onMessage(message) {
    const data = JSON.parse(message.data);
    if (data.type === "snapshot") {
      reset();
      retryAfter = 1000;
      attached = true;
      say("attached");
      for (const info of data.snapshot.agents || []) { agents.set(info.agent_id, info); numberOf(info.agent_id); }
      for (const request of data.snapshot.escalations || []) addEscalation(request);
      onState(data.snapshot.state);
      renderHonesty(data.snapshot.honesty);
      renderSupervision(data.snapshot.auto_escalate);
      renderPermission(data.snapshot.permission_mode, null);
      limits = data.snapshot.rate_limits || [];
      renderUsage();
      countEscalations();
      send("session.history", { limit: 1 });
      send("usage.get", {});
      // No events.subscribe from here: the server subscribed when it attached, and every event
      // since then is relayed. The browser only sends commands.
    } else if (data.type === "event") {
      onEvent(data.method, data.params);
    } else if (data.type === "result") {
      onResult(data.method, data.result || {});
    } else if (data.type === "refused") {
      if (data.method === "usage.get" || data.method === "session.history") return;
      if (data.method === "usage.panel") { usagePanelRefused(data.message); return; }
      if (data.method === "side.ask") { sideRefused(data.message); return; }
      say(`${data.method} was refused: ${data.message}`);
      if (data.method === "escalation.decide") {
        // The decision did not take: the cards that were sent can be answered again.
        for (const entry of escalations.values()) {
          if (!entry.sent) continue;
          entry.sent = false;
          entry.card.classList.remove("sent");
          for (const control of entry.card.querySelectorAll("button, input")) control.disabled = false;
          entry.status.textContent = visible(`refused: ${data.message}`);
        }
      }
    } else if (data.type === "closed") {
      attached = false;
      explained = true;
      onState("not attached");
      say(data.message);
    }
  }

  // The connection to calu web can end without an explanation: calu web was restarted, a
  // tunnel dropped, the laptop slept. Then the page says that it is not attached and tries
  // again, after 1 s, 2 s, 4 s ... up to 30 s. An explained end ("closed": the session is gone
  // or serves no clients) is final for this connection; "Try again" attaches anew.
  let retryAfter = 1000;
  let retryTimer = null;
  function attach() {
    retryTimer = null;
    el("retry").hidden = true;
    socket = key ? new WebSocket(address, ["calu", `calu.key.${key}`]) : new WebSocket(address);
    socket.onmessage = onMessage;
    socket.onclose = () => {
      attached = false;
      if (explained) {
        el("retry").hidden = false;
        onState("not attached");
        return;
      }
      const seconds = Math.round(retryAfter / 1000);
      onState("not attached");
      say(`The connection to calu web ended; trying again in ${seconds} s. What is shown below ` +
          "may be out of date. If this stays, check that `calu web` still runs (and the SSH " +
          "tunnel, if this page comes through one); after a restart of `calu web`, sign in again.");
      retryTimer = setTimeout(attach, retryAfter);
      retryAfter = Math.min(retryAfter * 2, 30000);
      signedOut();
    };
  }
  // A restarted calu web knows no browser any more: trying to attach again would be refused
  // for ever. One request tells that case apart (status 401); the page then stops trying and
  // says that a new sign-in is needed, which a reload leads to.
  async function signedOut() {
    let status = 0;
    try {
      status = (await fetch(document.body.dataset.sessionsApi, { headers: { accept: "application/json" } })).status;
    } catch (error) { return; }  // calu web is not reachable at all: keep trying
    if (status !== 401 || attached) return;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    explained = true;
    say("calu web was restarted and this browser is not signed in to it any more. ");
    const again = document.createElement("a");
    again.href = location.href;
    again.textContent = "Sign in again";
    el("note").appendChild(again);
  }

  el("retry").onclick = () => {
    explained = false;
    if (retryTimer) clearTimeout(retryTimer);
    say("attaching…");
    onState("connecting");
    attach();
  };
  onState("connecting");
  attach();

  // ------------------------------------------------------------------ the usage panel

  // One request (`usage.panel`) gives the limits calu stored from Claude Code's reports, each
  // with when it came, and Claude Code's own /usage, which the session reads without a turn:
  // nothing of it is recorded or shown to the checkers.
  const LIMIT_WORDS = {
    five_hour: "5-hour", seven_day: "weekly", seven_day_opus: "weekly Opus",
    seven_day_sonnet: "weekly Sonnet", overage: "extra usage",
  };
  function ago(iso) {
    if (!iso) return "time not known";
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    return `${Math.floor(seconds / 86400)} days ago`;
  }
  function limitRow(limit, stamped) {
    const row = document.createElement("p");
    const name = LIMIT_WORDS[limit.type] || limit.type || "limit";
    const used = limit.utilization === null || limit.utilization === undefined
      ? (limit.status || "").replace(/_/g, " ")
      : `${Math.round(limit.utilization * 100)} % used`;
    const resets = limit.resets_at ? `, resets ${new Date(limit.resets_at).toLocaleString()}` : "";
    row.textContent = `${name}: ${used}${resets}` + (stamped ? ` · reported ${ago(limit.reported_at)}` : "");
    if (limit.status && limit.status !== "allowed") row.className = "problem";
    return row;
  }
  function panelSection(title, note) {
    const head = document.createElement("h3");
    head.textContent = title;
    if (note) {
      const small = document.createElement("span");
      small.className = "muted";
      small.textContent = ` ${note}`;
      head.appendChild(small);
    }
    return head;
  }
  function renderUsagePanel(result) {
    const body = el("usage-body");
    body.replaceChildren(panelSection("Limits reported by Claude Code"));
    if (!(result.limits || []).length) {
      const none = document.createElement("p");
      none.className = "muted";
      none.textContent = "Not reported yet by Claude Code. It reports a limit in the stream of " +
        "a running session; an account without limits (an API key, Bedrock) reports none.";
      body.appendChild(none);
    }
    for (const limit of result.limits || []) body.appendChild(limitRow(limit, true));
    if ((result.account_limits || []).length) {
      body.appendChild(panelSection(`Your plan now${result.subscription ? ` (${result.subscription})` : ""}`));
      for (const limit of result.account_limits) body.appendChild(limitRow(limit, false));
    } else if (result.account_limits_problem) {
      // get_usage failed or answered in an unknown shape: the panel says why.
      body.appendChild(panelSection("Your plan now"));
      const why = document.createElement("p");
      why.className = "muted";
      why.textContent = visible(result.account_limits_problem);
      body.appendChild(why);
    }
    body.appendChild(panelSection("Claude Code's /usage", "read without a turn; not recorded, not shown to the checkers"));
    const text = document.createElement("pre");
    text.id = "usage-text";
    text.textContent = visible(result.claude_usage || result.claude_usage_problem || "not available");
    if (!result.claude_usage) text.className = "muted";
    body.appendChild(text);
    body.appendChild(panelSection("This session"));
    const cost = document.createElement("p");
    cost.textContent = result.cost_usd === null || result.cost_usd === undefined
      ? "cost: no turn has ended yet"
      : `cost $${Number(result.cost_usd).toFixed(4)} · ${result.num_turns} turn${result.num_turns === 1 ? "" : "s"}`;
    body.appendChild(cost);
    if (result.context) {
      const fill = document.createElement("p");
      fill.textContent = `context ${result.context.tokens.toLocaleString()} of ` +
        `${result.context.max_tokens.toLocaleString()} tokens (${Math.round(result.context.percent)} %)`;
      body.appendChild(fill);
      context = result.context;
    }
    if ((result.limits || []).length) limits = result.limits;
    renderUsage();
  }
  function usagePanelRefused(message) {
    const failed = document.createElement("p");
    failed.className = "problem";
    failed.textContent = visible(`The session did not answer: ${message}`);
    el("usage-body").replaceChildren(failed);
  }
  function openUsagePanel() {
    if (!attached) return;
    const asking = document.createElement("p");
    asking.className = "muted";
    asking.textContent = "asking the session…";
    el("usage-body").replaceChildren(asking);
    const panel = el("usage-panel");
    if (!panel.open) {
      if (typeof panel.showModal === "function") panel.showModal(); else panel.setAttribute("open", "");
    }
    send("usage.panel", {});
  }
  el("usage").onclick = openUsagePanel;
  el("usage").onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openUsagePanel(); }
  };
  el("usage-open").onclick = openUsagePanel;
  el("usage-again").onclick = openUsagePanel;
  el("usage-close").onclick = () => {
    const panel = el("usage-panel");
    if (typeof panel.close === "function") panel.close(); else panel.removeAttribute("open");
  };

  // ------------------------------------------------------------------ side questions

  // Asked with `side.ask`; Claude answers from the conversation, without tools. The answers
  // stay in this page (kept in memory, sent along as `history` so that a follow-up can refer
  // to them); the session keeps none, and no other client sees them. One question at a time.
  const MAX_SIDE_HISTORY = 20;
  const sideHistory = [];
  let sideAsking = null;  // the question being answered, or null
  function sideControls() {
    const usable = attached && !ENDED.includes(sessionState) && sessionState !== "starting";
    el("side-text").disabled = !usable;
    el("side-ask").disabled = !usable || sideAsking !== null;
  }
  function sideEntry(question, answer, pending) {
    const entry = document.createElement("div");
    entry.className = "side-entry";
    const asked = document.createElement("p");
    asked.className = "user";
    asked.textContent = visible(question);
    const said = document.createElement("div");
    said.className = pending ? "muted" : "md";
    if (pending) said.textContent = "Claude is answering…";
    else said.appendChild(window.caluMarkdown.render(visible(answer)));
    entry.append(asked, said);
    return entry;
  }
  function renderSide() {
    const box = el("side-answers");
    box.replaceChildren();
    for (const exchange of sideHistory) box.appendChild(sideEntry(exchange.question, exchange.answer, false));
    if (sideAsking !== null) box.appendChild(sideEntry(sideAsking, "", true));
    el("side-clear").hidden = !sideHistory.length;
    sideControls();
  }
  function ask() {
    const question = el("side-text").value.trim();
    if (!question || sideAsking !== null) return;
    const history = sideHistory.slice(-MAX_SIDE_HISTORY);
    if (!send("side.ask", { question, history })) return;
    sideAsking = question;
    el("side-text").value = "";
    el("side-note").textContent = "";
    renderSide();
  }
  function sideAnswered(answer) {
    if (sideAsking === null) return;
    sideHistory.push({ question: sideAsking, answer });
    sideAsking = null;
    renderSide();
  }
  function sideRefused(message) {
    if (sideAsking !== null) el("side-text").value = sideAsking;
    sideAsking = null;
    el("side-note").textContent = visible(`not answered: ${message}`);
    renderSide();
  }
  el("side-ask").onclick = ask;
  el("side-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.ctrlKey) { event.preventDefault(); ask(); }
  });
  el("side-clear").onclick = () => { sideHistory.length = 0; renderSide(); };

  // ------------------------------------------------------------------ the message form

  // Images for the next message: chosen with the file input or pasted into the text. They
  // are sent with the message as base64 and reach Claude with it; the limits are the ones
  // the session enforces (protocol/messages.py).
  const MAX_IMAGES = 4;
  const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
  const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  let chosenImages = [];
  const problem = (text) => { el("attach-problem").textContent = text; };
  function picture(image, className) {
    const shown = document.createElement("img");
    shown.className = className;
    shown.alt = "attached image";
    shown.src = `data:${image.media_type};base64,${image.data}`;
    return shown;
  }
  function renderAttached() {
    const box = el("attached");
    box.replaceChildren();
    chosenImages.forEach((image, index) => {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.title = "remove this image";
      remove.setAttribute("aria-label", `remove image ${index + 1}`);
      remove.className = "attached";
      remove.appendChild(picture(image, "thumb-small"));
      remove.append(" ×");
      remove.onclick = () => { chosenImages.splice(index, 1); renderAttached(); };
      box.appendChild(remove);
    });
  }
  function attachFiles(files) {
    problem("");
    let pending = chosenImages.length;
    for (const file of files) {
      if (!IMAGE_TYPES.includes(file.type)) { problem(`Not attached: ${file.name || "that"} is no PNG, JPEG, GIF or WebP image.`); continue; }
      if (file.size > MAX_IMAGE_BYTES) { problem(`Not attached: ${file.name || "the image"} is larger than 5 MB.`); continue; }
      if (pending >= MAX_IMAGES) { problem(`A message takes at most ${MAX_IMAGES} images; the others were not attached.`); break; }
      pending += 1;
      const reader = new FileReader();
      reader.onload = () => {
        if (chosenImages.length >= MAX_IMAGES) return;
        chosenImages.push({ media_type: file.type, data: String(reader.result).split(",")[1] });
        renderAttached();
      };
      reader.readAsDataURL(file);
    }
  }
  el("images").onchange = (event) => { attachFiles(event.target.files); event.target.value = ""; };
  el("text").addEventListener("paste", (event) => {
    const files = [...(event.clipboardData || {}).files || []].filter((file) => file.type.startsWith("image/"));
    if (files.length) { event.preventDefault(); attachFiles(files); }
  });

  const message = (delivery) => {
    const text = el("text").value.trim();
    if (!text) { problem(chosenImages.length ? "Write a line with the image: a message needs text." : "Write a message first."); return; }
    if (chosenImages.length && el("to").value !== MAIN) { problem("An image can be sent to the main agent only."); return; }
    if (!send("chat.send", { text, delivery, to: el("to").value, images: chosenImages })) {
      problem("Not sent: this page is not attached to the session. Your text is still here.");
      return;
    }
    problem("");
    el("text").value = "";
    chosenImages = [];
    renderAttached();
  };
  el("send").onclick = () => message("queued");
  el("steer").onclick = () => message("inject");
  el("interrupt").onclick = () => send("interrupt", {});
  el("stop").onclick = () => {
    if (window.confirm("Stop the Claude session? The conversation can be continued later; open decisions are cancelled.")) {
      send("session.stop", {});
    }
  };
  el("start").onclick = () => {
    const params = {};
    if (el("model").value) params.model = el("model").value;
    if (el("effort").value) params.effort = el("effort").value;
    send("session.start", params);
  };
  el("text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      message(event.shiftKey && !el("steer").disabled ? "inject" : "queued");
    }
  });
})();
