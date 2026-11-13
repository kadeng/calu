// A small Markdown renderer for Claude's text in the live view. It builds DOM nodes and never
// sets HTML from text, so nothing Claude writes can become markup or script of the page.
// Covered: headings, paragraphs, fenced and inline code, bold, italic, links (http, https and
// mailto only), ordered and unordered lists (one level), block quotes, rules and simple tables.
// Anything else stays text. No framework and no build step, as the project's rules ask.
window.caluMarkdown = (() => {
  const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

  // Inline: `code`, **bold**, *italic* or _italic_, [text](url). Returns nodes.
  function inline(text) {
    const nodes = [];
    const pattern = /(`+)([\s\S]*?)\1|\*\*([\s\S]+?)\*\*|(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])|(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])|\[([^\]\n]+)\]\(([^)\s]+)\)/g;
    let last = 0;
    for (let found = pattern.exec(text); found; found = pattern.exec(text)) {
      if (found.index > last) nodes.push(document.createTextNode(text.slice(last, found.index)));
      let node;
      if (found[1]) {
        node = document.createElement("code");
        node.textContent = found[2];
      } else if (found[3] !== undefined) {
        node = document.createElement("strong");
        node.append(...inline(found[3]));
      } else if (found[4] !== undefined || found[5] !== undefined) {
        node = document.createElement("em");
        node.append(...inline(found[4] !== undefined ? found[4] : found[5]));
      } else if (SAFE_LINK.test(found[7])) {
        node = document.createElement("a");
        node.href = found[7];
        node.target = "_blank";
        node.rel = "noopener noreferrer";
        node.append(...inline(found[6]));
      } else {
        node = document.createTextNode(found[0]);
      }
      nodes.push(node);
      last = found.index + found[0].length;
    }
    if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
    return nodes;
  }

  function block(tag, text) {
    const node = document.createElement(tag);
    node.append(...inline(text));
    return node;
  }

  const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());

  // Blocks, line by line. Returns a fragment.
  function render(source) {
    const out = document.createDocumentFragment();
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    while (index < lines.length) {
      const current = lines[index];
      const fence = current.match(/^\s*(```+|~~~+)\s*([\w+-]*)\s*$/);
      if (fence) {
        const body = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith(fence[1])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1;  // the closing fence; a block still streaming has none yet
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        if (fence[2]) code.dataset.language = fence[2];
        code.textContent = body.join("\n");
        pre.appendChild(code);
        out.appendChild(pre);
        continue;
      }
      if (!current.trim()) { index += 1; continue; }
      const heading = current.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        out.appendChild(block(`h${Math.min(6, heading[1].length + 2)}`, heading[2]));
        index += 1;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(current)) {
        out.appendChild(document.createElement("hr"));
        index += 1;
        continue;
      }
      if (/^\s*>/.test(current)) {
        const quoted = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        const quote = document.createElement("blockquote");
        quote.appendChild(render(quoted.join("\n")));
        out.appendChild(quote);
        continue;
      }
      const item = current.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if (item) {
        const list = document.createElement(/^\d/.test(item[1]) ? "ol" : "ul");
        while (index < lines.length) {
          const next = lines[index].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
          if (next) {
            list.appendChild(block("li", next[2]));
          } else if (lines[index].trim() && /^\s{2,}/.test(lines[index]) && list.lastChild) {
            // An indented line continues the item above it.
            list.lastChild.append(" ", ...inline(lines[index].trim()));
          } else {
            break;
          }
          index += 1;
        }
        out.appendChild(list);
        continue;
      }
      if (current.includes("|") && index + 1 < lines.length &&
          /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[index + 1])) {
        const table = document.createElement("table");
        const head = table.createTHead().insertRow();
        for (const cell of cells(current)) head.appendChild(block("th", cell));
        index += 2;
        const body = table.createTBody();
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          const row = body.insertRow();
          for (const cell of cells(lines[index])) row.appendChild(block("td", cell));
          index += 1;
        }
        out.appendChild(table);
        continue;
      }
      const paragraph = [];
      while (index < lines.length && lines[index].trim() &&
             !/^\s*(```|~~~|#{1,6}\s|>|([-*+]|\d+[.)])\s)/.test(lines[index])) {
        paragraph.push(lines[index]);
        index += 1;
      }
      if (!paragraph.length) {  // a line no rule above took: keep it, never loop on it
        paragraph.push(lines[index]);
        index += 1;
      }
      const node = document.createElement("p");
      paragraph.forEach((text, at) => {
        if (at) node.appendChild(document.createElement("br"));
        node.append(...inline(text));
      });
      out.appendChild(node);
    }
    return out;
  }

  return { render };
})();
