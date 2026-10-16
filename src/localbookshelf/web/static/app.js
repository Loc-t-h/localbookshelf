// Enough JavaScript to make the shelf feel live, and no more.

(() => {
  const grid = document.getElementById("grid");
  const search = document.getElementById("search");
  const sort = document.getElementById("sort");
  const panel = document.getElementById("panel");
  const panelContent = document.getElementById("panel-content");
  const scrim = document.getElementById("scrim");
  const loadMore = document.getElementById("load-more");
  const sentinel = document.getElementById("load-sentinel");
  const azStrip = document.getElementById("az");
  const resultCount = document.getElementById("result-count");

  let page = 1;
  let debounce;
  let loading = false;
  let autoLoad = true;
  // Bumped whenever the filters change. A page request that lands after a
  // newer refresh has its generation checked and is dropped, so results from
  // an abandoned search can never be appended to the shelf you are looking at.
  let generation = 0;

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
    generation += 1;
    const mine = generation;
    const response = await fetch(`/books?${params().toString()}`);
    const html = await response.text();
    if (mine !== generation) return;
    grid.innerHTML = html;
    resultCount.textContent = response.headers.get("X-Total-Count") || "0";
    syncStrip(response.headers.get("X-Initials"));
    autoLoad = true;
    failed = false;
    hasMore = response.headers.get("X-Has-More") === "1";
    renderPager();
    syncStripVisibility();
    markCurrentLetter();
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

  // -- paging ------------------------------------------------------------

  // Matches the observer's rootMargin, so the explicit follow-up below and the
  // observer agree on what "near the bottom" means.
  const LOAD_MARGIN = 600;

  // Auto-load is the normal path. The button only appears where there is no
  // IntersectionObserver, or after a page has failed and needs a retry.
  const AUTO = "IntersectionObserver" in window;

  // Seeded from what the server rendered, before any of this hides anything.
  let hasMore = loadMore ? !loadMore.hidden : false;
  let failed = false;

  function nearViewport(el) {
    const rect = el.getBoundingClientRect();
    return rect.top - LOAD_MARGIN < window.innerHeight && rect.bottom + LOAD_MARGIN > 0;
  }

  function renderPager() {
    if (sentinel) sentinel.hidden = !(hasMore && AUTO && !failed);
    if (!loadMore) return;
    loadMore.hidden = !(hasMore && (!AUTO || failed));
    loadMore.disabled = loading;
    loadMore.textContent = loading
      ? "Loading…"
      : failed
        ? "Load more — retry"
        : "Load more";
  }

  async function loadNextPage() {
    if (loading || !hasMore) return;
    loading = true;
    renderPager();

    const mine = generation;
    const next = params();
    next.set("page", String(page + 1));
    try {
      const response = await fetch(`/books?${next.toString()}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const html = await response.text();
      // The filters moved on while this was in flight; drop it rather than
      // appending books that do not match what is on screen.
      if (mine !== generation) return;
      // Advanced only on success, so a failed page is retried and not skipped.
      page += 1;
      grid.insertAdjacentHTML("beforeend", html);
      hasMore = response.headers.get("X-Has-More") === "1";
      // IntersectionObserver reports *changes* in intersection, not a standing
      // state. On a tall window the sentinel can still be in view after this
      // append, in which case no further callback ever comes and the shelf
      // stalls -- so carry on explicitly. The timeout lets `loading` clear in
      // the finally block below first.
      if (autoLoad && hasMore && sentinel && nearViewport(sentinel)) {
        setTimeout(loadNextPage, 0);
      }
    } catch (_) {
      // Stop auto-loading so a persistent failure cannot spin, and surface the
      // button as an explicit retry.
      autoLoad = false;
      failed = true;
    } finally {
      loading = false;
      renderPager();
    }
  }

  loadMore?.addEventListener("click", () => {
    autoLoad = true;
    failed = false;
    loadNextPage();
  });

  // Auto-load on reaching the bottom. The sentinel is hidden once the last page
  // has arrived, and a hidden element never intersects, so the observer falls
  // silent at the end of the shelf without needing to be disconnected. The
  // margin starts the fetch a screen early, so the next row of covers is
  // usually already there by the time you scroll to it.
  if (sentinel && AUTO) {
    const observer = new IntersectionObserver(
      (entries) => {
        if (autoLoad && entries.some((entry) => entry.isIntersecting)) loadNextPage();
      },
      { rootMargin: `${LOAD_MARGIN}px 0px` },
    );
    observer.observe(sentinel);
  }

  // Takes the button out of the layout where auto-load works, and starts the
  // spinner if the first page already reaches the bottom of the window.
  renderPager();

  // -- A-Z strip ---------------------------------------------------------
  // Scrolls the shelf to an initial rather than filtering it. The catch is
  // that the shelf is paginated, so the letter you asked for is usually not in
  // the DOM yet: pages are pulled in until it arrives.

  const topbar = document.querySelector(".topbar");

  // Changing the sort re-renders the grid over fetch, not the page, so the
  // strip's own visibility has to be kept in step here. Reading the select
  // rather than the URL also covers the default, where there is no sort param.
  function syncStripVisibility() {
    if (!azStrip) return;
    const current = sort?.value || params().get("sort") || "added";
    azStrip.hidden = current !== "title";
  }

  function syncStrip(header) {
    if (!azStrip || header === null) return;
    const counts = new Map(
      header
        .split(",")
        .filter(Boolean)
        .map((pair) => {
          const [ch, n] = pair.split(":");
          return [ch, Number(n) || 0];
        }),
    );
    azStrip.querySelectorAll("[data-letter]").forEach((button) => {
      const ch = button.dataset.letter;
      const n = counts.get(ch) || 0;
      button.disabled = n === 0;
      button.setAttribute("aria-disabled", n === 0 ? "true" : "false");
      button.title = n ? `Skip to ${ch} (${n} book${n === 1 ? "" : "s"})` : `nothing under ${ch}`;
    });
  }

  function firstCardFor(ch) {
    return grid?.querySelector(`[data-initial="${CSS.escape(ch)}"]`) || null;
  }

  function scrollToCard(card) {
    // Clear the sticky header, or the row lands underneath it.
    const offset = (topbar?.getBoundingClientRect().height || 0) + 12;
    const top = card.getBoundingClientRect().top + window.scrollY - offset;
    window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }

  async function jumpTo(ch) {
    let card = firstCardFor(ch);
    // Pull pages in until the letter shows up. `guard` is paranoia: without
    // it a page that silently stops advancing would spin this forever.
    let guard = 0;
    while (!card && hasMore && guard < 60) {
      guard += 1;
      const before = page;
      await loadNextPage();
      if (page === before) {
        // A scroll-triggered load was already in flight; let it land.
        await new Promise((resolve) => setTimeout(resolve, 80));
      }
      card = firstCardFor(ch);
    }
    if (card) scrollToCard(card);
  }

  azStrip?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-letter]");
    if (button && !button.disabled) jumpTo(button.dataset.letter);
  });

  // Highlight whichever letter you are currently among, the way Plex does, so
  // the strip reads as a position rather than a set of buttons.
  let spyQueued = false;

  function markCurrentLetter() {
    spyQueued = false;
    if (!azStrip || !grid) return;
    const edge = (topbar?.getBoundingClientRect().height || 0) + 16;
    let current = null;
    for (const card of grid.querySelectorAll("[data-initial]")) {
      if (card.getBoundingClientRect().top > window.innerHeight) break;
      if (card.getBoundingClientRect().bottom >= edge) {
        current = card.dataset.initial;
        break;
      }
    }
    azStrip.querySelectorAll("[data-letter]").forEach((button) => {
      button.classList.toggle("is-on", button.dataset.letter === current);
    });
  }

  if (azStrip) {
    const queueSpy = () => {
      if (spyQueued) return;
      spyQueued = true;
      requestAnimationFrame(markCurrentLetter);
    };
    window.addEventListener("scroll", queueSpy, { passive: true });
    window.addEventListener("resize", queueSpy, { passive: true });
    syncStripVisibility();
    markCurrentLetter();
  }

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

    const confirmButton = event.target.closest("[data-confirm]");
    if (confirmButton) {
      confirmButton.disabled = true;
      confirmButton.textContent = "Keeping…";
      await fetch(`/book/${confirmButton.dataset.confirm}/confirm`, { method: "POST" });
      await openBook(confirmButton.dataset.confirm);
      refresh();
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
