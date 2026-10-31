"""Preview a test before it runs, and estimate progress while it runs.

plan_test() turns a test definition and a cell into the sequence of phases
the runner will execute, using the runner's own rules: the same defaults,
the same C-rate scaling, the same per-phase time limits.

What can be known in advance is reported as exact: rests, profile steps,
pulse widths, fixed durations. What depends on how the cell behaves -- a
discharge that ends at a voltage, a CC-CV charge that ends at a taper
current -- is estimated from the nominal capacity and marked as such. The
estimates assume each such phase starts from the opposite end of its voltage
window (a discharge from full, a charge from empty), which is exactly the
situation inside a discharge/charge cycle. They are deliberately simple:
this is not a cell model, and a confident-looking simulation of a generic
cell would be worse than an honest estimate.

progress() maps the runner's own progress reports (stage, cycle and phase
indices, elapsed time in the phase) onto that plan, so the phase is always
known exactly and only the position inside an estimated phase is estimated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

from .celllib import Cell
from .experiment import dst_profile, profile_from_entries
from .runner import TestDef, _charge_target_v, _discharge_target_v

MAX_PREVIEW_POINTS = 4000
DEFAULT_RECOVERY_REST_S = 600.0
CV_VOLTAGE_MARGIN_V = 0.015       # within this of v_max counts as CV
DEFAULT_DISCHARGE_LIMIT_S = 6 * 3600   # runner default for discharges


@dataclass
class Segment:
    label: str
    kind: str                       # rest | discharge | charge | profile | pulses
    current_a: float                # signed: + charge, - discharge (peak for profiles)
    duration_s: float
    exact: bool
    until: str                      # human-readable end condition
    match: dict = field(default_factory=dict)
    cccv: Optional[str] = None      # "cc" / "cv" for the two halves of a charge
    i_cc: Optional[float] = None
    i_cut: Optional[float] = None
    v_max: Optional[float] = None
    steps: Optional[list] = None    # [(current_a, duration_s), ...] for profiles


@dataclass
class Plan:
    test: str
    cell: str
    capacity_ah: float
    segments: list
    total_s: float
    exact: bool
    notes: list

    def to_dict(self) -> dict:
        out = asdict(self)
        out["points"] = preview_points(self)
        for seg in out["segments"]:
            seg.pop("steps", None)  # the chart gets them through "points"
        return out


# ---------------- estimates ----------------

def _window_fraction(cell: Cell, v_low: float, v_high: float) -> float:
    """Rough share of capacity between two voltages (linear in voltage).

    Crude by design -- real OCV curves are not linear, and an LFP plateau
    makes voltage almost useless as a SOC proxy -- but it gets a partial
    window like "charge to 3.8 V" into the right ballpark, which is all a
    duration estimate needs.
    """
    span = cell.max_v - cell.min_v
    if span <= 0:
        return 1.0
    lo = min(max(v_low, cell.min_v), cell.max_v)
    hi = min(max(v_high, cell.min_v), cell.max_v)
    return min(max((hi - lo) / span, 0.05), 1.0)


def _discharge_estimate_s(cell: Cell, amps: float, v_cutoff: float) -> float:
    amps = abs(amps)
    if amps <= 0:
        return 0.0
    q = cell.nominal_capacity_ah * _window_fraction(cell, v_cutoff, cell.max_v)
    return 3600.0 * q / amps


def _cccv_estimate_s(cell: Cell, i_cc: float, i_cut: float,
                     v_max: float) -> tuple[float, float]:
    """(CC seconds, CV seconds) for a CC-CV charge from the bottom.

    CC delivers most of the charge -- less at higher C-rates, because the
    IR rise brings the voltage to v_max sooner. The rest arrives during CV
    with a roughly exponential current decay from i_cc to i_cut.
    """
    q = cell.nominal_capacity_ah * _window_fraction(cell, cell.min_v, v_max)
    i_cc = abs(i_cc)
    if i_cc <= 0:
        return 0.0, 0.0
    c_rate = i_cc / cell.nominal_capacity_ah
    f_cc = min(max(0.95 - 0.15 * c_rate, 0.5), 0.95)
    t_cc = 3600.0 * f_cc * q / i_cc
    i_cut = abs(i_cut)
    if i_cut <= 0 or i_cut >= i_cc:
        return t_cc, 0.0
    tau = 3600.0 * (1.0 - f_cc) * q / (i_cc - i_cut)
    return t_cc, tau * math.log(i_cc / i_cut)


def _cap(estimate_s: float, limit_s) -> tuple[float, bool]:
    """Apply a phase time limit. Returns (duration, limited)."""
    if limit_s is not None and float(limit_s) < estimate_s:
        return float(limit_s), True
    return estimate_s, False


def _fmt_s(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s) if s else "%d min" % m
    return "%d s" % s


# ---------------- segment builders ----------------

def _rest(label, seconds, match):
    return Segment(label, "rest", 0.0, float(seconds), True,
                   "after %s" % _fmt_s(seconds), dict(match))


def _cccv(cell, i_cc, v_max, i_cut, limit_s, match, prefix="Charge"):
    t_cc, t_cv = _cccv_estimate_s(cell, i_cc, i_cut, v_max)
    out = []
    total, limited = _cap(t_cc + t_cv, limit_s)
    if limited:
        out.append(Segment(
            "%s %.2f A (CC-CV)" % (prefix, i_cc), "charge", abs(i_cc), total,
            True, "time limit %s, before reaching the %.3f A taper"
            % (_fmt_s(total), i_cut), dict(match), "cc", abs(i_cc), abs(i_cut),
            v_max))
        return out
    out.append(Segment(
        "%s CC %.2f A" % (prefix, i_cc), "charge", abs(i_cc), t_cc, False,
        "voltage reaches %.3f V" % v_max, dict(match), "cc", abs(i_cc),
        abs(i_cut), v_max))
    if t_cv > 0:
        out.append(Segment(
            "%s CV at %.3f V" % (prefix, v_max), "charge", abs(i_cc), t_cv,
            False, "current falls to %.3f A" % i_cut, dict(match), "cv",
            abs(i_cc), abs(i_cut), v_max))
    return out


def _discharge(cell, amps, v_cut, limit_s, match, label):
    est, limited = _cap(_discharge_estimate_s(cell, amps, v_cut), limit_s)
    until = ("time limit %s" % _fmt_s(est) if limited
             else "voltage falls to %.3f V" % v_cut)
    return Segment(label, "discharge", -abs(amps), est, limited, until,
                   dict(match)), limited


def _pulses(cell, amps, pulse_s, rest_s, v_min, limit_s, match, label):
    q = cell.nominal_capacity_ah * _window_fraction(cell, v_min, cell.max_v)
    per_pulse_ah = abs(amps) * pulse_s / 3600.0
    n = max(int(math.ceil(q / per_pulse_ah)), 1) if per_pulse_ah > 0 else 1
    est, limited = _cap(n * (pulse_s + rest_s), limit_s)
    if limited:
        n = max(int(est // (pulse_s + rest_s)), 1)
    until = ("time limit %s" % _fmt_s(est) if limited
             else "voltage falls to %.3f V (about %d pulses)" % (v_min, n))
    steps = [(-abs(amps), pulse_s), (0.0, rest_s)] * n
    return Segment(label, "pulses", -abs(amps), est, limited, until,
                   dict(match), steps=steps), limited


def _profile_seg(steps, repeat, limit_s, match, label, until_v=None,
                 cell=None):
    cycle_s = sum(d for _, d in steps)
    if until_v is not None:
        # Looped until a voltage: estimate how many loops the capacity lasts.
        net_ah = -sum(a * d for a, d in steps) / 3600.0
        q = cell.nominal_capacity_ah * _window_fraction(cell, until_v,
                                                        cell.max_v)
        loops = max(int(math.ceil(q / net_ah)), 1) if net_ah > 0 else 1
        est, limited = _cap(loops * cycle_s, limit_s)
        exact = False
        until = ("time limit %s" % _fmt_s(est) if limited else
                 "voltage falls to %.3f V (about %d loops)" % (until_v, loops))
    else:
        loops = max(int(repeat), 1)
        est, limited = _cap(loops * cycle_s, limit_s)
        exact = True
        until = ("time limit %s" % _fmt_s(est) if limited
                 else "%d x %s" % (loops, _fmt_s(cycle_s)))
    peak = max(steps, key=lambda s: abs(s[0]))[0] if steps else 0.0
    return Segment(label, "profile", peak, est, exact, until, dict(match),
                   steps=list(steps) * loops)


# ---------------- planning ----------------

def plan_test(test: TestDef, cell: Cell) -> Plan:
    t = test.type
    raw = test.raw
    segs: list[Segment] = []
    notes: list[str] = []

    if test.initial_rest_s > 0:
        segs.append(Segment(
            "Initial rest + link checks", "rest", 0.0, test.initial_rest_s,
            True, "after %s; includes a short C/10 test pulse"
            % _fmt_s(test.initial_rest_s), {"stage": "initial"}))

    main = {"stage": "main"}
    recovery_s = float(raw.get("voltage_cutoff_rest_s",
                               DEFAULT_RECOVERY_REST_S))

    # The device clamps every setpoint to the cell rating and the tester's
    # own ceiling. Apply the same clamp *before* estimating durations: a
    # 14 A request capped to 5 A takes almost three times as long.
    from .protocol import EBC_MAX_CHARGE_A, EBC_MAX_DISCHARGE_A
    lim_charge = min(cell.max_charge_a, EBC_MAX_CHARGE_A)
    lim_discharge = min(cell.max_discharge_a, EBC_MAX_DISCHARGE_A)
    capped_notes: list[str] = []

    def chg(a: float) -> float:
        a = abs(float(a))
        if a > lim_charge + 1e-9:
            note = ("Charge current %.2f A is capped to %.2f A by the cell "
                    "rating or the tester." % (a, lim_charge))
            if note not in capped_notes:
                capped_notes.append(note)
            return lim_charge
        return a

    def dis(a: float) -> float:
        a = abs(float(a))
        if a > lim_discharge + 1e-9:
            note = ("Discharge current %.2f A is capped to %.2f A by the cell "
                    "rating or the tester." % (a, lim_discharge))
            if note not in capped_notes:
                capped_notes.append(note)
            return lim_discharge
        return a

    def step(a: float) -> float:
        """A signed profile step, clamped by its own direction's limit."""
        return chg(a) if a > 0 else (-dis(a) if a < 0 else 0.0)

    if t == "rest":
        segs.append(_rest("Rest (zero current)", test.max_duration_s or 0,
                          main))

    elif t == "dst":
        peak = raw.get("profile_peak_c_rate")
        if peak is not None:
            steps = [(s.current_a, s.duration_s) for s in profile_from_entries(
                raw["profile"], cell.c_to_amps(float(peak)))]
        else:
            steps = [(test.amps(s["current"], cell), float(s["duration_s"]))
                     for s in raw["profile"]]
        steps = [(step(a), d) for a, d in steps]
        segs.append(_profile_seg(steps, raw.get("repeat", 1),
                                 test.max_duration_s, main, "Current profile"))

    elif t == "charge":
        ccfg = raw.get("charge")
        if isinstance(ccfg, dict):
            i_cc = cell.c_to_amps(float(ccfg["c_rate"]))
            v_max = _quiet(_charge_target_v, ccfg, cell)
            i_cut = (cell.c_to_amps(float(ccfg["cutoff_c"]))
                     if "cutoff_c" in ccfg else cell.charge_taper_a)
            limit = ccfg.get("max_duration_s", test.max_duration_s)
            rest_after = float(ccfg.get("rest_after_s", 0.0))
        else:
            i_cc = test.amps(raw["current"], cell)
            v_max, i_cut = cell.max_v, cell.charge_taper_a
            limit, rest_after = test.max_duration_s, 0.0
        segs += _cccv(cell, chg(i_cc), v_max, i_cut, limit, main)
        if rest_after > 0:
            segs.append(_rest("Rest after charge", rest_after,
                              {"stage": "rest_after_charge"}))

    elif t == "discharge":
        amps = dis(test.amps(raw["current"], cell))
        v_cut = _quiet(_discharge_target_v, raw, "v_cutoff", cell)
        seg, limited = _discharge(
            cell, amps, v_cut, test.max_duration_s or DEFAULT_DISCHARGE_LIMIT_S,
            main, "Discharge %.2f A" % abs(amps))
        segs.append(seg)
        if not limited and recovery_s > 0:
            segs.append(_rest("Recovery rest after cutoff", recovery_s,
                              {"stage": "voltage_cutoff_recovery_rest"}))

    elif t == "hppc":
        amps = dis(test.amps(raw["pulse_current"], cell))
        v_min = _quiet(_discharge_target_v, raw, "v_min", cell)
        seg, limited = _pulses(cell, amps, float(raw["pulse_s"]),
                               float(raw["rest_s"]), v_min,
                               test.max_duration_s, main,
                               "Pulses %.2f A" % abs(amps))
        segs.append(seg)
        if not limited and recovery_s > 0:
            segs.append(_rest("Recovery rest after cutoff", recovery_s,
                              {"stage": "voltage_cutoff_recovery_rest"}))

    elif t == "cycle":
        i_c = chg(test.amps(raw["charge_current"], cell))
        i_d = dis(test.amps(raw["discharge_current"], cell))
        r_c = float(raw.get("rest_after_charge_s", 300))
        r_d = float(raw.get("rest_after_discharge_s", 300))
        for k in range(max(int(raw.get("repeat", 1)), 1)):
            tag = " (cycle %d)" % (k + 1) if int(raw.get("repeat", 1)) > 1 else ""
            m = lambda phase: {"stage": "main", "cycle_index": k,
                               "cycle_phase": phase}
            segs += _cccv(cell, i_c, cell.max_v, cell.charge_taper_a, None,
                          m("charge"), "Charge" + tag)
            segs.append(_rest("Rest" + tag, r_c, m("rest_c")))
            seg, _ = _discharge(cell, i_d, cell.min_v, None, m("discharge"),
                                "Discharge %.2f A%s" % (abs(i_d), tag))
            segs.append(seg)
            segs.append(_rest("Rest" + tag, r_d, m("rest_d")))
        if test.max_duration_s is not None:
            notes.append("max_duration_s (%s) limits the whole cycle test."
                         % _fmt_s(test.max_duration_s))

    elif t == "protocol":
        repeat = max(int(raw.get("repeat", 1)), 1)
        order = raw.get("order", ["discharge", "charge"])
        dcfg, ccfg = raw.get("discharge"), raw.get("charge")
        for k in range(repeat):
            tag = " (cycle %d)" % (k + 1) if repeat > 1 else ""
            for p, phase in enumerate(order):
                m = lambda stage: {"stage": stage, "cycle_index": k,
                                   "phase_index": p}
                if phase == "discharge" and dcfg:
                    raw_amps = cell.c_to_amps(float(dcfg["c_rate"]))
                    v_min = _quiet(_discharge_target_v, dcfg, "v_min", cell)
                    limit = dcfg.get("max_duration_s", test.max_duration_s)
                    proto = dcfg["protocol"]
                    if proto == "crate":
                        amps = dis(raw_amps)
                        seg, _ = _discharge(cell, amps, v_min, limit,
                                            m("discharge"),
                                            "Discharge %.2f A%s"
                                            % (amps, tag))
                    elif proto == "hppc":
                        amps = dis(raw_amps)
                        seg, _ = _pulses(cell, amps, float(dcfg["pulse_s"]),
                                         float(dcfg["pulse_rest_s"]), v_min,
                                         limit, m("discharge"),
                                         "Pulses %.2f A%s" % (amps, tag))
                    elif proto == "dst":
                        # Scaled from the requested peak, as the runner
                        # does; the device then clamps each step on its own.
                        steps = [(step(s.current_a), s.duration_s) for s in (
                            profile_from_entries(dcfg["profile"], raw_amps)
                            if "profile" in dcfg
                            else dst_profile(scale_a=raw_amps))]
                        seg = _profile_seg(steps, None, limit, m("discharge"),
                                           "Profile%s" % tag, until_v=v_min,
                                           cell=cell)
                    else:
                        raise ValueError("unknown discharge protocol: %s"
                                         % proto)
                    segs.append(seg)
                    rest_after = float(dcfg.get("rest_after_s", 0))
                    if rest_after > 0:
                        segs.append(_rest("Rest after discharge" + tag,
                                          rest_after,
                                          m("rest_after_discharge")))
                elif phase == "charge" and ccfg:
                    i_cc = chg(cell.c_to_amps(float(ccfg["c_rate"])))
                    v_max = _quiet(_charge_target_v, ccfg, cell)
                    i_cut = (cell.c_to_amps(float(ccfg["cutoff_c"]))
                             if "cutoff_c" in ccfg else cell.charge_taper_a)
                    limit = ccfg.get("max_duration_s", test.max_duration_s)
                    segs += _cccv(cell, i_cc, v_max, i_cut, limit,
                                  m("charge"), "Charge" + tag)
                    rest_after = float(ccfg.get("rest_after_s", 0))
                    if rest_after > 0:
                        segs.append(_rest("Rest after charge" + tag,
                                          rest_after, m("rest_after_charge")))
    else:
        raise ValueError("unknown test type: %s" % t)

    notes.extend(capped_notes)
    if any(not s.exact for s in segs):
        notes.append("Phases marked as estimated end on a voltage or a "
                     "current, so their length depends on the cell. They "
                     "assume a discharge starts full and a charge starts "
                     "empty.")
    return Plan(test=test.name, cell=cell.id,
                capacity_ah=cell.nominal_capacity_ah, segments=segs,
                total_s=sum(s.duration_s for s in segs),
                exact=all(s.exact for s in segs), notes=notes)


