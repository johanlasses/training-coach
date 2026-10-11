// Chart.js setup driven by <script type="application/json" data-chart="..."> blocks.
// Colours come from CSS custom properties so light/dark both work.
(function () {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const ramp = v("--ramp").split(",").map((s) => s.trim());

  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = v("--text-muted");
  Chart.defaults.borderColor = v("--grid");
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation = false;
  Chart.defaults.plugins.legend.labels.color = v("--text-secondary");
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  Chart.defaults.plugins.legend.labels.boxHeight = 2;
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: v("--surface-1"), titleColor: v("--text-primary"), bodyColor: v("--text-secondary"),
    borderColor: v("--border"), borderWidth: 1, padding: 8, usePointStyle: true,
  });

  const axis = (extra = {}) => ({
    grid: { color: v("--grid"), drawTicks: false }, border: { color: v("--axis") },
    ticks: { padding: 6 }, ...extra,
  });
  const lineDs = (label, data, colour) => ({
    label, data, borderColor: colour, backgroundColor: colour, borderWidth: 2,
    pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: v("--surface-1"), tension: 0.2,
  });
  const hover = { mode: "index", intersect: false };
  const fmtDate = (s) => new Date(s).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const fmtDur = (s) => (s >= 3600 ? s / 3600 + " h" : s >= 60 ? s / 60 + " min" : s + " s");

  const builders = {
    fitness(d, el) {
      return new Chart(el, {
        type: "line",
        data: {
          labels: d.map((p) => fmtDate(p.date)),
          datasets: [lineDs("Fitness (CTL)", d.map((p) => p.ctl), v("--series-1")),
                     lineDs("Fatigue (ATL)", d.map((p) => p.atl), v("--series-2"))],
        },
        options: { interaction: hover, scales: { x: axis({ ticks: { maxTicksLimit: 7 } }), y: axis({ beginAtZero: true }) } },
      });
    },
    form(d, el) {
      const vals = d.map((p) => p.tsb);
      return new Chart(el, {
        type: "bar",
        data: {
          labels: d.map((p) => fmtDate(p.date)),
          datasets: [{ label: "Form (TSB)", data: vals, borderRadius: 2, barPercentage: 0.9, categoryPercentage: 1,
                       backgroundColor: vals.map((x) => (x >= 0 ? v("--div-pos") : v("--div-neg"))) }],
        },
        options: {
          interaction: hover,
          plugins: { legend: { display: false }, tooltip: { callbacks: {
            label: (c) => `TSB ${c.raw > 0 ? "+" : ""}${c.raw} (${c.raw >= 0 ? "fresh" : "fatigued"})` } } },
          scales: { x: axis({ ticks: { maxTicksLimit: 7 } }), y: axis({ grid: { color: (c) => (c.tick.value === 0 ? v("--axis") : v("--grid")) } }) },
        },
      });
    },
    weekly(d, el) {
      return new Chart(el, {
        type: "bar",
        data: {
          labels: d.map((w) => fmtDate(w.week)),
          datasets: [{ label: "Hours", data: d.map((w) => w.hours), backgroundColor: v("--series-1"),
                       borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", barPercentage: 0.8 }],
        },
        options: {
          plugins: { legend: { display: false }, tooltip: { callbacks: {
            title: (c) => "Week of " + c[0].label,
            label: (c) => { const w = d[c.dataIndex]; return [`${w.hours} h · ${w.km} km · ${w.rides} rides`, `TSS ${w.tss} · indoor ${w.indoor_hours} h`]; } } } },
          scales: { x: axis({ grid: { display: false } }), y: axis({ beginAtZero: true, title: { display: true, text: "hours" } }) },
        },
      });
    },
    curve(d, el) {
      // d = {series: [{label, points: {seconds: watts}}]}
      const durations = [...new Set(d.series.flatMap((s) => Object.keys(s.points).map(Number)))].sort((a, b) => a - b);
      const colours = [v("--series-1"), v("--series-2")];
      return new Chart(el, {
        type: "line",
        data: {
          datasets: d.series.map((s, i) => ({
            ...lineDs(s.label, durations.filter((t) => s.points[t] != null).map((t) => ({ x: t, y: s.points[t] })), colours[i]),
            pointRadius: 2, tension: 0,
          })),
        },
        options: {
          interaction: { mode: "nearest", axis: "x", intersect: false },
          plugins: { legend: { display: d.series.length > 1 }, tooltip: { callbacks: {
            title: (c) => fmtDur(c[0].raw.x), label: (c) => `${c.dataset.label}: ${c.raw.y} W` } } },
          scales: {
            x: axis({ type: "logarithmic", min: durations[0], max: durations[durations.length - 1],
                      // 30 min sits too close to 20 min on a log axis to label both
                      afterBuildTicks: (ax) => { ax.ticks = durations.filter((t) => t !== 1800).map((t) => ({ value: t })); },
                      ticks: { callback: (t) => fmtDur(t), autoSkip: false, maxRotation: 0 } }),
            y: axis({ title: { display: true, text: "watts" } }),
          },
        },
      });
    },
  };

  document.querySelectorAll("script[data-chart]").forEach((node) => {
    const canvas = document.getElementById(node.dataset.target);
    builders[node.dataset.chart](JSON.parse(node.textContent), canvas);
  });

  // Ordinal zone bars use the sequential ramp, light → dark.
  document.querySelectorAll(".zbar[data-zone]").forEach((el) => {
    el.style.background = ramp[Number(el.dataset.zone)] || ramp[ramp.length - 1];
  });
})();
