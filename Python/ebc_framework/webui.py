#!/usr/bin/env python3
"""Local browser interface for the EBC-A20.

Start it, open the page it prints, pick port / cell / test, press Start.

    python webui.py
    python webui.py --port COM5          # pre-select a port
    python webui.py --no-browser         # do not open a browser window

Why a server at all, rather than a static page: a browser cannot touch a
serial port, and the tester's port is exclusive -- exactly one process may
hold it. So this process owns the device and the page is a thin client that
polls it. While this UI runs, launchTest.py cannot use the same port, and
the other way round.

Only the loopback interface is bound. An interface that commands current
into a cell has no business being reachable from the lab network.

Runs go through exactly the same code as the command line
(run_with_reconnect -> run_test), so safety limits, the startup link and
write-path checks, auto-reconnect, checkpoints and the cumulative_ah column
all behave identically. Stop is routed through the same KeyboardInterrupt
path that Ctrl-C uses in a terminal.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import launchTest as LT
from ebc.celllib import CellLibrary
from ebc.device import EBCA20
from ebc.experiment import DataLogger
from ebc.runner import TestDef
from ebc import protocol as P

STATIC_DIR = os.path.join(HERE, "webui_static")
MAX_SAMPLES = 20000          # ring cap, about 5.5 h at 1 Hz
IDLE_POLL_S = 0.3
NO_ANSWER_S = 6.0            # the tester emits a frame every 1-2 s
# No folder separators, and the first character may not be a dot. That last
# part matters: ".." passes a naive character-class check and lands as
# "...json" inside the folder -- inside, but not a definition.
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


class AbortRun(KeyboardInterrupt):
    """Raised in the sample callback when the user presses Stop.

    Subclasses KeyboardInterrupt deliberately: run_with_reconnect already
    treats that as "stop cleanly, send STOP, keep the checkpoint", which is
    the terminal's Ctrl-C behaviour and is already well tested.
    """


class NullLogger:
    rows = 0

    def log(self, m, t_elapsed, **extra):
        pass

    def close(self):
        pass


class Controller:
    """Owns the serial device. Exactly one worker thread ever touches it."""

    def __init__(self, cells_dir: str):
        self._lock = threading.Lock()
        self._cells_dir = cells_dir
        self._thread = None
        self._stop_idle = threading.Event()
        self._abort = threading.Event()
        self.port = None
        self._clear(status="disconnected", message="")

    def _clear(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        self.voltage = None
        self.current = None
        self.mode = None
        self.test_name = None
        self.cell_id = None
        self.elapsed_s = 0.0
        self.cumulative_ah = 0.0
        self.csv_path = None
        self.samples = []
        self.sample_base = 0
        # The verdict of the most recent run. Kept apart from `status`,
        # which goes back to "idle" as soon as the reader resumes -- without
        # this a run that ended on a safety stop would come back looking fine.
        self.last_run = None

    def _push_sample(self, t, v, i) -> None:
        self.samples.append((round(t, 2), round(v, 4), round(i, 4)))
        if len(self.samples) > MAX_SAMPLES:
            drop = len(self.samples) - MAX_SAMPLES
            del self.samples[:drop]
            self.sample_base += drop

    def snapshot(self, since: int) -> dict:
        with self._lock:
            start = max(since - self.sample_base, 0)
            running = self._thread is not None and self._thread.is_alive()
            return {
                "status": self.status,
                "message": self.message,
                "port": self.port,
                "voltage": self.voltage,
                "current": self.current,
                "mode": self.mode,
                "test": self.test_name,
                "cell": self.cell_id,
                "elapsed_s": round(self.elapsed_s, 1),
                "cumulative_ah": round(self.cumulative_ah, 5),
                "csv": self.csv_path,
                "samples": self.samples[start:],
                "next_index": self.sample_base + len(self.samples),
                "worker_alive": running,
                "last_run": self.last_run,
            }

    # ---------------- port ----------------

    def open_port(self, port: str) -> None:
        self.close_port()
        with self._lock:
            self.port = port
            self._clear(status="connecting", message="Opening port...")
        self._stop_idle.clear()
        self._abort.clear()
        self._thread = threading.Thread(
            target=self._idle_loop, args=(port,), daemon=True)
        self._thread.start()

    def close_port(self) -> None:
        self._stop_idle.set()
        self._abort.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=20)
        self._thread = None
        self._abort.clear()
        with self._lock:
            self.status = "disconnected"
            self.message = ""
            self.voltage = self.current = self.mode = None

    def _idle_loop(self, port: str) -> None:
        """Read V and I with no current commanded, until a test starts.

        "Connected" is only claimed once a valid status frame has arrived.
        Opening a port proves nothing -- a wrong port opens just as happily
        -- and on a Mac the /dev/cu.* list is easy to misread. This mirrors
        the read-path half of the command line's startup link check.
        """
        dev = None
        try:
            dev = EBCA20.on_serial(port)
            dev.connect()
            with self._lock:
                self.status = "connecting"
                self.message = "Port open. Waiting for the tester to answer..."
            opened = time.time()
            last_frame = None
            while not self._stop_idle.is_set():
                for frame in dev._t.read_frames():
                    m = P.parse_status(frame)
                    if m is None:
                        continue
                    last_frame = time.time()
                    with self._lock:
                        self.voltage = m.voltage
                        self.current = m.signed_current
                        self.mode = m.mode
                        if self.status != "idle":
                            self.status = "idle"
                            self.message = ("Connected. Idle, no current "
                                            "commanded.")
                now = time.time()
                if last_frame is None and now - opened > NO_ANSWER_S:
                    raise RuntimeError(
                        "the port opened but nothing answered within %g s. "
                        "Wrong port, or the tester is switched off."
                        % NO_ANSWER_S)
                if last_frame is not None and now - last_frame > NO_ANSWER_S:
                    with self._lock:
                        self.status = "connecting"
                        self.message = ("No data from the tester for %d s. "
                                        "Check the USB cable."
                                        % (now - last_frame))
                time.sleep(IDLE_POLL_S)
        except Exception as exc:
            with self._lock:
                self.status = "error"
                # Our own RuntimeErrors are written for people; keep the
                # class name only for unexpected ones, where it helps.
                self.message = (str(exc) if isinstance(exc, RuntimeError)
                                else "%s: %s" % (type(exc).__name__, exc))
        finally:
            if dev is not None:
                try:
                    dev.disconnect()
                except Exception:
                    pass

    # ---------------- running ----------------

    def start_test(self, test_value: str, cell_id, save: bool) -> None:
        with self._lock:
            if self.status == "running":
                raise RuntimeError("a test is already running")
            # Enforced here, not only by the page greying out the button:
            # never drive current through a port that has not answered.
            if self.status in ("disconnected", "connecting", "error"):
                raise RuntimeError(
                    "the tester is not answering on this port; connect and "
                    "wait for live readings first")
            port = self.port
        if not port:
            raise RuntimeError("no serial port selected")

        test = TestDef.load(LT.resolve_test_path(test_value))
        library = CellLibrary(self._cells_dir)
        chosen = cell_id or test.cell_id
        if not chosen:
            raise RuntimeError("this test does not name a cell; choose one")
        cell = library.get(chosen)

        # Hand the port from the idle reader to the run.
        self._stop_idle.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=20)
        self._abort.clear()

        with self._lock:
            self._clear(status="running", message="Starting...")
            self.test_name = test.name
            self.cell_id = cell.id

        self._thread = threading.Thread(
            target=self._run, args=(test, cell, port, save), daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._abort.set()

    def _run(self, test, cell, port: str, save: bool) -> None:
        controller = self
        out = None
        checkpoint_path = None
        real_logger = NullLogger()
        try:
            if save:
                folder = os.path.join(LT.RESULTS_DIR,
                                      LT.result_folder_name(test.name))
                os.makedirs(folder, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                out = os.path.join(folder, "%s_%s.csv" % (test.name, stamp))
                checkpoint_path = os.path.splitext(out)[0] + "_checkpoint.json"
                meta_path = os.path.splitext(out)[0] + "_meta.json"
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump({
                        "test": test.raw,
                        "cell": {"id": cell.id,
                                 "nominal_capacity_ah": cell.nominal_capacity_ah},
                        "run": {"timestamp": stamp,
                                "backend": "serial %s" % port,
                                "started_from": "webui",
                                "csv_file": os.path.basename(out)},
                    }, fh, indent=2)
                with controller._lock:
                    controller.csv_path = out
                real_logger = DataLogger(out)

            class UILogger:
                """Mirror every logged sample into the page's state.

                Charge is integrated here rather than read from the CSV
                logger, so the page shows it even when saving is off. Same
                rule as DataLogger: zero-order hold on each interval's
                starting current, over t_s, which is frozen during
                reconnects.
                """

                def __init__(self, inner):
                    self._inner = inner
                    self._cum_ah = 0.0
                    self._last_t = None
                    self._last_i = 0.0

                def log(self, m, t_elapsed, **extra):
                    self._inner.log(m, t_elapsed, **extra)
                    if self._last_t is not None:
                        dt = t_elapsed - self._last_t
                        if 0.0 < dt <= 3600.0:
                            self._cum_ah += self._last_i * dt / 3600.0
                    self._last_t = t_elapsed
                    self._last_i = m.signed_current
                    with controller._lock:
                        controller.voltage = m.voltage
                        controller.current = m.signed_current
                        controller.mode = m.mode
                        controller.elapsed_s = t_elapsed
                        controller.cumulative_ah = self._cum_ah
                        controller._push_sample(t_elapsed, m.voltage,
                                                m.signed_current)
                    if controller._abort.is_set():
                        raise AbortRun()

                @property
                def rows(self):
                    return self._inner.rows

                def close(self):
                    self._inner.close()

            logger = UILogger(real_logger)
            checkpoint = {"csv_path": out, "test": test.raw,
                          "cell_id": cell.id, "runner_state": {},
                          "complete": False}
            last_write = {"wall": 0.0, "stage": None}

            def save_progress(runner_state: dict) -> None:
                checkpoint["runner_state"] = copy.deepcopy(runner_state)
                checkpoint["saved_at_epoch"] = time.time()
                if controller._abort.is_set():
                    raise AbortRun()
                if not save:
                    return
                stage = runner_state.get("stage")
                now = time.time()
                if (stage == last_write["stage"]
                        and now - last_write["wall"] < 10.0):
                    return
                LT.write_json_atomic(checkpoint_path, checkpoint)
                last_write.update({"wall": now, "stage": stage})

            def build_device():
                return EBCA20.on_serial(port, limits=cell.safety_limits())

            with controller._lock:
                controller.message = "Running"

            result = LT.run_with_reconnect(
                build_device(), build_device, test, cell, logger, checkpoint,
                checkpoint_path, None, save_progress,
                auto_reconnect=True, backoff_s=5.0, max_attempts=0)

            with controller._lock:
                if controller._abort.is_set() or result is None:
                    controller.status = "stopped"
                    controller.message = "Stopped. The tester received STOP."
                else:
                    controller.status = "finished"
                    controller.message = str(getattr(result, "reason",
                                                     "complete"))
        except AbortRun:
            with controller._lock:
                controller.status = "stopped"
                controller.message = "Stopped. The tester received STOP."
        except Exception as exc:
            with controller._lock:
                controller.status = "error"
                controller.message = "%s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                real_logger.close()
            except Exception:
                pass
            self._abort.clear()
            with controller._lock:
                controller.last_run = {
                    "status": controller.status,
                    "message": controller.message,
                    "test": controller.test_name,
                    "cell": controller.cell_id,
                    "csv": controller.csv_path,
                    "elapsed_s": round(controller.elapsed_s, 1),
                    "cumulative_ah": round(controller.cumulative_ah, 5),
                    "ended": time.strftime("%H:%M:%S"),
                }
            self._stop_idle.clear()
            self._thread = threading.Thread(
                target=self._idle_after, args=(port,), daemon=True)
            self._thread.start()

    def _idle_after(self, port: str) -> None:
        """Resume showing V and I once the run has released the port."""
        time.sleep(1.5)
        if self._stop_idle.is_set():
            return            # a new run or a disconnect got here first
        self._idle_loop(port)


# ---------------- catalogue helpers ----------------

def list_ports() -> list:
    from serial.tools import list_ports as lp
    out = []
    for p in sorted(lp.comports(), key=lambda x: x.device):
        text = ("%s %s %s" % (p.device, p.description, p.hwid)).lower()
        if sys.platform == "darwin" and "/dev/tty." in p.device:
            continue          # the cu.* twin is the one to open
        likely = ("ch34" in text) or ("usbserial" in text)
        out.append({"device": p.device, "description": p.description,
                    "likely": likely})
    return out


def list_cells(cells_dir: str) -> list:
    library = CellLibrary(cells_dir)
    out = []
    for cid in library.ids():
        c = library.get(cid)
        out.append({"id": cid, "capacity_ah": c.nominal_capacity_ah,
                    "min_v": c.min_v, "max_v": c.max_v,
                    "chemistry": c.chemistry})
    return out


def list_tests() -> list:
    out = []
    for root, _dirs, files in os.walk(LT.TESTS_DIR):
        for fn in sorted(files):
            if not fn.endswith(".json"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, LT.TESTS_DIR).replace(os.sep, "/")
            entry = {"value": rel[:-5], "type": "?", "cell": None,
                     "description": ""}
            try:
                raw = json.load(open(full, encoding="utf-8"))
                entry["type"] = raw.get("type", "?")
                entry["cell"] = raw.get("cell") or None
                entry["description"] = raw.get("description", "")
            except Exception as exc:
                entry["description"] = "unreadable: %s" % exc
            out.append(entry)
    return out


def validate_definition(kind, payload):
    """Reject a bad definition here rather than at the tester.

    Validation runs the production constructors rather than a hand-written
    copy of their rules, so whatever would fail at launch fails now. For a
    cell that is Cell.from_dict, exactly what CellLibrary itself calls.
    """
    if not isinstance(payload, dict):
        raise ValueError("the definition must be a JSON object")

    if kind == "cell":
        from ebc.celllib import Cell
        if not str(payload.get("id", "")).strip():
            raise ValueError('a cell needs a non-empty "id"')
        cell = Cell.from_dict(payload)        # raises on a bad definition
        if cell.nominal_capacity_ah <= 0:
            raise ValueError("nominal_capacity_ah must be above zero")
        if cell.min_v >= cell.max_v:
            raise ValueError("voltage.min_v must be below voltage.max_v")
        if cell.hard_min_v > cell.min_v or cell.hard_max_v < cell.max_v:
            raise ValueError("the hard limits must sit outside min_v/max_v")
        cell.safety_limits()                  # the runner builds these too
        return

    if not str(payload.get("name", "")).strip():
        raise ValueError('a test needs a non-empty "name"')
    if not str(payload.get("type", "")).strip():
        raise ValueError('a test needs a "type"')
    test = TestDef(payload)
    if test.sample_period_s <= 0:
        raise ValueError("sample_period_s must be above zero")
    known = {"charge", "discharge", "dst", "hppc", "rest", "cycle",
             "protocol"}
    if test.type not in known:
        raise ValueError('unknown type "%s"; expected one of %s'
                         % (test.type, ", ".join(sorted(known))))


def save_json_definition(kind, name, payload):
    """Validate, then write atomically into config/cells or config/tests."""
    if kind not in ("cell", "test"):
        raise ValueError('kind must be "cell" or "test"')
    if not SAFE_NAME.match(name or ""):
        raise ValueError("the file name may contain only letters, digits, "
                         "dot, dash and underscore -- no folders")

    validate_definition(kind, payload)

    directory = LT.CELLS_DIR if kind == "cell" else LT.TESTS_DIR
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(directory, name + ".json")
    # Belt and braces over SAFE_NAME: the result must stay in the folder.
    if os.path.dirname(os.path.abspath(final)) != os.path.abspath(directory):
        raise ValueError("bad path")
    tmp = final + ".%d.tmp" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return final


# ---------------- HTTP ----------------

class LocalServer(ThreadingHTTPServer):
    """Loopback HTTP server that refuses to share its port.

    Python's HTTP server turns on SO_REUSEADDR. On Windows that option lets
    a second process bind a port that is already listening, so a second
    double-click on the launcher silently started a twin server instead of
    failing and finding the first -- two interfaces on one port, each able
    to grab the serial port. Confirmed on Windows 11, 2026-10-31.

    POSIX SO_REUSEADDR never allows two listeners, and is still wanted on
    macOS/Linux so the interface can be restarted straight after closing.
    """

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32":
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    controller: Controller = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                  # keep the console for the test output

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        route = urlparse(self.path)
        path = route.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._static("app.js",
                                    "text/javascript; charset=utf-8")
            if path == "/style.css":
                return self._static("style.css", "text/css; charset=utf-8")
            if path == "/api/bootstrap":
                return self._json({
                    "ports": list_ports(),
                    "cells": list_cells(LT.CELLS_DIR),
                    "tests": list_tests(),
                    "platform": sys.platform,
                })
            if path == "/api/state":
                since = int((parse_qs(route.query).get("since") or ["0"])[0])
                return self._json(self.controller.snapshot(since))
            if path == "/api/definition":
                q = parse_qs(route.query)
                kind = (q.get("kind") or [""])[0]
                name = (q.get("name") or [""])[0]
                base = LT.CELLS_DIR if kind == "cell" else LT.TESTS_DIR
                full = os.path.join(base, name + ".json")
                if os.path.commonpath([os.path.abspath(base),
                                       os.path.abspath(full)]) != \
                        os.path.abspath(base):
                    return self._json({"error": "bad path"}, 400)
                with open(full, encoding="utf-8") as fh:
                    return self._json({"json": json.load(fh)})
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/open":
                self.controller.open_port(body["port"])
                return self._json({"ok": True})
            if path == "/api/close":
                self.controller.close_port()
                return self._json({"ok": True})
            if path == "/api/start":
                self.controller.start_test(body["test"], body.get("cell"),
                                           bool(body.get("save", True)))
                return self._json({"ok": True})
            if path == "/api/stop":
                self.controller.request_stop()
                return self._json({"ok": True})
            if path == "/api/definition":
                written = save_json_definition(body["kind"], body["name"],
                                               body["json"])
                return self._json({"ok": True, "path": written})
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 400)

    def _static(self, name: str, ctype: str) -> None:
        full = os.path.join(STATIC_DIR, name)
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


def _say(text: str) -> None:
    """Print, but never let a dying console stop the shutdown."""
    try:
        print(text, flush=True)
    except Exception:
        pass


def shut_down(controller, reason: str) -> None:
    """Stop any run so the tester receives STOP, then release the port.

    STOP is requested before anything is printed: while a console window
    is closing, writing to it can raise, and that must not be able to skip
    the one step that matters.
    """
    controller.request_stop()
    _say("\n%s: sending STOP if a test is running..." % reason)
    thread = controller._thread
    deadline = time.time() + 4.0
    while (thread is not None and thread.is_alive()
           and controller.status == "running" and time.time() < deadline):
        time.sleep(0.05)
    controller.close_port()
    _say("Port released.")


def install_close_hooks(controller) -> None:
    """Stop the tester when the launcher window is closed, not only on Ctrl-C.

    Double-click launchers invite closing the window with its X button.
    Without this, that kills Python before STOP is sent -- the same failure
    as VS Code's force-terminate. The cell stays protected by the cutoff
    embedded in each command, but the run would not end where intended.

    Windows: closing the console delivers CTRL_CLOSE_EVENT and allows about
    five seconds; a run takes well under one to stop.
    macOS / Linux: closing the Terminal window delivers SIGHUP.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CLOSE_EVENTS = {2, 5, 6}      # close, logoff, shutdown

        def on_console_event(event):
            if event in CLOSE_EVENTS:
                shut_down(controller, "Window closing")
                return True
            return False              # leave Ctrl-C to KeyboardInterrupt

        # Keep a reference: a collected callback would crash the process.
        install_close_hooks._handler = HANDLER(on_console_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(
            install_close_hooks._handler, True)
    else:
        import signal

        def on_signal(signum, _frame):
            shut_down(controller, "Window closed")
            os._exit(0)

        for name in ("SIGHUP", "SIGTERM"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), on_signal)


