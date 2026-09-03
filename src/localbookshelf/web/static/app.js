// Enough JavaScript to make the shelf feel live, and no more.

(() => {
  const grid = document.getElementById("grid");
  const search = document.getElementById("search");
  const sort = document.getElementById("sort");
  const panel = document.getElementById("panel");
  const panelContent = document.getElementById("panel-content");
  const scrim = document.getElementById("scrim");
  const loadMore = document.getElementById("load-more");
  const resultCount = document.getElementById("result-count");

  let page = 1;
  let debounce;

  // -- URL state ---------------------------------------------------------

  function params() {
    return new URLSearchParams(window.location.search);
  }

  function setParams(mutate, { reload = true } = {}) {
    const next = params();
    mutate(next);
    next.delete("page");
    const qs = next.toString();
    history.replaceState(null, "", qs ? `/?${qs}` : "/");
    if (reload) refresh();
  }

  async function refresh() {
    page = 1;
    const response = await fetch(`/books?${params().toString()}`);
    grid.innerHTML = await response.text();
    resultCount.textContent = response.headers.get("X-Total-Count") || "0";
    loadMore.hidden = response.headers.get("X-Has-More") !== "1";
  }

  // -- search ------------------------------------------------------------

  if (search) {
    search.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        setParams((p) => {
          const value = search.value.trim();
          if (value) p.set("q", value);
          else p.delete("q");
        });
      }, 220);
    });
  }

  const sortDir = document.getElementById("sort-dir");

  function renderSortDir(direction) {
    if (!sortDir) return;
    sortDir.dataset.dir = direction;
    sortDir.textContent = direction === "desc" ? "↓" : "↑";
    sortDir.title =
      direction === "desc"
        ? "Descending — click for ascending"
        : "Ascending — click for descending";
  }

  // Each sort has a sensible default direction (newest first, but A-Z for
  // titles), so changing the sort resets the toggle rather than carrying the
  // previous direction over.
  const DEFAULT_DIRS = {
    added: "desc", title: "asc", author: "asc", series: "asc",
    year: "desc", modified: "desc", size: "desc",
  };

  sort?.addEventListener("change", () => {
    const direction = DEFAULT_DIRS[sort.value] || "asc";
    renderSortDir(direction);
    setParams((p) => {
      p.set("sort", sort.value);
      p.delete("dir");
    });
  });

  sortDir?.addEventListener("click", () => {
    const next = sortDir.dataset.dir === "desc" ? "asc" : "desc";
    renderSortDir(next);
    setParams((p) => p.set("dir", next));
  });

  document.getElementById("author-filter")?.addEventListener("change", (event) => {
    setParams((p) => (event.target.value ? p.set("author", event.target.value) : p.delete("author")));
  });

  document.getElementById("series-filter")?.addEventListener("change", (event) => {
    setParams((p) => (event.target.value ? p.set("series", event.target.value) : p.delete("series")));
  });

  document.getElementById("format-filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-format]");
    if (button) {
      button.classList.toggle("is-on");
      setParams((p) => {
        p.delete("format");
        document
          .querySelectorAll("#format-filters [data-format].is-on")
          .forEach((el) => p.append("format", el.dataset.format));
      });
      return;
    }
    const review = event.target.closest("#review-filter");
    if (review) {
      review.classList.toggle("is-on");
      setParams((p) => (review.classList.contains("is-on") ? p.set("review", "1") : p.delete("review")));
    }
  });

  loadMore?.addEventListener("click", async () => {
    page += 1;
    const next = params();
    next.set("page", String(page));
    const response = await fetch(`/books?${next.toString()}`);
    grid.insertAdjacentHTML("beforeend", await response.text());
    loadMore.hidden = response.headers.get("X-Has-More") !== "1";
  });

  // -- detail panel ------------------------------------------------------

  async function openBook(id, { updateHash = true } = {}) {
    const response = await fetch(`/book/${id}`);
    if (!response.ok) return;
    panelContent.innerHTML = await response.text();
    panel.hidden = false;
    panel.setAttribute("aria-hidden", "false");
    scrim.hidden = false;
    panel.scrollTop = 0;
    panel.querySelector(".panel__close")?.focus();
    // A book gets its own URL, so a shelf link can point at one.
    if (updateHash) history.replaceState(null, "", `#book-${id}`);
  }

  function closePanel() {
    panel.hidden = true;
    panel.setAttribute("aria-hidden", "true");
    scrim.hidden = true;
    if (window.location.hash.startsWith("#book-")) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }

  grid?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-book-id]");
    if (card) openBook(card.dataset.bookId);
  });

  grid?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const card = event.target.closest("[data-book-id]");
    if (card) {
      event.preventDefault();
      openBook(card.dataset.bookId);
    }
  });

  scrim?.addEventListener("click", closePanel);

  panel?.addEventListener("click", async (event) => {
    if (event.target.closest("[data-close-panel]")) return closePanel();

    if (event.target.closest("[data-edit-toggle]")) {
      const editor = panel.querySelector(".editor");
      const actions = panel.querySelector(".detail__actions");
      if (editor) {
        editor.hidden = !editor.hidden;
        if (actions) actions.style.opacity = editor.hidden ? "" : "0.5";
        if (!editor.hidden) editor.querySelector("input")?.focus();
      }
      return;
    }

    const refreshButton = event.target.closest("[data-refresh]");
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.textContent = "Refreshing…";
      await fetch(`/book/${refreshButton.dataset.refresh}/refresh`, { method: "POST" });
      await openBook(refreshButton.dataset.refresh);
      refresh();
      return;
    }

    const candidate = event.target.closest("[data-match]");
    if (candidate) {
      const body = new FormData();
      body.append("openlibrary_id", candidate.dataset.olid);
      await fetch(`/book/${candidate.dataset.match}/match`, { method: "POST", body });
      await openBook(candidate.dataset.match);
      refresh();
    }
  });

  // Save metadata without losing the panel.
  panel?.addEventListener("submit", async (event) => {
    const form = event.target.closest(".editor");
    if (!form) return;
    event.preventDefault();
    const id = form.action.match(/\/book\/(\d+)\//)?.[1];
    await fetch(form.action, { method: "POST", body: new FormData(form), redirect: "manual" });
    if (id) await openBook(id);
    refresh();
  });

  // -- keyboard ----------------------------------------------------------

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) return closePanel();
    if (event.key === "/" && document.activeElement !== search) {
      event.preventDefault();
      search?.focus();
      search?.select();
    }
  });

  // -- theme -------------------------------------------------------------
  // Three states: follow the system (default), forced dark, forced light.

  const themeButton = document.getElementById("theme-toggle");
  const THEMES = [
    { value: "auto", icon: "◐", label: "Theme: follows your system" },
    { value: "dark", icon: "●", label: "Theme: always dark" },
    { value: "light", icon: "○", label: "Theme: always light" },
  ];

  function readTheme() {
    try {
      return localStorage.getItem("theme") || "auto";
    } catch (_) {
      return "auto";
    }
  }

  function applyTheme(value) {
    const state = THEMES.find((t) => t.value === value) || THEMES[0];
    if (state.value === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = state.value;
    if (themeButton) {
      themeButton.textContent = state.icon;
      themeButton.title = state.label;
    }
    try {
      if (state.value === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", state.value);
    } catch (_) {
      /* private window; the theme just won't persist */
    }
  }

  applyTheme(readTheme());

  themeButton?.addEventListener("click", () => {
    const index = THEMES.findIndex((t) => t.value === readTheme());
    applyTheme(THEMES[(index + 1) % THEMES.length].value);
  });

  // -- scan progress -----------------------------------------------------

  const scanbar = document.getElementById("scanbar");
  const scanFill = document.getElementById("scanbar-fill");
  const scanText = document.getElementById("scanbar-text");
  const scanButton = document.getElementById("scan-button");
  let polling = null;
  let wasRunning = false;

  function renderScan(state) {
    if (state.running) {
      wasRunning = true;
      scanbar.hidden = false;
      scanButton.disabled = true;
      const phase = state.phase === "enriching"
        ? `Fetching metadata ${state.enriched}/${state.enrich_total}`
        : `Indexing ${state.processed}/${state.total_files}`;
      const percent = state.phase === "enriching"
        ? Math.round((100 * state.enriched) / Math.max(1, state.enrich_total))
        : state.percent;
      scanFill.style.width = `${percent}%`;
      scanText.textContent = state.current ? `${phase} — ${state.current}` : phase;
      return true;
    }

    scanButton.disabled = false;
    if (wasRunning) {
      wasRunning = false;
      scanFill.style.width = "100%";
      scanText.textContent = state.message || "Scan complete.";
      refresh();
      setTimeout(() => { scanbar.hidden = true; scanFill.style.width = "0"; }, 4000);
    } else {
      scanbar.hidden = true;
    }
    return false;
  }

  async function poll() {
    try {
      const state = await (await fetch("/scan/status")).json();
      if (!renderScan(state) && polling) {
        clearInterval(polling);
        polling = null;
      }
    } catch (_) {
      /* server restarting; the next tick will pick it up */
    }
  }

  function startPolling() {
    if (!polling) polling = setInterval(poll, 700);
  }

  scanButton?.addEventListener("click", async () => {
    scanButton.disabled = true;
    await fetch("/scan", { method: "POST" });
    startPolling();
  });

  // A scan may already be running when the page loads (it starts on boot).
  poll().then(() => { if (wasRunning) startPolling(); });

  // Open the book named in the URL fragment, if there is one.
  const deepLink = window.location.hash.match(/^#book-(\d+)$/);
  if (deepLink) openBook(deepLink[1], { updateHash: false });
})();
