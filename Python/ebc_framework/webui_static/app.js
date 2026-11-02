"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok || body.error) throw new Error(body.error || ("HTTP " + r.status));
  return body;
};
const post = (path, data) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });

let TESTS = [];
let CELLS = [];
let since = 0;
let samples = [];      // [t, v, i]
let running = false;

// Remember port / cell / test between visits. Per-browser convenience only;
// a blocked or wiped storage just means choosing again.
const remember = (key, value) => { try { localStorage.setItem("ebc." + key, value); } catch (e) {} };
const recall = (key) => { try { return localStorage.getItem("ebc." + key); } catch (e) { return null; } };

/* ---------------- tabs ---------------- */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.remove("hidden");
  };
});

/* ---------------- bootstrap ---------------- */

async function bootstrap() {
  const b = await api("/api/bootstrap");
  CELLS = b.cells;
  TESTS = b.tests;

  const port = $("port");
  port.innerHTML = "";
  if (!b.ports.length) {
    port.innerHTML = '<option value="">(no serial ports found)</option>';
    $("porthint").textContent =
      "No ports. Is the tester plugged in and switched on?";
  } else {
    b.ports.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.device;
      o.textContent = p.device + " - " + p.description + (p.likely ? "  (likely the tester)" : "");
      port.appendChild(o);
    });
    const likely = b.ports.find((p) => p.likely);
    const saved = recall("port");
    if (saved && b.ports.some((p) => p.device === saved)) port.value = saved;
    else if (likely) port.value = likely.device;
    const notes = [];
    if (!likely) notes.push("No port looks like the tester (a CH340). Check that it is plugged in and switched on, then Rescan.");
    if (b.platform === "darwin") notes.push("On macOS the /dev/tty.* twins are hidden: opening one hangs, so only /dev/cu.* is offered.");
    $("porthint").textContent = notes.join(" ");
  }

  // The cell is always an explicit choice. A blank "use the test's own cell"
  // default is dangerous: C-rates scale by capacity, so a test that names a
  // 7 Ah cell, run on a 1.9 Ah cell, drives 3.7x the intended current.
  const cell = $("cell");
  cell.innerHTML = "";
  CELLS.forEach((c) => {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = c.id + "  -  " + c.capacity_ah + " Ah, " + c.min_v + "-" + c.max_v + " V";
    cell.appendChild(o);
  });
  const savedCell = recall("cell");
  if (savedCell && CELLS.some((c) => c.id === savedCell)) cell.value = savedCell;

  const test = $("test");
  test.innerHTML = "";
  TESTS.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.value;
    o.textContent = t.value + "  [" + t.type + "]";
    test.appendChild(o);
  });
  const savedTest = recall("test");
  if (savedTest && TESTS.some((t) => t.value === savedTest)) test.value = savedTest;

  fillBase($("cellbase"), CELLS.map((c) => c.id));
  fillBase($("testbase"), TESTS.map((t) => t.value));
  describeTest();
  loadPreview();
}

function fillBase(sel, names) {
  const keep = sel.value;
  sel.innerHTML = '<option value="">(blank template)</option>';
  names.forEach((n) => {
    const o = document.createElement("option");
    o.value = n;
    o.textContent = n;
    sel.appendChild(o);
  });
  sel.value = keep;
}

function describeTest() {
  const t = TESTS.find((x) => x.value === $("test").value);
  const c = CELLS.find((x) => x.id === $("cell").value);
  const hint = $("testhint");
  hint.classList.remove("warn");
  if (!t) return (hint.textContent = "");
  const parts = [];
  if (t.description) parts.push(t.description);
  if (c) parts.push("Runs on " + c.id + ": 1C = " + c.capacity_ah.toFixed(3) + " A.");
  if (t.cell && c && t.cell !== c.id) {
    parts.push('This file was written for "' + t.cell + '"; your selection overrides it.');
    hint.classList.add("warn");
  }
  hint.textContent = parts.join("  ");
}
$("test").onchange = () => { remember("test", $("test").value); describeTest(); loadPreview(); };
$("cell").onchange = () => { remember("cell", $("cell").value); describeTest(); loadPreview(); };
$("port").onchange = () => remember("port", $("port").value);

/* ---------------- port + run ---------------- */

$("rescan").onclick = () => bootstrap();
$("connect").onclick = async () => {
  try {
    await post("/api/open", { port: $("port").value });
  } catch (e) {
    flash($("status"), e.message, true);
  }
};
$("disconnect").onclick = () => post("/api/close").catch(() => {});

