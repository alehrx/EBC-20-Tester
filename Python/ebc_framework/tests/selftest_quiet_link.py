"""Offline regressions for quiet periods on the serial link.

The tester regularly stops reporting for a few seconds while it keeps
driving the commanded current. These checks pin down how that is handled:
a short quiet period is waited out (no STOP, no reconnect) and reported
with what arrived meanwhile; a long one still raises, and the error says
whether the line was silent, carried valid frames without measurements, or
carried garbage -- which is what separates a silent tester from a stalled
link or a framing problem.

The real SerialTransport, FrameReassembler and EBCA20.run are used; only
the pyserial port underneath is replaced by a scripted fake.
"""
from __future__ import annotations

import contextlib
import io
import sys
import time
from types import SimpleNamespace

import _paths  # noqa: F401  (adds framework root to sys.path)
from ebc import protocol as P
from ebc.device import EBCA20


def status_frame(volts=3.7) -> bytes:
    """An idle status frame (0 A, so it agrees with no current commanded)."""
    v1, v2 = divmod(int(round(volts * 1000)), 240)
    payload = bytes([P.PT_STATUS_IDLE, 0, 0, v1, v2, 0, 0, 0, 0,
                     0, 0, 0, 0, 0, 0, P.DEVICE_TYPE_A20])
    return bytes([P.SOF]) + payload + bytes([P.crc(payload), P.EOF])


def fw_frame(pt=0x66) -> bytes:
    """A firmware-report frame: valid checksum, but no measurement in it."""
    payload = bytes([pt, 0, 0, 0x0F, 0xCB, 0, 0, 0, 0, 0x01, 0x3E, 0x0C,
                     0x6D, 0x08, 0xBA, 0x09])
    return bytes([P.SOF]) + payload + bytes([P.crc(payload), P.EOF])


def bad_frame() -> bytes:
    good = bytearray(status_frame())
    good[17] ^= 0x55                      # corrupt the checksum
    return bytes(good)


class ScriptedPort:
    """Stands in for serial.Serial. One frame per read, except while quiet."""

    def __init__(self, quiet_from, quiet_to, during):
        self.t0 = time.time()
        self.quiet_from, self.quiet_to, self.during = quiet_from, quiet_to, during

    def read(self, n):
        el = time.time() - self.t0
        if self.quiet_from <= el < self.quiet_to:
            return {"nothing": b"", "fw": fw_frame(),
                    "garbage": bad_frame()}[self.during]
        return status_frame()

    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass


@contextlib.contextmanager
def scripted_transport(quiet_from, quiet_to, during):
    """A real SerialTransport whose pyserial port is the scripted fake."""
    fake = SimpleNamespace(
        Serial=lambda **kw: ScriptedPort(quiet_from, quiet_to, during),
        EIGHTBITS=8, PARITY_ODD="O", STOPBITS_ONE=1)
    saved = sys.modules.get("serial")
    sys.modules["serial"] = fake
    try:
        from ebc.transport import SerialTransport
        yield SerialTransport("FAKE")
    finally:
        if saved is not None:
            sys.modules["serial"] = saved
        else:
            sys.modules.pop("serial", None)


def run_for(transport, seconds, timeout_s):
    dev = EBCA20(transport)
    dev.QUIET_REPORT_S = 0.3
    start = time.time()
    ctrl = lambda m, t: float("nan") if time.time() - start > seconds else None
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        reason = dev.run(ctrl, period_s=0.1, no_response_timeout_s=timeout_s)
    return dev, reason, out.getvalue()


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError("%s %s" % (label, detail))
    print("      %s" % label)


def stats_are_counted():
    with scripted_transport(0, 0, "nothing") as t:
        chunks = iter([status_frame() + fw_frame(), bad_frame(), b"",
                       fw_frame(0x64)])
        t._ser = SimpleNamespace(read=lambda n: next(chunks))
        for _ in range(4):
            list(t.read_frames())
        s = t.stats
    check("transport counts bytes, frames, and valid frames by type",
          s["bytes"] == 19 * 4 and s["frames"] == 4 and s["valid"] == 3
          and s["by_type"] == {P.PT_STATUS_IDLE: 1, 0x66: 1, 0x64: 1}, s)
    check("a frame with a bad checksum is counted as unreadable",
          s["invalid"] == 1, s)


def short_quiet_is_waited_out():
    with scripted_transport(0.5, 1.2, "fw") as t:
        dev, reason, printed = run_for(t, 2.2, timeout_s=1.6)
    check("a short quiet period does not raise (no STOP, no reconnect)",
          reason == "controller requested stop", reason)
    check("it is recorded (%s s)" % dev.quiet_periods,
          len(dev.quiet_periods) == 1 and dev.quiet_periods[0] < 1.6)
    check("and reported with what arrived meanwhile",
          "[link] no readings for" in printed and "0x66" in printed, printed)