def _quiet(fn, *args):
    """Call a runner target helper without its console warnings."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def preview_points(plan: Plan) -> list:
    """Step-chart points [t, current] for the whole plan, size-capped."""
    pts = []
    t = 0.0
    for seg in plan.segments:
        if seg.steps:
            for a, d in seg.steps:
                pts.append([round(t, 1), round(a, 4)])
                t += d
                pts.append([round(t, 1), round(a, 4)])
        elif seg.cccv == "cv":
            # Draw the taper as an exponential decay from i_cc to i_cut.
            n = 24
            for j in range(n + 1):
                frac = j / n
                i = seg.i_cc * (seg.i_cut / seg.i_cc) ** frac
                pts.append([round(t + frac * seg.duration_s, 1), round(i, 4)])
            t += seg.duration_s
        else:
            pts.append([round(t, 1), round(seg.current_a, 4)])
            t += seg.duration_s
            pts.append([round(t, 1), round(seg.current_a, 4)])
    if len(pts) > MAX_PREVIEW_POINTS:
        step = int(math.ceil(len(pts) / MAX_PREVIEW_POINTS))
        pts = pts[::step] + [pts[-1]]
    return pts


# ---------------- progress ----------------

def _matches(match: dict, state: dict) -> bool:
    return all(state.get(k) == v for k, v in match.items())


def progress(plan: Plan, runner_state: Optional[dict], elapsed_s: float,
             voltage: Optional[float] = None,
             current: Optional[float] = None) -> dict:
    """Where the run is in its plan.

    The phase comes from the runner's own report, so it is exact. Inside a
    phase: exact phases use elapsed time; a CV taper uses the falling
    current on a log scale, which tracks the approach to the cutoff far
    better than time; other estimated phases use time against the estimate
    and are held just short of done until the runner moves on.
    """
    segs = plan.segments
    total = max(plan.total_s, 1e-9)
    if not segs:
        return {"fraction": 0.0, "index": -1, "label": "", "remaining_s": None}

    state = dict(runner_state or {})
    state.update(state.pop("controller_state", {}) or {})
    stage = state.get("stage")

    if stage is None:
        idx, phase_elapsed = 0, elapsed_s
    elif stage == "between_phases":
        idx = next((i for i, s in enumerate(segs)
                    if s.match.get("cycle_index") == state.get("cycle_index")
                    and s.match.get("phase_index") == state.get("phase_index")),
                   len(segs))
        phase_elapsed = 0.0
    else:
        candidates = [i for i, s in enumerate(segs) if _matches(s.match, state)]
        idx = candidates[0] if candidates else None
        phase_elapsed = float(state.get("cycle_phase_elapsed_s",
                                        state.get("phase_elapsed_s", 0.0)))
        if idx is not None and segs[idx].cccv == "cc" and len(candidates) > 1:
            cv = candidates[1]
            if (voltage is not None and segs[cv].v_max is not None
                    and voltage >= segs[cv].v_max - CV_VOLTAGE_MARGIN_V):
                idx = cv
        if idx is None:
            idx = len(segs)

    if idx >= len(segs):
        return {"fraction": 0.995, "index": len(segs) - 1,
                "label": segs[-1].label, "remaining_s": 0.0}

    seg = segs[idx]
    if seg.cccv == "cv" and current is not None and seg.i_cut:
        i = min(max(abs(current), seg.i_cut), seg.i_cc)
        inner = math.log(seg.i_cc / i) / math.log(seg.i_cc / seg.i_cut)
        tau = seg.duration_s / math.log(seg.i_cc / seg.i_cut)
        seg_remaining = tau * math.log(i / seg.i_cut)
    else:
        inner = phase_elapsed / seg.duration_s if seg.duration_s > 0 else 1.0
        seg_remaining = max(seg.duration_s - phase_elapsed, 0.0)
    inner = min(max(inner, 0.0), 1.0 if seg.exact else 0.99)

    done = sum(s.duration_s for s in segs[:idx])
    fraction = min((done + inner * seg.duration_s) / total, 0.995)
    remaining = seg_remaining + sum(s.duration_s for s in segs[idx + 1:])
    return {"fraction": round(fraction, 4), "index": idx, "label": seg.label,
            "remaining_s": round(remaining, 1),
            "estimated": not all(s.exact for s in segs[idx:])}