$("start").onclick = async () => {
  const t = $("test").value;
  const c = CELLS.find((x) => x.id === $("cell").value);
  if (!t || !c) return flash($("status"), "Choose a cell and a test first.", true);
  const msg = [
    "Start this test?",
    "",
    "Test:  " + t,
    "Cell:  " + c.id + "  (" + c.capacity_ah + " Ah, " + c.min_v + "-" + c.max_v + " V)",
    "1C on this cell = " + c.capacity_ah.toFixed(3) + " A",
    "Port:  " + $("port").value,
    "Data:  " + ($("save").checked ? "saved to CSV" : "NOT saved"),
    "",
    "Check that the cell above is the one actually connected.",
    "This drives real current. Never leave a charging cell unattended.",
  ].join("\n");
  if (!confirm(msg)) return;
  samples = [];
  since = 0;
  try {
    await post("/api/start", { test: t, cell: c.id, save: $("save").checked });
  } catch (e) {
    flash($("status"), e.message, true);
  }
};

$("stop").onclick = async () => {
  $("stop").disabled = true;
  try {
    await post("/api/stop");
  } catch (e) {
    flash($("status"), e.message, true);
  }
};

function flash(el, text, bad) {
  el.textContent = text;
  el.classList.toggle("error", !!bad);
}

/* ---------------- polling ---------------- */

async function poll() {
  try {
    const s = await api("/api/state?since=" + since);
    if (s.samples && s.samples.length) {
      samples = samples.concat(s.samples);
      if (samples.length > 20000) samples = samples.slice(-20000);
    }
    since = s.next_index;
    render(s);
  } catch (e) {
    $("badge").textContent = "server unreachable";
    $("badge").className = "badge error";
  } finally {
    setTimeout(poll, 1000);
  }
}

function render(s) {
  $("rv").textContent = s.voltage == null ? "--" : s.voltage.toFixed(3);
  $("ri").textContent = s.current == null ? "--" : (s.current >= 0 ? "+" : "") + s.current.toFixed(3);
  $("badge").textContent = s.status;
  $("badge").className = "badge " + s.status;

  running = s.status === "running";
  $("stop").disabled = !running;
  $("start").disabled = running || !(s.status === "idle" || s.status === "finished" || s.status === "stopped");
  $("connect").disabled = running;
  $("disconnect").disabled = running;

  const fallback = {
    disconnected: "Not connected. Choose the tester's port and press Connect.",
    connecting: "Opening the port...",
  }[s.status] || "-";
  if (running && s.stale_s != null && s.stale_s > 5) {
    // The tester goes quiet now and then while it keeps its setpoint; say
    // so rather than letting the page look frozen.
    flash($("status"), "No readings from the tester for " + Math.round(s.stale_s) +
      " s. It keeps the current setpoint meanwhile; the software reconnects only after 60 s.", false);
    $("status").classList.add("warn");
  } else {
    $("status").classList.remove("warn");
    flash($("status"), s.message || fallback, s.status === "error");
  }
  const lr = s.last_run;
  const box = $("lastrun");
  if (lr && !running) {
    const label = { finished: "Finished", stopped: "Stopped by you", error: "ENDED WITH AN ERROR" }[lr.status] || lr.status;
    box.className = "lastrun " + lr.status;
    box.innerHTML = "";
    const head = document.createElement("b");
    head.textContent = "Last run, " + lr.ended + ": " + label;
    const body = document.createElement("div");
    body.textContent = lr.test + " on " + lr.cell + "  |  " + fmtTime(lr.elapsed_s || 0) +
      "  |  " + (lr.cumulative_ah || 0).toFixed(4) + " Ah  |  " + (lr.message || "");
    const file = document.createElement("div");
    file.className = "path";
    file.textContent = lr.csv ? "Saved: " + lr.csv : "Not saved";
    box.append(head, body, file);
    box.hidden = false;
  } else {
    box.hidden = true;
  }

  $("selapsed").textContent = s.elapsed_s ? fmtTime(s.elapsed_s) : "--";
  $("smode").textContent = s.mode || "--";
  $("scum").textContent = (running || s.cumulative_ah) ? s.cumulative_ah.toFixed(4) + " Ah" : "--";
  $("scsv").textContent = s.csv || (s.status === "running" ? "(not saving)" : "--");

  renderProgress(s);
  draw();
}

