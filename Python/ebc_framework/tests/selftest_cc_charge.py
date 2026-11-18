"""Offline checks for CC-only charging ("protocol": "cc").

The EBC-A20 only offers CC-CV, so a CC-only charge is ended by the software:
STOP as soon as the voltage reaches v_max, with the tester's own CV target
set a little higher so its CV hold never starts first. These checks watch
what the emulated tester receives to prove the STOP arrives while it is
still charging, and that an ordinary CC-CV charge is unchanged.
"""
import contextlib
import io
import os

import _paths  # noqa: F401  (adds framework root to sys.path)
from ebc import protocol as P
from ebc.celllib import CellLibrary
from ebc.device import EBCA20
from ebc.runner import TestDef, run_test, CC_ONLY_CV_MARGIN_V
from fake_tester import SimulatorTransport

# The tester reports voltage in 1 mV steps, so a reading of 4.200 V can come
# from a true 4.1995 V. Allow that much below the target.
RESOLUTION_V = 0.0015

CELL = CellLibrary(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "config", "cells")).get("LP604765")


class Watched(SimulatorTransport):
    """Notes the charge command's CV target, and the tester's state when the
    first STOP after it arrives."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cv_target = None
        self.stop_found_mode = None
        self.v_at_stop = None

    def write(self, frame):
        if len(frame) > 5 and frame[1] == P.PT_START_CHARGE:
            self.cv_target = P.decode_base240(frame[4], frame[5]) / 100.0
        elif (len(frame) > 1 and frame[1] == P.PT_STOP
              and self.cv_target is not None and self.stop_found_mode is None):
            self.stop_found_mode = self._mode
            self.v_at_stop = getattr(self, "_v_terminal", None)
        super().write(frame)


class Rows:
    def __init__(self):
        self.rows = 0
        self.samples = []

    def log(self, m, t, **extra):
        self.rows += 1
        self.samples.append((t, m.voltage, m.signed_current, m.mode))

    def close(self):
        pass


def run(raw):
    t = Watched(capacity_ah=1.9, soc0=0.85, emit_period_s=0.0,
                time_accel=100.0)
    dev = EBCA20(t, limits=CELL.safety_limits())
    log = Rows()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = run_test(TestDef(raw), CELL, dev, log)
    return t, log, result, out.getvalue()


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError("%s %s" % (label, detail))
    print("      %s" % label)


def charge_test(proto, rest=30):
    return {"name": "t", "type": "charge", "sample_period_s": 1,
            "max_duration_s": 10800, "current_unit": "C",
            "charge": {"protocol": proto, "c_rate": 1, "v_max": 4.2,
                       "cutoff_c": 0.05, "rest_after_s": rest}}


def cc_only_charge():
    t, log, result, printed = run(charge_test("cc"))
    check("the command's own CV target is raised to %.2f V"
          % (4.2 + CC_ONLY_CV_MARGIN_V),
          t.cv_target is not None
          and abs(t.cv_target - (4.2 + CC_ONLY_CV_MARGIN_V)) < 0.006,
          t.cv_target)
    check("our STOP arrives while the tester is still charging "
          "(at %.3f V, below its CV target)" % (t.v_at_stop or 0),
          t.stop_found_mode == "charge"
          and 4.2 - RESOLUTION_V <= t.v_at_stop < 4.2 + CC_ONLY_CV_MARGIN_V,
          (t.stop_found_mode, t.v_at_stop))
    charging =[s for s in log.samples if s[3] == "charge" and s[2] > 0.05]
    check("charging stays at constant current to the end (no CV taper)",
          charging and all(abs(s[2] - 1.9) < 0.02 for s in charging),
          sorted({round(s[2], 3) for s in charging}))
    after = [s for s in log.samples if s[0] > charging[-1][0]]
    check("then the rest follows at zero current",
          after and all(abs(s[2]) < 0.03 for s in after[2:]), after[:5])
    check("the console says it is CC only", "no CV phase" in printed)
    check("and the run completes normally",
          "rest complete" in result.reason, result.reason)


def cc_only_in_protocol():
    raw = {"name": "t", "type": "protocol", "sample_period_s": 1,
           "max_duration_s": 10800, "repeat": 1, "order": ["charge"],
           "discharge": None,
           "charge": {"protocol": "cc", "c_rate": 1, "v_max": 4.2,
                      "rest_after_s": 20}}
    t, log, result, printed = run(raw)
    check("inside a protocol too, STOP arrives while still charging",
          t.stop_found_mode == "charge"
          and 4.2 - RESOLUTION_V <= t.v_at_stop < 4.2 + CC_ONLY_CV_MARGIN_V,
          (t.stop_found_mode, t.v_at_stop))


def cc_cv_unchanged():
    t, log, result, printed = run(charge_test("cc_cv"))
    check("an ordinary CC-CV charge still targets exactly 4.20 V",
          abs(t.cv_target - 4.2) < 0.006, t.cv_target)
    check("and still lets the tester end the charge itself",
          t.stop_found_mode == "idle", t.stop_found_mode)


def unknown_protocol_refused():
    raised = None
    try:
        run(charge_test("constant"))
    except ValueError as exc:
        raised = str(exc)
    check("an unknown charge protocol is refused, naming the valid ones",
          raised is not None and '"cc"' in raised, raised)


def main():
    print("    CC-only charge:")
    cc_only_charge()
    cc_only_in_protocol()
    cc_cv_unchanged()
    unknown_protocol_refused()
    print("    CC CHARGE CHECKS PASSED")


if __name__ == "__main__":
    main()
