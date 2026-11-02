"""Offline checks for ebc/plan.py: previews and progress estimates."""
import glob
import math
import os

import _paths  # noqa: F401  (puts the framework on sys.path)

from ebc.celllib import CellLibrary
from ebc.plan import plan_test, progress
from ebc.runner import TestDef

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.path.dirname(HERE)
LIB = CellLibrary(os.path.join(FW, "config", "cells"))


FIXTURES = os.path.join(HERE, "fixtures")


def test_def(name):
    # Fixtures, not config/tests: those are examples users edit and delete.
    return TestDef.load(os.path.join(FIXTURES, name + ".json"))


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError("%s %s" % (label, detail))
    print("      %s" % label)


def every_config_plans():
    # Whatever test files the user currently keeps, plus the fixtures.
    paths = sorted(glob.glob(os.path.join(FW, "config", "tests", "**", "*.json"),
                             recursive=True)
                   + glob.glob(os.path.join(FIXTURES, "*.json")))
    count = 0
    for cid in LIB.ids():
        cell = LIB.get(cid)
        for path in paths:
            p = plan_test(TestDef.load(path), cell)
            d = p.to_dict()
            assert p.segments, path
            assert all(s.duration_s >= 0 for s in p.segments), path
            assert abs(p.total_s - sum(s.duration_s for s in p.segments)) < 1e-6
            assert d["points"] and len(d["points"]) <= 4001, path
            # No planned current may exceed what the device would apply.
            for s in p.segments:
                if s.current_a > 0:
                    assert s.current_a <= min(cell.max_charge_a, 5.0) + 1e-9, (path, cid)
                elif s.current_a < 0:
                    assert -s.current_a <= min(cell.max_discharge_a, 20.0) + 1e-9, (path, cid)
            count += 1
    check("all %d test files plan on all %d cells (%d plans), currents "
          "within the device limits" % (len(paths), len(LIB.ids()), count),
          True)


def exact_durations():
    cell = LIB.get("LP604765")
    p = plan_test(test_def("connection_test"), cell)
    check("connection_test is exact: 10 s + 60 s + 60 s = 130 s",
          p.exact and abs(p.total_s - 130) < 1e-6, p.total_s)
    p = plan_test(test_def("dst_2c_check"), cell)
    check("dst_2c_check is exact: 10 s + 3 x 92 s = 286 s",
          p.exact and abs(p.total_s - 286) < 1e-6, p.total_s)
    peak = max(abs(a) for _, a in p.to_dict()["points"])
    check("dst_2c_check peak is 2C = 3.80 A", abs(peak - 3.8) < 1e-6, peak)


def cycle_1c_contents():
    cell = LIB.get("LP604765")
    p = plan_test(test_def("cycle_1C_discharge_charge"), cell)
    kinds = [s.kind for s in p.segments]
    check("1C cycle: rest, discharge, rest, charge CC, charge CV, rest",
          kinds == ["rest", "discharge", "rest", "charge", "charge", "rest"],
          kinds)
    d = p.segments[1]
    check("discharge is -1.90 A and ends at 2.800 V",
          abs(d.current_a + 1.9) < 1e-9 and "2.800 V" in d.until, d.until)
    cv = p.segments[4]
    check("CV tapers to 0.05C = 0.095 A",
          cv.cccv == "cv" and abs(cv.i_cut - 0.095) < 1e-9, cv.i_cut)
    check("discharge and charge lengths are flagged as estimates",
          not d.exact and not cv.exact and not p.exact)
    check("rests are exact (600 s each)",
          p.segments[2].exact and abs(p.segments[2].duration_s - 600) < 1e-9)


def capping_changes_duration():
    cell = LIB.get("cellAlex")            # 7 Ah: 2C = 14 A, tester max 5 A
    p = plan_test(test_def("cc_cv_charge"), cell)
    cc = next(s for s in p.segments if s.cccv == "cc")
    check("2C charge on a 7 Ah cell is capped to the tester's 5 A",
          abs(cc.current_a - 5.0) < 1e-9, cc.current_a)
    check("a note explains the cap",
          any("capped" in n for n in p.notes), p.notes)
    # CC estimate must use the capped current: f_cc * Q / I with I = 5 A.
    c_rate = 5.0 / 7.0
    f_cc = min(max(0.95 - 0.15 * c_rate, 0.5), 0.95)
    want = 3600 * f_cc * 7.0 / 5.0
    check("the duration estimate uses the capped current",
          abs(cc.duration_s - want) < 1.0, (cc.duration_s, want))


