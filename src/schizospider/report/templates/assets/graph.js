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

    const nodeList = DATA.pages.map((p) => ({
      id: p.id,
      label: shortLabel(p),
      title: tooltipFor(p),
      color: { background: colorFor(p), border: "#2a2a30" },
      font: { color: "#e7e7ea", size: 11 },
      shape: p.state === "skipped" ? "diamond" : (p.screenshot ? "dot" : "square"),
      size: 6 + Math.min(18, p.in_count),
      url: p.detail_path,
      _is_seed: p.is_seed,
      _state: p.state,
    }));
    const edgeList = DATA.links.map((l, i) => ({
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
          stabilization: { iterations: stab, fit: true },
          barnesHut: {
            gravitationalConstant: heavy ? -2200 : -3000,
            springLength: heavy ? 140 : 110,
            springConstant: 0.035,
            damping: 0.4,
            avoidOverlap: 0.1,
          },
        },
        interaction: {
          hover: true,
          tooltipDelay: 150,
          multiselect: false,
          navigationButtons: true,
          keyboard: false,
        },
        nodes: { borderWidth: 1 },
        edges: { width: 0.6 },
      }
    );

    // Stop physics once layout settles — keeps interaction snappy.
    network.once("stabilizationIterationsDone", () => {
      network.setOptions({ physics: { enabled: false } });
    });

    network.on("doubleClick", (params) => {
      if (params.nodes && params.nodes.length) {
        const node = nodes.get(params.nodes[0]);
        if (node && node.url) window.location.href = node.url;
      }
    });

    // Initial filter sync.
    applyCurrentFilter();
  }

  function shortLabel(p) {
    const s = p.title || p.url;
    return s.length > 26 ? s.slice(0, 25) + "…" : s;
  }

  function applyCurrentFilter() {
    const DATA = window.__SCHIZO_DATA__;
    if (!nodes || !edges || !DATA) return;
    // The grid view's filter set is authoritative; the graph mirrors it.
    const evt = new CustomEvent("schizo:filter-request");
    window.dispatchEvent(evt);
  }

  function applyFilter(visibleIds) {
    if (!nodes || !edges) return;
    const visibleSet = visibleIds;
    const update = [];
    nodes.forEach((n) => {
      const hidden = !visibleSet.has(n.id);
      if (n.hidden !== hidden) update.push({ id: n.id, hidden });
    });
    if (update.length) nodes.update(update);
    const eUpdate = [];
    edges.forEach((e) => {
      const hidden = !(visibleSet.has(e.from) && visibleSet.has(e.to));
      if (e.hidden !== hidden) eUpdate.push({ id: e.id, hidden });
    });
    if (eUpdate.length) edges.update(eUpdate);
  }

  window.addEventListener("schizo:graph-show", boot);
  window.addEventListener("schizo:filter", (e) => {
    if (network) applyFilter(e.detail);
  });
})();
