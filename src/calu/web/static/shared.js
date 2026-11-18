// What several pages of the web interface need: untrusted text made safe to read, and the form
// that answers a question of the agent. Loaded before the page scripts (base.html), without
// `defer`, so that `window.caluShared` exists when they run. No framework and no build step.
window.caluShared = (() => {
  // Unicode's bidirectional control characters are invisible and change the order in which
  // the text after them is drawn, so a command or a file name could be displayed in another
  // order than it is stored and run in. Each is shown as its name instead ("⟦RLO⟧"). Letters
  // of right-to-left scripts are not touched. The server does the same for the pages it
  // renders (`calu.web.app.visible`), with the same names.
  const BIDI_NAMES = {
    0x061c: "ALM", 0x200e: "LRM", 0x200f: "RLM", 0x202a: "LRE", 0x202b: "RLE", 0x202c: "PDF",
    0x202d: "LRO", 0x202e: "RLO", 0x2066: "LRI", 0x2067: "RLI", 0x2068: "FSI", 0x2069: "PDI",
  };
  const BIDI = /[؜‎‏‪-‮⁦-⁩]/g;

  // Returns `text` with every bidirectional control character replaced by "⟦NAME⟧".
  function visible(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(BIDI, (found) => `⟦${BIDI_NAMES[found.codePointAt(0)] || "?"}⟧`);
  }

  // The one answer text the protocol takes for a set of questions, composed as every other
  // client composes it (`calu.protocol.answers.compose_answer`): one question is answered with
  // its bare text, several with one line `<header>: <answers>` per answered question.
  // `answers` holds, per question, the list of chosen texts.
  function composeAnswer(questions, answers) {
    const lines = [];
    questions.forEach((question, index) => {
      const chosen = (answers[index] || []).map((text) => text.trim()).filter(Boolean);
      if (!chosen.length) return;
      const header = typeof question.header === "string" && question.header.trim()
        ? question.header.trim() : `Question ${index + 1}`;
      lines.push([header, chosen.join(", ")]);
    });
    if (questions.length === 1) return lines.length ? lines[0][1] : "";
    return lines.map(([header, text]) => `${header}: ${text}`).join("\n");
  }

  // Builds the form for the questions of one `AskUserQuestion` call and returns its element.
  // Each question shows its options as toggle buttons (one may be chosen, or several when the
  // question allows it) and a field for an answer in the user's own words, which replaces the
  // chosen options of that question. `onAnswer(text)` is called once with the composed answer:
  // at the click on an option when the call asks exactly one single-choice question, else at
  // "Send the answer" (or Enter in a field), which needs an answer to every question.
  function questionForm(questions, onAnswer) {
    const form = document.createElement("div");
    form.className = "questions";
    const quick = questions.length === 1 && !questions[0].multiSelect;
    const chosen = questions.map(() => new Set());
    const fields = [];
    const problem = document.createElement("span");
    problem.className = "muted";
    const collect = () => questions.map((_question, index) => {
      const own = fields[index].value.trim();
      return own ? [own] : [...chosen[index]];
    });
    const submit = () => {
      const answers = collect();
      const missing = answers.findIndex((given) => !given.length);
      if (missing >= 0) {
        problem.textContent = questions.length === 1
          ? "choose an option or write an answer first"
          : `question ${missing + 1} of ${questions.length} has no answer yet`;
        return;
      }
      onAnswer(composeAnswer(questions, answers));
    };
    questions.forEach((question, index) => {
      const asked = document.createElement("p");
      asked.className = "question";
      const many = question.multiSelect ? " (several may be chosen)" : "";
      asked.textContent = visible(question.question || question.header || "a question") + many;
      const choices = document.createElement("p");
      choices.className = "row";
      const buttons = [];
      for (const option of question.options || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "option";
        button.textContent = visible(option.label);
        button.title = visible(option.description || "");
        button.setAttribute("aria-pressed", "false");
        button.onclick = () => {
          if (quick) { onAnswer(option.label); return; }
          const set = chosen[index];
          if (set.has(option.label)) set.delete(option.label);
          else {
            if (!question.multiSelect) set.clear();
            set.add(option.label);
          }
          (question.options || []).forEach((other, at) => {
            buttons[at].setAttribute("aria-pressed", String(set.has(other.label)));
          });
          problem.textContent = "";
        };
        buttons.push(button);
        choices.appendChild(button);
      }
      const free = document.createElement("input");
      free.type = "text";
      free.className = "own-answer";
      free.placeholder = quick ? "or answer in your own words, then Enter" : "or in your own words";
      free.onkeydown = (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        submit();
      };
      fields.push(free);
      choices.appendChild(free);
      form.append(asked, choices);
    });
    if (!quick) {
      const row = document.createElement("p");
      row.className = "row";
      const sendButton = document.createElement("button");
      sendButton.type = "button";
      sendButton.className = "primary send-answer";
      sendButton.textContent = "Send the answer";
      sendButton.onclick = submit;
      row.append(sendButton, problem);
      form.appendChild(row);
    } else {
      form.appendChild(problem);
    }
    return form;
  }

  return { visible, composeAnswer, questionForm };
})();