function fmtTime(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = Math.floor(sec % 60);
  return (h ? h + "h " : "") + (h || m ? m + "m " : "") + x + "s";
}

/* ---------------- chart ---------------- */

const W = 900, H = 380, PAD_L = 56, PAD_R = 56, PAD_T = 14, PAD_B = 28;

// Time window shown, in seconds of the run; 0 = everything since the start.
let rangeS = Number(recall("range") || 0);

function markRange() {
  document.querySelectorAll("#range button").forEach((b) =>
    b.classList.toggle("active", Number(b.dataset.s) === rangeS));
}
document.querySelectorAll("#range button").forEach((b) => {
  b.onclick = () => {
    rangeS = Number(b.dataset.s);
    remember("range", String(rangeS));
    markRange();
    draw();
  };
});
markRange();

function visibleSamples() {
  if (!rangeS || !samples.length) return samples;
  const from = samples[samples.length - 1][0] - rangeS;
  // Samples are in time order, so find the first one inside the window.
  let lo = 0, hi = samples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (samples[mid][0] < from) lo = mid + 1; else hi = mid;
  }
  return samples.slice(lo);
}

function draw() {
  const svg = $("chart");
  const view = visibleSamples();
  if (!view.length) {
    svg.innerHTML = '<text x="450" y="190" text-anchor="middle" fill="currentColor" opacity=".4" font-size="14">no data yet</text>';
    return;
  }
  // Until the run is longer than the window, show the whole run; after
  // that the window scrolls, always ending at the newest sample.
  const t1 = view[view.length - 1][0];
  const t0 = rangeS ? Math.max(samples[0][0], t1 - rangeS) : view[0][0];
  const span = Math.max(t1 - t0, 1);
  const vs = view.map((s) => s[1]), is = view.map((s) => s[2]);
  const vlo = Math.min(...vs), vhi = Math.max(...vs);
  const ilo = Math.min(...is, 0), ihi = Math.max(...is, 0);
  const vpad = Math.max((vhi - vlo) * 0.1, 0.02);
  const ipad = Math.max((ihi - ilo) * 0.1, 0.05);

  const x = (t) => PAD_L + ((t - t0) / span) * (W - PAD_L - PAD_R);
  const yv = (v) => PAD_T + (1 - (v - (vlo - vpad)) / ((vhi + vpad) - (vlo - vpad))) * (H - PAD_T - PAD_B);
  const yi = (i) => PAD_T + (1 - (i - (ilo - ipad)) / ((ihi + ipad) - (ilo - ipad))) * (H - PAD_T - PAD_B);

  // Decimate so a long run stays responsive, but always keep the newest
  // sample so the trace reaches the right edge.
  const step = Math.max(1, Math.floor(view.length / 1200));
  const picked = [];
  for (let k = 0; k < view.length; k += step) picked.push(view[k]);
  if (picked[picked.length - 1] !== view[view.length - 1]) picked.push(view[view.length - 1]);
  let pv = "", pi = "";
  picked.forEach((s, k) => {
    pv += (k ? "L" : "M") + x(s[0]).toFixed(1) + " " + yv(s[1]).toFixed(1);
    pi += (k ? "L" : "M") + x(s[0]).toFixed(1) + " " + yi(s[2]).toFixed(1);
  });

  const parts = [];
  parts.push('<line x1="' + PAD_L + '" y1="' + yi(0).toFixed(1) + '" x2="' + (W - PAD_R) + '" y2="' + yi(0).toFixed(1) + '" stroke="currentColor" opacity=".18" stroke-dasharray="4 4"/>');
  parts.push(axisLabel(PAD_L - 8, yv(vhi), vhi.toFixed(2), "end", "var(--v)"));
  parts.push(axisLabel(PAD_L - 8, yv(vlo), vlo.toFixed(2), "end", "var(--v)"));
  parts.push(axisLabel(W - PAD_R + 8, yi(ihi), ihi.toFixed(2), "start", "var(--i)"));
  parts.push(axisLabel(W - PAD_R + 8, yi(ilo), ilo.toFixed(2), "start", "var(--i)"));
  // Time labels show the run's own elapsed time, so a scrolled window still
  // tells you where in the test you are.
  parts.push(axisLabel(PAD_L, H - 8, fmtTime(t0), "start", "currentColor"));
  parts.push(axisLabel((PAD_L + W - PAD_R) / 2, H - 8, fmtTime((t0 + t1) / 2), "middle", "currentColor"));
  parts.push(axisLabel(W - PAD_R, H - 8, fmtTime(t1), "end", "currentColor"));
  parts.push('<path d="' + pi + '" fill="none" stroke="var(--i)" stroke-width="1.6"/>');
  parts.push('<path d="' + pv + '" fill="none" stroke="var(--v)" stroke-width="1.8"/>');
  svg.innerHTML = parts.join("");
}