def long_quiet_says_what_it_was():
    for during, expect in (("nothing", "nothing at all arrived"),
                           ("fw", "valid frames 0x66"),
                           ("garbage", "unreadable frame")):
        with scripted_transport(0.4, 30, during) as t:
            raised = None
            try:
                run_for(t, 10, timeout_s=1.0)
            except OSError as exc:
                raised = str(exc)
        check("a long quiet period with %s still raises, and says so"
              % {"nothing": "no bytes", "fw": "only firmware frames",
                 "garbage": "garbled frames"}[during],
              raised is not None and expect in raised, raised)


def frame(pt, amps=0.0, volts=3.9) -> bytes:
    i1, i2 = divmod(int(round(abs(amps) * 100)), 240)   # 0.01 A per count
    v1, v2 = divmod(int(round(volts * 1000)), 240)
    payload = bytes([pt, i1, i2, v1, v2, 0, 0, 0, 0,
                     0, 0, 0, 0, 0, 0, P.DEVICE_TYPE_A20])
    return bytes([P.SOF]) + payload + bytes([P.crc(payload), P.EOF])


class FixedPort:
    """Always reports the same frame; records everything written to it."""

    def __init__(self, reply):
        self.reply = reply
        self.writes = []

    def read(self, n):
        return self.reply

    def write(self, data):
        self.writes.append(bytes(data))

    def flush(self):
        pass

    def close(self):
        pass


@contextlib.contextmanager
def transport_on(port):
    fake = SimpleNamespace(Serial=lambda **kw: port, EIGHTBITS=8,
                           PARITY_ODD="O", STOPBITS_ONE=1)
    saved = sys.modules.get("serial")
    sys.modules["serial"] = fake
    try:
        from ebc.transport import SerialTransport
        yield SerialTransport("FAKE")
    finally:
        if saved is not None:
            sys.modules["serial"] = saved
        else:
            sys.modules.pop("serial", None)


def keepalive_writes(reply, setup, keepalive_s=0.3, seconds=1.3):
    """Writes made during a run, minus the setup command and the final STOP."""
    import os
    from ebc.celllib import CellLibrary
    cell = CellLibrary(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config", "cells")).get("LP604765")
    port = FixedPort(reply)
    with transport_on(port) as t:
        dev = EBCA20(t, limits=cell.safety_limits())
        dev.KEEPALIVE_S = keepalive_s
        setup(dev)
        command = port.writes[-1]
        start = time.time()
        ctrl = lambda m, tt: float("nan") if time.time() - start > seconds else None
        with contextlib.redirect_stdout(io.StringIO()):
            dev.run(ctrl, period_s=0.1, no_response_timeout_s=5)
    during = port.writes[1:-1]           # drop setup command and final STOP
    return command, during, dev.keepalives


def keepalive_behaviour():
    stop_frame = P.build_stop()

    cmd, during, n = keepalive_writes(
        frame(P.PT_STATUS_CHARGE_RUN, amps=1.9),
        lambda d: d.set_charge(1.9))
    check("while charging, the identical start frame is repeated "
          "(%d times in 1.3 s at a 0.3 s interval)" % n,
          n >= 2 and during and all(w == cmd for w in during), during)
    check("never as STOP plus start, which would interrupt the current",
          stop_frame not in during)

    cmd, during, n = keepalive_writes(
        frame(P.PT_STATUS_CHARGE_END, amps=0.05),
        lambda d: d.set_charge(1.9))
    check("not sent once the tester reports the charge as ended "
          "(so it cannot restart it)", n == 0 and not during, during)

    cmd, during, n = keepalive_writes(
        frame(P.PT_STATUS_IDLE, amps=0.0), lambda d: d.stop())
    check("at rest it repeats STOP, a no-op on an idle tester",
          n >= 2 and all(w == stop_frame for w in during), during)

    cmd, during, n = keepalive_writes(
        frame(P.PT_STATUS_CHARGE_RUN, amps=1.9),
        lambda d: d.set_charge(1.9), keepalive_s=0)
    check("can be switched off", n == 0 and not during, during)


def default_is_patient():
    import inspect
    default = inspect.signature(EBCA20.run).parameters[
        "no_response_timeout_s"].default
    check("in-run default waits 60 s before declaring the link dead",
          default == 60.0, default)


def main():
    print("    quiet link:")
    stats_are_counted()
    short_quiet_is_waited_out()
    long_quiet_says_what_it_was()
    default_is_patient()
    print("    keepalive:")
    keepalive_behaviour()
    print("    QUIET LINK CHECKS PASSED")


if __name__ == "__main__":
    main()
