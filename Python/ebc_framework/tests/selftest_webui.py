"""Offline checks for webui.py: HTTP surface and definition validation.

No hardware and no serial port: the server is started on loopback, every
read-only endpoint is exercised, and the cell/test editors are driven
through their validation path. Anything these write is removed again.
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

import _paths  # noqa: F401  (puts the framework on sys.path)

import webui
import launchTest as LT

HTTP_PORT = 8799
BASE = "http://127.0.0.1:%d" % HTTP_PORT
CREATED = []
# A fixture, not config/tests/connection_test.json: config/tests holds
# examples users are free to delete.
CONNECTION_TEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "fixtures", "connection_test.json")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError("%s %s" % (label, detail))
    print("      %s" % label)


def main():
    controller = webui.Controller(LT.CELLS_DIR)
    webui.Handler.controller = controller
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), webui.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)

    try:
        # ---- static assets ----
        status, body = get_raw("/")
        check("page served", status == 200 and "<title>EBC-A20" in body)
        for asset, needle in (("/app.js", "api/state"), ("/style.css", "--bg")):
            status, body = get_raw(asset)
            check("%s served" % asset, status == 200 and needle in body)

        # ---- catalogues ----
        status, body = get("/api/bootstrap")
        check("bootstrap lists cells and tests",
              status == 200 and body["cells"] and body["tests"])
        check("every test entry has a value and a type",
              all(t.get("value") and t.get("type") for t in body["tests"]))
        check("ports are a list (may be empty with no tester)",
              isinstance(body["ports"], list))

        # ---- state, with no port open ----
        status, body = get("/api/state?since=0")
        check("state reports disconnected",
              status == 200 and body["status"] == "disconnected"
              and body["samples"] == [])

        # ---- starting with no port must fail, not hang ----
        status, body = post("/api/start", {"test": CONNECTION_TEST})
        check("start without a port is refused",
              status == 400 and "port" in body["error"].lower())

        # ---- stop is always safe to call ----
        status, body = post("/api/stop", {})
        check("stop with nothing running is harmless", status == 200)

        # ---- cell validation ----
        good_cell = {
            "id": "_selftest_cell", "description": "temporary",
            "chemistry": "NMC", "nominal_capacity_ah": 2.0,
            "nominal_voltage_v": 3.7,
            "voltage": {"max_v": 4.2, "min_v": 2.8, "storage_v": 3.8},
            "current": {"max_charge_a": 2.0, "max_discharge_a": 4.0,
                        "standard_charge_a": 1.0, "standard_discharge_a": 2.0},
            "safety": {"hard_max_v": 4.25, "hard_min_v": 2.75,
                       "charge_taper_a": 0.05},
        }
        status, body = post("/api/definition",
                            {"kind": "cell", "name": "_selftest_cell",
                             "json": good_cell})
        check("valid cell saved", status == 200 and os.path.isfile(body["path"]))
        CREATED.append(body["path"])

        status, body = get("/api/bootstrap")
        check("new cell appears in the library",
              any(c["id"] == "_selftest_cell" for c in body["cells"]))

        bad = dict(good_cell, id="_selftest_bad")
        bad["voltage"] = {"max_v": 3.0, "min_v": 4.0, "storage_v": 3.5}
        status, body = post("/api/definition",
                            {"kind": "cell", "name": "_selftest_bad",
                             "json": bad})
        check("min_v above max_v is rejected", status == 400)
        check("rejected cell left no file behind",
              not os.path.exists(os.path.join(LT.CELLS_DIR,
                                              "_selftest_bad.json")))

        status, body = post("/api/definition",
                            {"kind": "cell", "name": "_selftest_nofields",
                             "json": {"id": "x"}})
        check("cell missing required blocks is rejected", status == 400)

        # ---- path traversal ----
        for evil in ("../evil", "a/b", "..", "with space"):
            status, body = post("/api/definition",
                                {"kind": "cell", "name": evil,
                                 "json": good_cell})
            check("name %r refused" % evil, status == 400)

        # ---- test validation ----
        good_test = {"name": "_selftest_test", "type": "discharge",
                     "sample_period_s": 1.0, "current_unit": "C",
                     "current": 1.0, "v_cutoff": 3.0, "max_duration_s": 60}
        status, body = post("/api/definition",
                            {"kind": "test", "name": "_selftest_test",
                             "json": good_test})
        check("valid test saved", status == 200 and os.path.isfile(body["path"]))
        CREATED.append(body["path"])

        status, body = post("/api/definition",
                            {"kind": "test", "name": "_selftest_badtype",
                             "json": dict(good_test, type="nonsense")})
        check("unknown test type is rejected", status == 400)

        status, body = post("/api/definition",
                            {"kind": "test", "name": "_selftest_noname",
                             "json": {"type": "rest"}})
        check("test without a name is rejected", status == 400)

        # ---- reading a definition back ----
        status, body = get("/api/definition?kind=cell&name=_selftest_cell")
        check("definition reads back unchanged",
              status == 200 and body["json"]["id"] == "_selftest_cell")

        print("    WEBUI CHECKS PASSED")
    finally:
        server.shutdown()
        server.server_close()
        for path in CREATED:
            try:
                os.remove(path)
            except OSError:
                pass


class SilentTransport:
    """A port that opens fine and then never says anything: a wrong port."""

    def write(self, data):
        pass

    def read_frames(self):
        return iter(())

    def close(self):
        pass


def wait_for(controller, predicate, timeout_s, label):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = controller.snapshot(0)
        if predicate(snap):
            return snap
        time.sleep(0.05)
    raise AssertionError("timed out waiting for: %s (last state: %s / %s)"
                         % (label, snap["status"], snap["message"]))


def with_fake(time_accel):
    from fake_tester import fake_device
    original = webui.EBCA20.on_serial
    webui.EBCA20.on_serial = classmethod(
        lambda cls, port, limits=None: fake_device(
            limits=limits, capacity_ah=1.9, soc0=0.6, emit_period_s=0.0,
            time_accel=time_accel))
    return original


def run_end_to_end():
    """Drive real runs through the controller, as the page does."""
    print("    end to end, against the emulated cell:")
    created = []
    original = webui.EBCA20.on_serial
    try:
        # ---- connect: claims "idle" only once a frame has arrived ----
        with_fake(40.0)
        c = webui.Controller(LT.CELLS_DIR)
        c.open_port("FAKE")
        snap = wait_for(c, lambda s: s["status"] == "idle", 10, "idle")
        check("connect -> idle, with a live voltage reading",
              snap["voltage"] is not None and 2.5 < snap["voltage"] < 4.3)

        # ---- a run to completion, saving ----
        c.start_test(CONNECTION_TEST,"LP604765", save=True)
        wait_for(c, lambda s: s["status"] == "running", 5, "running")
        seen = []
        deadline = time.time() + 60
        while time.time() < deadline:
            s = c.snapshot(0)
            if s["progress"]:
                seen.append(s["progress"]["fraction"])
            if s["last_run"] is not None:
                break
            time.sleep(0.02)
        snap = c.snapshot(0)
        check("the runner's own reports drive a progress estimate "
              "(%d readings, %.0f%% -> %.0f%%)"
              % (len(seen), 100 * min(seen or [0]), 100 * max(seen or [0])),
              len(seen) >= 3 and 0 < max(seen) < 1)
        check("progress never went backwards during the run",
              all(b >= a - 1e-9 for a, b in zip(seen, seen[1:])), seen)
        lr = snap["last_run"]
        check("run finishes with a 'finished' verdict",
              lr["status"] == "finished", lr)
        check("a finished run reports the bar at 100%",
              lr["fraction"] == 1.0, lr["fraction"])
        check("samples streamed to the page during the run",
              len(c.snapshot(0)["samples"]) > 20)
        csv_path = lr["csv"]
        check("CSV written", csv_path and os.path.isfile(csv_path))
        created.append(csv_path)
        with open(csv_path, encoding="utf-8") as fh:
            header = fh.readline()
        check("CSV carries cumulative_ah", "cumulative_ah" in header)
        meta = csv_path[:-4] + "_meta.json"
        check("meta written and marked as started from the web UI",
              json.load(open(meta, encoding="utf-8"))["run"]["started_from"]
              == "webui")

        # ---- the verdict must survive the reader resuming ----
        snap = wait_for(c, lambda s: s["status"] == "idle", 10,
                        "idle after run")
        check("after the run: live status back to idle, verdict kept",
              snap["last_run"] and snap["last_run"]["status"] == "finished")

        # ---- Stop mid-run ----
        c.close_port()
        with_fake(4.0)          # slow enough to still be running
        c.open_port("FAKE")
        wait_for(c, lambda s: s["status"] == "idle", 10, "idle again")
        c.start_test(CONNECTION_TEST,"LP604765", save=False)
        # Stop only once the main phase is driving its 0.5 A, so there is
        # real charge to account for (the startup rest is at 0 A).
        wait_for(c, lambda s: sum(1 for x in s["samples"]
                                  if abs(x[2]) >= 0.4) >= 3,
                 60, "main-phase current")
        stop_pressed = time.time()
        c.request_stop()
        snap = wait_for(c, lambda s: s["last_run"] is not None, 20,
                        "stopped")
        latency = time.time() - stop_pressed
        check("Stop ends the run with a 'stopped' verdict",
              snap["last_run"]["status"] == "stopped", snap["last_run"])
        check("Stop takes effect within 3 s (took %.1f s)" % latency,
              latency < 3.0)
        check("unsaved run reports no CSV", snap["last_run"]["csv"] is None)
        check("unsaved run still reports the charge it moved (%.5f Ah)"
              % snap["last_run"]["cumulative_ah"],
              abs(snap["last_run"]["cumulative_ah"]) > 1e-5)
        c.close_port()

        # ---- wrong port: must NOT claim to be connected ----
        webui.EBCA20.on_serial = classmethod(
            lambda cls, port, limits=None: webui.EBCA20(SilentTransport(),
                                                        limits))
        saved_timeout = webui.NO_ANSWER_S
        webui.NO_ANSWER_S = 1.0
        try:
            c.open_port("WRONG")
            time.sleep(0.4)
            check("silent port is not reported as connected",
                  c.snapshot(0)["status"] == "connecting")
            snap = wait_for(c, lambda s: s["status"] == "error", 5,
                            "wrong-port error")
            check("silent port ends in an error naming the likely cause",
                  "wrong port" in snap["message"].lower())
            refused = False
            try:
                c.start_test(CONNECTION_TEST,"LP604765", save=False)
            except RuntimeError:
                refused = True
            check("cannot start a test on a port that never answered",
                  refused or c.snapshot(0)["status"] != "running")
        finally:
            webui.NO_ANSWER_S = saved_timeout
            c.close_port()
    finally:
        webui.EBCA20.on_serial = original
        for path in created:
            base = path[:-4]
            for suffix in (".csv", "_meta.json", "_checkpoint.json"):
                try:
                    os.remove(base + suffix)
                except OSError:
                    pass
    print("    WEBUI END-TO-END CHECKS PASSED")


def run_port_exclusivity():
    """A second launch must fail to bind, so it can find the first instead.

    Regression for Windows SO_REUSEADDR, which let a second process bind a
    listening port and start a silent twin server.
    """
    print("    port exclusivity:")
    first = webui.LocalServer(("127.0.0.1", HTTP_PORT + 1), webui.Handler)
    try:
        refused = False
        try:
            second = webui.LocalServer(("127.0.0.1", HTTP_PORT + 1),
                                       webui.Handler)
            second.server_close()
        except OSError:
            refused = True
        check("a second server cannot bind the same port", refused)
    finally:
        first.server_close()
    print("    PORT EXCLUSIVITY CHECKS PASSED")


if __name__ == "__main__":
    main()
    run_end_to_end()
    run_port_exclusivity()