def progress_mapping():
    cell = LIB.get("LP604765")
    p = plan_test(test_def("cycle_1C_discharge_charge"), cell)
    seg = p.segments
    total = p.total_s

    pr = progress(p, None, 5.0)
    check("before the main phase: initial rest, by elapsed time",
          pr["index"] == 0 and abs(pr["fraction"] - 5.0 / total) < 1e-3)

    half = seg[1].duration_s / 2
    pr = progress(p, {"stage": "discharge", "cycle_index": 0,
                      "phase_index": 0, "phase_elapsed_s": half}, 0)
    want = (seg[0].duration_s + half) / total
    check("halfway through the discharge", pr["index"] == 1
          and abs(pr["fraction"] - want) < 1e-3, pr)

    pr = progress(p, {"stage": "charge", "cycle_index": 0, "phase_index": 1,
                      "phase_elapsed_s": 60}, 0, voltage=3.9, current=1.9)
    check("charging below v_max is the CC phase", pr["index"] == 3, pr)

    cv = seg[4]
    i_mid = math.sqrt(cv.i_cc * cv.i_cut)          # halfway on a log scale
    pr = progress(p, {"stage": "charge", "cycle_index": 0, "phase_index": 1,
                      "phase_elapsed_s": 5000}, 0, voltage=4.2, current=i_mid)
    done = sum(s.duration_s for s in seg[:4])
    want = (done + 0.5 * cv.duration_s) / total
    check("at v_max the CV phase is tracked by current, not time",
          pr["index"] == 4 and abs(pr["fraction"] - want) < 1e-3, pr)

    pr = progress(p, {"stage": "between_phases", "cycle_index": 0,
                      "phase_index": 1, "phase_elapsed_s": 0}, 0)
    check("between phases points at the start of the next one",
          pr["index"] == 3, pr)

    pr = progress(p, {"stage": "discharge", "cycle_index": 0,
                      "phase_index": 0, "phase_elapsed_s": 1e9}, 0)
    check("an overrunning estimated phase is held short of done",
          pr["fraction"] < (seg[0].duration_s + seg[1].duration_s) / total)

    # A walk through the whole run never moves the bar backwards.
    states = [(None, 0, None, None), (None, 9, None, None)]
    for k in range(0, 101, 10):
        states.append(({"stage": "discharge", "cycle_index": 0, "phase_index": 0,
                        "phase_elapsed_s": seg[1].duration_s * k / 100}, 0, 3.5, -1.9))
    for k in range(0, 101, 20):
        states.append(({"stage": "rest_after_discharge", "cycle_index": 0,
                        "phase_index": 0, "phase_elapsed_s": 6 * k}, 0, 3.4, 0))
    states.append(({"stage": "between_phases", "cycle_index": 0,
                    "phase_index": 1, "phase_elapsed_s": 0}, 0, 3.4, 0))
    for k in range(0, 101, 10):
        states.append(({"stage": "charge", "cycle_index": 0, "phase_index": 1,
                        "phase_elapsed_s": seg[3].duration_s * k / 100}, 0, 3.9, 1.9))
    for i in (1.9, 1.2, 0.6, 0.3, 0.15, 0.1):
        states.append(({"stage": "charge", "cycle_index": 0, "phase_index": 1,
                        "phase_elapsed_s": 9e3}, 0, 4.2, i))
    for k in range(0, 101, 20):
        states.append(({"stage": "rest_after_charge", "cycle_index": 0,
                        "phase_index": 1, "phase_elapsed_s": 6 * k}, 0, 4.1, 0))
    fracs = [progress(p, st, el, v, i)["fraction"] for st, el, v, i in states]
    check("a full walk through the run only ever moves forward "
          "(%.3f -> %.3f)" % (fracs[0], fracs[-1]),
          all(b >= a - 1e-9 for a, b in zip(fracs, fracs[1:])), fracs)


def main():
    print("    plan:")
    every_config_plans()
    exact_durations()
    cycle_1c_contents()
    capping_changes_duration()
    print("    progress:")
    progress_mapping()
    print("    PLAN CHECKS PASSED")


if __name__ == "__main__":
    main()
