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
$("test").onchange = () => { remember("test", $("test").value); describeTest(); };
$("cell").onchange = () => { remember("cell", $("cell").value); describeTest(); };
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
  flash($("status"), s.message || fallback, s.status === "error");
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
  $("scum").textContent = s.cumulative_ah ? s.cumulative_ah.toFixed(4) + " Ah" : "--";
  $("scsv").textContent = s.csv || (s.status === "running" ? "(not saving)" : "--");

  draw();
}

function fmtTime(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = Math.floor(sec % 60);
  return (h ? h + "h " : "") + (h || m ? m + "m " : "") + x + "s";
}

/* ---------------- chart ---------------- */

const W = 900, H = 380, PAD_L = 56, PAD_R = 56, PAD_T = 14, PAD_B = 28;

function draw() {
  const svg = $("chart");
  if (!samples.length) {
    svg.innerHTML = '<text x="450" y="190" text-anchor="middle" fill="currentColor" opacity=".4" font-size="14">no data yet</text>';
    return;
  }
  const t0 = samples[0][0], t1 = samples[samples.length - 1][0] || 1;
  const span = Math.max(t1 - t0, 1);
  const vs = samples.map((s) => s[1]), is = samples.map((s) => s[2]);
  const vlo = Math.min(...vs), vhi = Math.max(...vs);
  const ilo = Math.min(...is, 0), ihi = Math.max(...is, 0);
  const vpad = Math.max((vhi - vlo) * 0.1, 0.02);
  const ipad = Math.max((ihi - ilo) * 0.1, 0.05);

  const x = (t) => PAD_L + ((t - t0) / span) * (W - PAD_L - PAD_R);
  const yv = (v) => PAD_T + (1 - (v - (vlo - vpad)) / ((vhi + vpad) - (vlo - vpad))) * (H - PAD_T - PAD_B);
  const yi = (i) => PAD_T + (1 - (i - (ilo - ipad)) / ((ihi + ipad) - (ilo - ipad))) * (H - PAD_T - PAD_B);

  // Decimate so a long run stays responsive.
  const step = Math.max(1, Math.floor(samples.length / 1200));
  let pv = "", pi = "";
  for (let k = 0; k < samples.length; k += step) {
    const s = samples[k];
    pv += (k ? "L" : "M") + x(s[0]).toFixed(1) + " " + yv(s[1]).toFixed(1);
    pi += (k ? "L" : "M") + x(s[0]).toFixed(1) + " " + yi(s[2]).toFixed(1);
  }

  const parts = [];
  parts.push('<line x1="' + PAD_L + '" y1="' + yi(0).toFixed(1) + '" x2="' + (W - PAD_R) + '" y2="' + yi(0).toFixed(1) + '" stroke="currentColor" opacity=".18" stroke-dasharray="4 4"/>');
  parts.push(axisLabel(PAD_L - 8, yv(vhi), vhi.toFixed(2), "end", "var(--v)"));
  parts.push(axisLabel(PAD_L - 8, yv(vlo), vlo.toFixed(2), "end", "var(--v)"));
  parts.push(axisLabel(W - PAD_R + 8, yi(ihi), ihi.toFixed(2), "start", "var(--i)"));
  parts.push(axisLabel(W - PAD_R + 8, yi(ilo), ilo.toFixed(2), "start", "var(--i)"));
  parts.push(axisLabel(PAD_L, H - 8, fmtTime(0), "start", "currentColor"));
  parts.push(axisLabel(W - PAD_R, H - 8, fmtTime(span), "end", "currentColor"));
  parts.push('<path d="' + pi + '" fill="none" stroke="var(--i)" stroke-width="1.6"/>');
  parts.push('<path d="' + pv + '" fill="none" stroke="var(--v)" stroke-width="1.8"/>');
  svg.innerHTML = parts.join("");
}

function axisLabel(x, y, text, anchor, fill) {
  return '<text x="' + x.toFixed(1) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="' + anchor + '" font-size="11" fill="' + fill + '" opacity=".85">' + text + "</text>";
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
