(() => {
  let network = null;
  let nodes = null;
  let edges = null;
  let booted = false;

  function colorFor(p) {
    if (p.state === "skipped") return "#6f7280";
    if (p.error || p.state === "error") return "#ff6b6b";
    if (p.is_seed) return "#7be0c4";
    return "#f0a070";
  }

  function tooltipFor(p) {
    const t = (p.title || "(untitled)");
    const u = p.url;
    const dep = p.depth != null ? `d=${p.depth}` : "off";
    return `${t}\n${u}\nstatus ${p.status ?? "—"} · ${dep} · ${p.state}`;
  }

  function boot() {
    if (booted) return;
    booted = true;
    const DATA = window.__SCHIZO_DATA__;
    if (!DATA || typeof vis === "undefined") {
      document.getElementById("graph").innerHTML =
        "<p style='color:#a0a0a8;padding:1em'>graph engine missing or no data.</p>";
      return;
    }

    // Pre-filter at construction time so physics only runs on the nodes
    // we actually want to see. report.js stashes the current filter set
    // on window.__SCHIZO_FILTER_IDS__ whenever it refreshes.
    const filterSet = window.__SCHIZO_FILTER_IDS__ || null;
    const inFilter = (id) => (filterSet ? filterSet.has(id) : true);

    const visiblePages = filterSet
      ? DATA.pages.filter((p) => inFilter(p.id))
      : DATA.pages;
    const visiblePageIds = new Set(visiblePages.map((p) => p.id));
    const visibleLinks = DATA.links.filter(
      (l) => visiblePageIds.has(l.src) && visiblePageIds.has(l.dst)
    );

    const nodeList = visiblePages.map((p) => ({
      id: p.id,
      label: shortLabel(p),
      title: tooltipFor(p),
      color: { background: colorFor(p), border: "#2a2a30" },
      font: { color: "#e7e7ea", size: 11 },
      shape: p.state === "skipped" ? "diamond" : (p.screenshot ? "dot" : "square"),
      size: 6 + Math.min(18, p.in_count),
      url: p.detail_path,
    }));
    const edgeList = visibleLinks.map((l, i) => ({
      id: i,
      from: l.src,
      to: l.dst,
      arrows: { to: { enabled: true, scaleFactor: 0.4 } },
      color: { color: "#3a3a45", opacity: 0.5 },
      smooth: false,
    }));

    nodes = new vis.DataSet(nodeList);
    edges = new vis.DataSet(edgeList);

    // For very large graphs, kill physics fairly aggressively so the user
    // gets an interactive view sooner.
    const heavy = nodeList.length > 1000;
    const stab = heavy ? 80 : 250;

    network = new vis.Network(
      document.getElementById("graph"),
      { nodes, edges },
      {
        physics: {
          enabled: true,
          stabilization: { iterations: stab, fit: true },
          barnesHut: {
            gravitationalConstant: heavy ? -2200 : -3000,
            springLength: heavy ? 140 : 110,
            springConstant: 0.035,
            // Over-damped so the system settles fast when perturbed but
            // still gives the satisfying "ripple from a dragged node" feel.
            damping: 0.7,
            avoidOverlap: 0.1,
          },
        },
        interaction: {
          hover: true,
          tooltipDelay: 150,
          multiselect: false,
          navigationButtons: true,
          keyboard: false,
          dragNodes: true,
        },
        nodes: { borderWidth: 1 },
        edges: { width: 0.6 },
      }
    );

    // Performance protocol on big graphs:
    //   - Run physics during initial stabilization (so layout is force-directed).
    //   - Pause the simulation once it settles to keep idle CPU at zero.
    //   - Resume on every user interaction (drag, zoom, click on background)
    //     so neighboring nodes actually ripple when you grab one.
    //   - Re-pause shortly after the last interaction so we don't spin forever.
    let pauseTimer = null;
    const wake = () => {
      if (pauseTimer) { clearTimeout(pauseTimer); pauseTimer = null; }
      try { network.startSimulation(); } catch (e) {}
    };
    const drowse = (after = 2000) => {
      if (pauseTimer) clearTimeout(pauseTimer);
      pauseTimer = setTimeout(() => {
        try { network.stopSimulation(); } catch (e) {}
      }, after);
    };

    network.once("stabilizationIterationsDone", () => drowse(500));
    network.on("dragStart", wake);
    network.on("dragEnd", () => drowse(1500));
    network.on("zoom", () => { wake(); drowse(800); });
    network.on("click", () => { wake(); drowse(1500); });

    network.on("doubleClick", (params) => {
      if (params.nodes && params.nodes.length) {
        const node = nodes.get(params.nodes[0]);
        if (node && node.url) window.location.href = node.url;
      }
    });
  }

  function shortLabel(p) {
    const s = p.title || p.url;
    return s.length > 26 ? s.slice(0, 25) + "…" : s;
  }

  // Live filter updates: when the user toggles a filter while already on the
  // graph view, rebuild the DataSets in place rather than constructing a new
  // Network (which would discard the user's pan/zoom).
  function rebuildForFilter(filterIds) {
    if (!network || !nodes || !edges) return;
    const DATA = window.__SCHIZO_DATA__;
    if (!DATA) return;
    const visibleIds = new Set();
    for (const p of DATA.pages) {
      if (!filterIds || filterIds.has(p.id)) visibleIds.add(p.id);
    }
    // Remove nodes that are no longer visible, add ones that just became visible.
    const currentNodeIds = new Set(nodes.getIds());
    const toRemove = [];
    for (const id of currentNodeIds) {
      if (!visibleIds.has(id)) toRemove.push(id);
    }
    if (toRemove.length) nodes.remove(toRemove);
    const toAdd = [];
    for (const p of DATA.pages) {
      if (visibleIds.has(p.id) && !currentNodeIds.has(p.id)) {
        toAdd.push({
          id: p.id,
          label: shortLabel(p),
          title: tooltipFor(p),
          color: { background: colorFor(p), border: "#2a2a30" },
          font: { color: "#e7e7ea", size: 11 },
          shape: p.state === "skipped" ? "diamond" : (p.screenshot ? "dot" : "square"),
          size: 6 + Math.min(18, p.in_count),
          url: p.detail_path,
        });
      }
    }
    if (toAdd.length) nodes.add(toAdd);

    // Edges: remove any whose endpoints aren't both visible, add new ones.
    const visibleEdges = new Map();
    DATA.links.forEach((l, i) => {
      if (visibleIds.has(l.src) && visibleIds.has(l.dst)) visibleEdges.set(i, l);
    });
    const currentEdgeIds = new Set(edges.getIds());
    const eRemove = [];
    for (const id of currentEdgeIds) {
      if (!visibleEdges.has(id)) eRemove.push(id);
    }
    if (eRemove.length) edges.remove(eRemove);
    const eAdd = [];
    for (const [i, l] of visibleEdges) {
      if (!currentEdgeIds.has(i)) {
        eAdd.push({
          id: i,
          from: l.src,
          to: l.dst,
          arrows: { to: { enabled: true, scaleFactor: 0.4 } },
          color: { color: "#3a3a45", opacity: 0.5 },
          smooth: false,
        });
      }
    }
    if (eAdd.length) edges.add(eAdd);
  }

  window.addEventListener("schizo:graph-show", boot);
  window.addEventListener("schizo:filter", (e) => {
    if (booted) rebuildForFilter(e.detail);
  });
})();