def already_running(url: str) -> bool:
    """Is our own interface already answering on this port?"""
    import urllib.request
    try:
        with urllib.request.urlopen(url + "api/state?since=0",
                                    timeout=2) as r:
            return "status" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--http-port", type=int, default=8765,
                    help="port for this web interface (default: 8765)")
    ap.add_argument("--port", help="serial port to pre-select")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser window")
    args = ap.parse_args(argv)
    url = "http://127.0.0.1:%d/" % args.http_port

    controller = Controller(LT.CELLS_DIR)
    Handler.controller = controller

    # Loopback only: never expose current control to the network.
    try:
        server = LocalServer(("127.0.0.1", args.http_port), Handler)
    except OSError:
        if already_running(url):
            # A second double-click on the launcher: reuse the first one.
            print("The interface is already running; opening it: %s" % url)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print("Port %d is used by another program. Start with "
              "--http-port 8766 (or any free port)." % args.http_port)
        return 1

    if args.port:
        controller.open_port(args.port)
    install_close_hooks(controller)

    print("EBC-A20 web interface: %s" % url)
    print("Keep this window open while you work. It owns the serial port.")
    print("To quit: press Ctrl-C here, or close the window. Either one "
          "sends STOP first.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shut_down(controller, "Ctrl-C")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