function axisLabel(x, y, text, anchor, fill) {
  return '<text x="' + x.toFixed(1) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="' + anchor + '" font-size="11" fill="' + fill + '" opacity=".85">' + text + "</text>";
}

/* ---------------- preview of the planned test ---------------- */

let PLAN = null;          // preview of the selected test on the selected cell
let PLAN_ERR = "";
let RUN_PLAN = null;      // plan of the test that is actually running
let RUN_ID = null;
let previewSeq = 0;
let wasRunning = false;

const KIND_COLOR = {
  discharge: "var(--dis)", profile: "var(--dis)", pulses: "var(--dis)",
  charge: "var(--chg)", rest: "var(--rest)",
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return h + "h " + String(m).padStart(2, "0") + "m";
  if (m) return m + " min" + (m < 10 && s ? " " + s + " s" : "");
  return s + " s";
}

async function loadPreview() {
  const t = $("test").value, c = $("cell").value;
  if (!t || !c) return;
  const seq = ++previewSeq;
  try {
    const p = await api("/api/plan?test=" + encodeURIComponent(t) + "&cell=" + encodeURIComponent(c));
    if (seq !== previewSeq) return;        // a newer selection already won
    PLAN = p; PLAN_ERR = "";
  } catch (e) {
    if (seq !== previewSeq) return;
    PLAN = null; PLAN_ERR = e.message;
  }
  if (!running) renderPlan(PLAN, null, PLAN_ERR);
}

function currentText(s) {
  if (s.kind === "rest") return "0 A";
  if (s.cccv === "cv") return s.i_cc.toFixed(2) + " → " + s.i_cut.toFixed(s.i_cut < 0.1 ? 3 : 2) + " A charge";
  return (s.kind === "profile" ? "peak " : "") + Math.abs(s.current_a).toFixed(2) + " A " +
    (s.current_a > 0 ? "charge" : "discharge");
}

function renderPlan(plan, prog, err) {
  const svg = $("pchart");
  if (!plan) {
    $("ptitle").textContent = "Preview";
    $("ptotal").textContent = "";
    svg.innerHTML = '<text x="450" y="90" text-anchor="middle" fill="currentColor" opacity=".55" font-size="13">' +
      esc(err ? "Preview not available: " + err : "Choose a cell and a test") + "</text>";
    $("ptable").innerHTML = "";
    $("pnotes").innerHTML = "";
    return;
  }
  $("ptitle").textContent = (prog ? "Running: " : "Preview: ") + plan.test + " on " + plan.cell;
  $("ptotal").textContent = (plan.exact ? "Total " : "Total ≈ ") + fmtDur(plan.total_s);
  drawPlanChart(plan, prog);

  const rows = ["<tr><th>#</th><th>Phase</th><th>Current</th><th>Length</th><th>Ends when</th></tr>"];
  plan.segments.forEach((s, i) => {
    const cls = prog ? (i < prog.index ? "done" : i === prog.index ? "active" : "") : "";
    rows.push('<tr class="' + cls + '"><td class="num">' + (i + 1) + "</td>" +
      '<td><span class="dot" style="background:' + KIND_COLOR[s.kind] + '"></span>' + esc(s.label) + "</td>" +
      '<td class="num">' + currentText(s) + "</td>" +
      '<td class="num">' + (s.exact ? "" : "≈ ") + fmtDur(s.duration_s) + "</td>" +
      "<td>" + esc(s.until) + "</td></tr>");
  });
  $("ptable").innerHTML = rows.join("");
  $("pnotes").innerHTML = plan.notes.map((n) => "<li>" + esc(n) + "</li>").join("");
}

