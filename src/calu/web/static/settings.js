// The settings page (templates/settings.html): which pane is shown, the search, what is unsaved,
// "Reset", and editors that grow with their text. Without this file every pane is shown, one
// below the other; saving needs JavaScript in any case, because every change carries the
// login's key (calu.js adds it).
(() => {
  const root = document.getElementById("st");
  const form = document.getElementById("st-form");
  const panes = [...root.querySelectorAll(".st-pane")];
  const links = [...root.querySelectorAll(".st-nav a[data-pane]")];
  const rows = [...root.querySelectorAll(".st-row")];
  const search = document.getElementById("st-search");
  const results = document.getElementById("st-results");
  const empty = document.getElementById("st-empty");
  const saveBar = document.getElementById("st-save");
  const paneOf = (id) => panes.find((pane) => pane.id === id);
  let active = "";

  // ---- editors grow with their text, up to the CSS max-height
  const fit = (area) => {
    if (!area.offsetParent) return; // hidden: sized when its pane is shown
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight + 2}px`;
  };
  const fitAll = () => root.querySelectorAll(".st-editor textarea").forEach(fit);

  // ---- one pane at a time
  const show = (id, { scroll = true } = {}) => {
    if (!paneOf(id)) id = panes[0].id;
    active = id;
    for (const pane of panes) pane.classList.toggle("is-active", pane.id === id);
    for (const link of links) link.setAttribute("aria-current", String(link.dataset.pane === id));
    links.find((link) => link.dataset.pane === id)?.scrollIntoView({ block: "nearest", inline: "nearest" });
    try { sessionStorage.setItem("calu-settings-pane", id); } catch (e) { /* storage blocked */ }
    fitAll();
    if (scroll) scrollTo(0, 0);
  };
  const remembered = () => {
    try { return sessionStorage.getItem("calu-settings-pane") || ""; } catch (e) { return ""; }
  };
  const fromHash = () => decodeURIComponent(location.hash.slice(1));
  addEventListener("hashchange", () => { clearSearch(); show(fromHash()); });
  root.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-pane], a[data-goto]");
    if (!link) return;
    // Also when the hash does not change: leave the search and go to the pane.
    clearSearch();
    show(link.dataset.pane || link.dataset.goto);
  });

  // ---- search across every pane
  const applySearch = () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const searching = words.length > 0;
    root.classList.toggle("is-searching", searching);
    let shown = 0;
    for (const row of rows) {
      const match = !searching || words.every((word) => row.dataset.search.includes(word));
      row.hidden = !match;
      shown += match ? 1 : 0;
    }
    for (const group of root.querySelectorAll(".st-group")) {
      group.hidden = searching && !group.querySelector(".st-row:not([hidden])");
    }
    for (const pane of panes) {
      const any = Boolean(pane.querySelector(".st-row:not([hidden])"));
      pane.classList.toggle("has-match", searching && any);
      links.find((link) => link.dataset.pane === pane.id)?.classList.toggle("is-nomatch", searching && !any);
    }
    results.hidden = !searching || shown === 0;
    results.textContent = `${shown} setting${shown === 1 ? "" : "s"} match “${search.value.trim()}”`;
    empty.hidden = !searching || shown !== 0;
    fitAll();
  };
  const clearSearch = () => {
    if (!search.value) return;
    search.value = "";
    applySearch();
  };
  search.addEventListener("input", applySearch);
  document.getElementById("st-clear").addEventListener("click", () => { clearSearch(); search.focus(); });

  // ---- what is unsaved
  const controls = [...form.elements].filter((el) => el.name && el.type !== "hidden" && el.type !== "submit");
  const valueOf = (el) => (el.type === "checkbox" ? String(el.checked) : el.value);
  const initial = new Map(controls.map((el) => [el, valueOf(el)]));
  const pendingReset = (row) => form.querySelector(`input[name="_reset"][value="${CSS.escape(row.dataset.field)}"]`);
  const refresh = () => {
    const dirtyPanes = new Map();
    let dirty = 0;
    for (const el of controls) {
      const row = el.closest(".st-row");
      const changed = valueOf(el) !== initial.get(el) || Boolean(row && pendingReset(row));
      row?.classList.toggle("is-dirty", changed);
      if (!changed) continue;
      dirty += 1;
      const pane = el.closest(".st-pane");
      dirtyPanes.set(pane.id, pane.dataset.title);
    }
    for (const link of links) link.querySelector(".st-dot").hidden = !dirtyPanes.has(link.dataset.pane);
    document.getElementById("st-dirty-count").textContent = dirty;
    document.getElementById("st-dirty-word").textContent = dirty === 1 ? "change" : "changes";
    document.getElementById("st-dirty-where").textContent = dirty ? `in ${[...dirtyPanes.values()].join(", ")}` : "";
    saveBar.hidden = dirty === 0;
  };
  form.addEventListener("input", (event) => {
    // Typing into a field takes back a pending "Reset" of it.
    const row = event.target.closest(".st-row");
    if (row && event.isTrusted) pendingReset(row)?.remove();
    if (event.target.matches(".st-editor textarea")) fit(event.target);
    refresh();
  });
  form.addEventListener("change", refresh);

  // ---- model fields: a list of known models that fills the text field, which stays free
  const syncModel = (select) => {
    const input = document.getElementById(select.dataset.modelFor);
    const known = [...select.options].some((option) => option.value && option.value === input.value);
    select.value = known ? input.value : "";
    // The text field shows only for a model the list does not have; for a listed one the
    // string behind the entry is shown instead.
    input.hidden = known;
    const id = select.parentElement.querySelector("[data-model-id]");
    id.textContent = input.value;
    id.hidden = !known;
  };
  const modelSelects = [...root.querySelectorAll("select[data-model-for]")];
  for (const select of modelSelects) {
    const input = document.getElementById(select.dataset.modelFor);
    select.addEventListener("change", () => {
      if (select.value) {
        input.value = select.value;
        syncModel(select);
      } else {
        input.hidden = false;
        select.parentElement.querySelector("[data-model-id]").hidden = true;
        input.focus();
        input.select();
      }
      refresh();
    });
    // Typing a listed model by hand selects it in the list when the field is left.
    input.addEventListener("change", () => syncModel(select));
    syncModel(select);
  }
  const syncModels = () => modelSelects.forEach(syncModel);

  // ---- "Reset": show the inherited value and ask the server to drop the stored one
  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-reset]");
    if (!button) return;
    const row = button.closest(".st-row");
    const el = controls.find((control) => control.name === button.dataset.reset);
    if (el.type === "checkbox") el.checked = row.dataset.base === "true";
    else el.value = row.dataset.base;
    if (!pendingReset(row)) {
      const marker = document.createElement("input");
      marker.type = "hidden";
      marker.name = "_reset";
      marker.value = row.dataset.field;
      form.append(marker);
    }
    if (el.matches("textarea")) fit(el);
    syncModels();
    refresh();
  });

  document.getElementById("st-discard").addEventListener("click", () => {
    form.reset();
    form.querySelectorAll('input[name="_reset"]').forEach((marker) => marker.remove());
    fitAll();
    syncModels();
    refresh();
  });

  // ---- saving, leaving, the scope and the keyboard
  let leaving = false;
  form.addEventListener("submit", () => {
    leaving = true;
    // The pane to come back to: a redirect keeps the fragment of the URL that was posted to.
    form.action = `${form.action.split("#")[0]}#${active}`;
  });
  addEventListener("beforeunload", (event) => {
    if (!leaving && !saveBar.hidden) { event.preventDefault(); event.returnValue = ""; }
  });
  document.getElementById("st-scope").addEventListener("change", (event) => {
    location.href = `${event.target.value}#${active}`;
  });
  addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "s") {
      event.preventDefault();
      if (!saveBar.hidden) form.requestSubmit(document.getElementById("st-submit"));
    } else if (event.key === "/" && !event.target.closest("input, textarea, select")) {
      event.preventDefault();
      search.focus();
    } else if (event.key === "Escape" && event.target === search) {
      clearSearch();
    }
  });
  addEventListener("resize", fitAll);

  show(root.dataset.open || fromHash() || remembered(), { scroll: false });
  refresh();
})();
