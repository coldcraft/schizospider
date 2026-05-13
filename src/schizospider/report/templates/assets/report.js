(() => {
  let DATA = null;
  let filtered = [];

  const $ = (sel) => document.querySelector(sel);
  const grid = $("#grid");
  const q = $("#q");
  const fOn = $("#f-on");
  const fOff = $("#f-off");
  const fSkipped = $("#f-skipped");
  const fStatus = $("#f-status");
  const fDepth = $("#f-depth");
  const hitCount = $("#hit-count");
  const copyBtn = $("#copy-urls");
  const toast = $("#toast");

  function load() {
    if (window.__SCHIZO_DATA__) {
      DATA = window.__SCHIZO_DATA__;
      paintCounts();
      refresh();
      window.dispatchEvent(new Event("schizo:data"));
      return Promise.resolve();
    }
    return fetch("data.json").then((r) => r.json()).then((d) => {
      DATA = d;
      window.__SCHIZO_DATA__ = DATA;
      paintCounts();
      refresh();
      window.dispatchEvent(new Event("schizo:data"));
    });
  }

  function paintCounts() {
    const m = DATA.meta || {};
    $("#ct-fetched").textContent = m.fetched_count ?? "?";
    $("#ct-err").textContent = m.error_count ?? "?";
    $("#ct-skipped").textContent = m.skipped_count ?? "?";
    $("#ct-links").textContent = m.link_count ?? "?";
  }

  function statusBucket(s) {
    if (s == null) return "err";
    if (s >= 500) return "5xx";
    if (s >= 400) return "4xx";
    if (s >= 300) return "3xx";
    if (s >= 200) return "2xx";
    return "err";
  }

  function applyFilters(p) {
    // Uncrawled (skipped) toggle is the master switch for state=skipped.
    if (p.state === "skipped" && !fSkipped.checked) return false;
    const text = (q.value || "").toLowerCase().trim();
    if (text) {
      const hay = (p.title + " " + p.url + " " + p.domain).toLowerCase();
      if (!hay.includes(text)) return false;
    }
    if (!fOn.checked && p.is_seed) return false;
    if (!fOff.checked && !p.is_seed) return false;
    const sv = fStatus.value;
    if (sv) {
      if (sv !== statusBucket(p.status)) return false;
    }
    const dv = parseInt(fDepth.value, 10);
    if (!isNaN(dv) && p.depth != null && p.depth > dv) return false;
    return true;
  }

  function refresh() {
    if (!DATA) return;
    filtered = DATA.pages.filter(applyFilters);
    hitCount.textContent = filtered.length;
    renderGrid();
    window.dispatchEvent(new CustomEvent("schizo:filter", {detail: new Set(filtered.map(p => p.id))}));
  }

  function renderGrid() {
    const frag = document.createDocumentFragment();
    // Cap initial render to avoid 5k-DOM-node hitches; "load more" pattern.
    const HARD_CAP = 1200;
    let count = 0;
    for (const p of filtered) {
      if (count >= HARD_CAP) break;
      frag.appendChild(card(p));
      count++;
    }
    grid.replaceChildren(frag);
    if (filtered.length > HARD_CAP) {
      const more = document.createElement("div");
      more.style.cssText = "grid-column:1/-1;text-align:center;color:var(--fg-dim);padding:1em";
      more.textContent = `showing ${HARD_CAP} of ${filtered.length} — refine the search to see the rest`;
      grid.appendChild(more);
    }
  }

  function escapeHtml(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function card(p) {
    const wrap = document.createElement("div");
    wrap.className = "card" + (p.state === "skipped" ? " skip" : "");
    const linkable = !!p.detail_path;

    const thumb = document.createElement("div");
    thumb.className = "thumb";
    if (p.thumb) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.src = p.thumb;
      img.alt = p.title || p.url;
      thumb.appendChild(img);
    } else {
      thumb.classList.add("empty");
      thumb.textContent = p.error ? "errored" : (p.state === "skipped" ? "uncrawled" : "no screenshot");
    }

    const body = document.createElement("div");
    body.className = "body";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = p.title || "(untitled)";
    const url = document.createElement("div");
    url.className = "url";
    url.textContent = p.url;
    const chips = document.createElement("div");
    chips.className = "chips";
    chips.innerHTML = renderChips(p);

    body.appendChild(title);
    body.appendChild(url);
    body.appendChild(chips);

    if (linkable) {
      const a = document.createElement("a");
      a.className = "cardlink";
      a.href = p.detail_path;
      a.appendChild(wrap);
      wrap.appendChild(thumb);
      wrap.appendChild(body);
      return a;
    } else {
      // Skipped pages: card body still useful (URL is a clickable external link)
      const ext = document.createElement("a");
      ext.href = p.url;
      ext.target = "_blank";
      ext.rel = "noopener";
      ext.style.cssText = "color:inherit;text-decoration:none";
      ext.title = "open externally — not crawled";
      ext.appendChild(wrap);
      wrap.appendChild(thumb);
      wrap.appendChild(body);
      return ext;
    }
  }

  function renderChips(p) {
    const parts = [];
    if (p.state === "skipped") {
      parts.push(`<span class="chip skip">uncrawled</span>`);
    } else if (p.is_seed) {
      parts.push(`<span class="chip on">on</span>`);
    } else {
      parts.push(`<span class="chip off">off</span>`);
    }
    if (p.status != null) {
      parts.push(`<span class="chip s${statusBucket(p.status)}">${p.status}</span>`);
    } else if (p.state === "error") {
      parts.push(`<span class="chip err">err</span>`);
    }
    if (p.depth != null) {
      parts.push(`<span class="chip">d=${p.depth}</span>`);
    }
    parts.push(`<span class="chip">out ${p.out_count}</span>`);
    parts.push(`<span class="chip">in ${p.in_count}</span>`);
    return parts.join("");
  }

  // ---- view toggling ----
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const t of document.querySelectorAll(".tab")) t.classList.remove("active");
      tab.classList.add("active");
      const view = tab.dataset.view;
      for (const v of document.querySelectorAll(".view")) v.classList.remove("active");
      document.getElementById("view-" + view).classList.add("active");
      if (view === "graph") {
        window.dispatchEvent(new Event("schizo:graph-show"));
      }
    });
  }

  // ---- debounced filter wiring ----
  let refreshTimer = null;
  function debouncedRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }
  for (const el of [q, fOn, fOff, fSkipped, fStatus, fDepth]) {
    el.addEventListener("input", debouncedRefresh);
    el.addEventListener("change", debouncedRefresh);
  }

  // ---- copy URLs ----
  copyBtn.addEventListener("click", async () => {
    const text = filtered.map((p) => p.url).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      showToast(`copied ${filtered.length} URLs`);
    } catch {
      // Fallback for file:// where clipboard API is sometimes restricted.
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); showToast(`copied ${filtered.length} URLs`); }
      catch { showToast("copy failed — clipboard blocked"); }
      ta.remove();
    }
  });

  let toastTimer = null;
  function showToast(msg) {
    toast.textContent = msg;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 1800);
  }

  load().catch((e) => {
    grid.innerHTML = `<p style="color:#ff6b6b">Failed to load data: ${escapeHtml(e.message)}</p>`;
  });
})();