function drawPlanChart(plan, prog) {
  const W2 = 900, H2 = 170, L = 56, R = 12, T = 10, B = 24;
  const total = Math.max(plan.total_s, 1);
  const pts = plan.points;
  const is = pts.map((p) => p[1]);
  const ihi = Math.max(0, ...is), ilo = Math.min(0, ...is);
  const pad = Math.max((ihi - ilo) * 0.12, 0.05);
  const x = (t) => L + (t / total) * (W2 - L - R);
  const y = (i) => T + (1 - (i - (ilo - pad)) / ((ihi + pad) - (ilo - pad))) * (H2 - T - B);
  const parts = [];

  // One tinted band per phase; a dashed edge where the length is estimated.
  let t0 = 0;
  plan.segments.forEach((s, i) => {
    const t1 = t0 + s.duration_s, xa = x(t0), xb = x(t1);
    parts.push('<rect x="' + xa.toFixed(1) + '" y="' + T + '" width="' + Math.max(xb - xa, 0.6).toFixed(1) +
      '" height="' + (H2 - T - B) + '" fill="' + KIND_COLOR[s.kind] + '" opacity="' + (i % 2 ? 0.10 : 0.17) + '"/>');
    if (!s.exact) {
      parts.push('<line x1="' + xb.toFixed(1) + '" y1="' + T + '" x2="' + xb.toFixed(1) + '" y2="' + (H2 - B) +
        '" stroke="currentColor" opacity=".4" stroke-dasharray="3 3"/>');
    }
    if (xb - xa > 70) {
      const short = s.label.length > 22 ? s.label.slice(0, 21) + "…" : s.label;
      parts.push('<text x="' + ((xa + xb) / 2).toFixed(1) + '" y="' + (T + 12) +
        '" text-anchor="middle" font-size="10" fill="currentColor" opacity=".65">' + esc(short) + "</text>");
    }
    t0 = t1;
  });

  parts.push('<line x1="' + L + '" y1="' + y(0).toFixed(1) + '" x2="' + (W2 - R) + '" y2="' + y(0).toFixed(1) +
    '" stroke="currentColor" opacity=".3"/>');
  let d = "";
  pts.forEach((p, k) => { d += (k ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(p[1]).toFixed(1); });
  parts.push('<path d="' + d + '" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".9"/>');

  if (ihi > 0) parts.push(axisLabel(L - 6, y(ihi), "+" + ihi.toFixed(2) + " A", "end", "currentColor"));
  parts.push(axisLabel(L - 6, y(0), "0 A", "end", "currentColor"));
  if (ilo < 0) parts.push(axisLabel(L - 6, y(ilo), ilo.toFixed(2) + " A", "end", "currentColor"));
  parts.push(axisLabel(L, H2 - 6, "start", "start", "currentColor"));
  parts.push(axisLabel(W2 - R, H2 - 6, (plan.exact ? "" : "≈ ") + fmtDur(total), "end", "currentColor"));

  if (prog && prog.fraction != null) {
    const xn = x(prog.fraction * total);
    parts.push('<line x1="' + xn.toFixed(1) + '" y1="' + (T - 2) + '" x2="' + xn.toFixed(1) + '" y2="' + (H2 - B) +
      '" stroke="var(--accent)" stroke-width="2.5"/>');
    parts.push('<text x="' + xn.toFixed(1) + '" y="' + (H2 - 8) +
      '" text-anchor="middle" font-size="11" font-weight="700" fill="var(--accent)">now</text>');
  }
  $("pchart").innerHTML = parts.join("");
}

/* ---------------- progress bar ---------------- */

function renderProgress(s) {
  // Load the running test's own plan once per run (also after a reload).
  if (running && s.run_id && s.run_id !== RUN_ID) {
    RUN_ID = s.run_id;
    RUN_PLAN = null;
    api("/api/plan?current=1").then((p) => { RUN_PLAN = p; }).catch(() => { RUN_PLAN = null; });
  }
  if (running) renderPlan(RUN_PLAN || PLAN, s.progress, "");
  else if (wasRunning) renderPlan(PLAN, null, PLAN_ERR);   // back to the preview
  wasRunning = running;

  const box = $("progress"), lr = s.last_run;
  let frac = null, label = "", cls = "";
  if (running && s.progress) {
    frac = s.progress.fraction;
    const seg = RUN_PLAN && RUN_PLAN.segments[s.progress.index];
    cls = seg ? "k-" + seg.kind : "";
    const n = RUN_PLAN ? RUN_PLAN.segments.length : "?";
    const left = s.progress.remaining_s == null ? "" :
      (s.progress.estimated ? "≈ " : "") + fmtDur(s.progress.remaining_s) + " left";
    label = "<b>Phase " + (s.progress.index + 1) + " of " + n + ":</b> " + esc(s.progress.label) +
      (left ? "  ·  " + left : "");
  } else if (running) {
    label = "Starting...";
  } else if (lr && lr.fraction != null) {
    frac = lr.fraction;
    cls = lr.status;
    label = { finished: "<b>Finished.</b>", stopped: "<b>Stopped</b> here.",
      error: "<b>Ended with an error</b> here." }[lr.status] || esc(lr.status);
  } else {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const pct = frac == null ? 0 : Math.round(frac * 100);
  $("pfill").style.width = "calc(" + pct + "% - 4px)";
  $("pfill").className = "fill " + cls;
  $("ppct").textContent = frac == null ? "" : pct + "%";
  $("pbar").setAttribute("aria-valuenow", String(pct));
  const quiet = running ? s.quiet_count : (lr ? lr.quiet_count : 0);
  const longest = running ? s.quiet_longest_s : (lr ? lr.quiet_longest_s : 0);
  if (quiet) {
    label += '<br><span class="quietnote">The tester went quiet ' + quiet + (quiet === 1 ? " time" : " times") +
      " (longest " + Math.round(longest) + " s).</span>";
  }
  $("plabel").innerHTML = label;
}

/* ---------------- definition editors ---------------- */

const CELL_TEMPLATE = {
  id: "MyCell",
  description: "",
  chemistry: "NMC",
  nominal_capacity_ah: 2.0,
  nominal_voltage_v: 3.7,
  voltage: { max_v: 4.2, min_v: 2.75, storage_v: 3.8 },
  current: { max_charge_a: 2.0, max_discharge_a: 4.0, standard_charge_a: 1.0, standard_discharge_a: 2.0 },
  safety: { hard_max_v: 4.25, hard_min_v: 2.7, charge_taper_a: 0.05 },
};

const TEST_TEMPLATE = {
  name: "my_test",
  type: "protocol",
  sample_period_s: 1.0,
  max_duration_s: 7200,
  repeat: 1,
  order: ["discharge", "charge"],
  discharge: { protocol: "crate", c_rate: 1.0, v_min: 3.0, rest_after_s: 600 },
  charge: { protocol: "cc_cv", c_rate: 1.0, v_max: 4.2, cutoff_c: 0.05, rest_after_s: 600 },
};

$("celljson").value = JSON.stringify(CELL_TEMPLATE, null, 2);
$("testjson").value = JSON.stringify(TEST_TEMPLATE, null, 2);

async function loadDefinition(kind, nameSel, nameBox, area, msg) {
  const name = $(nameSel).value;
  if (!name) {
    $(area).value = JSON.stringify(kind === "cell" ? CELL_TEMPLATE : TEST_TEMPLATE, null, 2);
    $(nameBox).value = "";
    return;
  }
  try {
    const r = await api("/api/definition?kind=" + kind + "&name=" + encodeURIComponent(name));
    $(area).value = JSON.stringify(r.json, null, 2);
    $(nameBox).value = name.split("/").pop();
    setMsg(msg, "Loaded " + name + ". Change the name before saving to keep the original.", false);
  } catch (e) {
    setMsg(msg, e.message, true);
  }
}

async function saveDefinition(kind, nameBox, area, msg) {
  let parsed;
  try {
    parsed = JSON.parse($(area).value);
  } catch (e) {
    return setMsg(msg, "Not valid JSON: " + e.message, true);
  }
  try {
    const r = await post("/api/definition", { kind: kind, name: $(nameBox).value.trim(), json: parsed });
    setMsg(msg, "Saved to " + r.path, false);
    await bootstrap();
  } catch (e) {
    setMsg(msg, e.message, true);
  }
}

function setMsg(id, text, bad) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (bad ? "bad" : "ok");
}

$("cellload").onclick = () => loadDefinition("cell", "cellbase", "cellname", "celljson", "cellmsg");
$("testload").onclick = () => loadDefinition("test", "testbase", "testname", "testjson", "testmsg");
$("cellsave").onclick = () => saveDefinition("cell", "cellname", "celljson", "cellmsg");
$("testsave").onclick = () => saveDefinition("test", "testname", "testjson", "testmsg");

window.addEventListener("beforeunload", (e) => {
  if (running) {
    e.preventDefault();
    e.returnValue = "A test is running. Closing this page does not stop it.";
  }
});

bootstrap().then(poll).catch((e) => flash($("status"), e.message, true));
