#!/usr/bin/env python3
"""
policy.py  --  Week 7, file 2: the controller's Sense -> Decide logic (pure, no I/O).

One state machine shared by the offline replay (replay_policy.py, Week 7 validation) and the
live daemon (controller.py, Week 8), so what is validated offline is exactly what runs live.

Sense (per subflow, from client-side `ss -tni`; never needs receiver-side data):
    rtt      latest smoothed RTT (ms)
    retx     EWMA (ewma_alpha) of the per-sample retransmit rate d(retrans) / d(data_segs_out);
             a burst must persist ~confirm_n samples to matter (single-second spikes are ignored)
    A subflow whose socket stops appearing in `ss` output entirely (interface down, route
    gone) produces no new samples at all -- silence, not a retransmit signal -- and is
    treated as failed once stale_after_s passes with no update (see 'stale' below).
Decide:
    rtt_min  = min RTT over ready, non-failing subflows         (Algorithm 1: RTT_min)
    score_i  = w_rtt * rtt_i / rtt_min  +  w_retx * (100 * retx_i)      (Algorithm 1: score_i)
States per subflow: ACTIVE, BACKUP (MP_PRIO backup), WITHDRAWN (subflow destroyed).
The first subflow passed in (or the explicit `primary=` arg) is the MPC/initial subflow
(kernel subflow id 0): it can be MARK_BACKUP'd but the kernel rejects SUBFLOW_DESTROY on it
(confirmed empirically, EINVAL), so it is never a WITHDRAW candidate.
Actions (at most ONE per step, to avoid simultaneous flips):
    RESTORE      emergency: no healthy ACTIVE subflow left -> promote the healthiest BACKUP
    MARK_BACKUP  ACTIVE and stale (silent, no new data -- see stale_after_s) -> demoted
              immediately, ahead of the score-threshold path
    WITHDRAW  a non-primary subflow failing (retx >= fail_retx, or stale) for
              >= withdraw_after_s while a healthy ACTIVE alternative exists -> SUBFLOW_DESTROY
    MARK_BACKUP  ACTIVE and score > theta for confirm_n consecutive samples (and a healthy
              ACTIVE alternative remains: never demote the last one)
    RESTORE   BACKUP, not currently failing, and score < theta - hyst for confirm_n
              consecutive samples, or a trial restore after trial_after_s regardless of
              failing status (a BACKUP subflow carries no traffic, so its telemetry goes
              stale; the trial re-measures it; 0 disables)
    RECREATE  WITHDRAWN for >= reprobe_s -> SUBFLOW_CREATE again
Anti-flapping: hysteresis band (theta - hyst .. theta), confirm_n consecutive samples,
per-subflow minimum interval between actions, warm-up after (re)creation, single action per step.

The defaults are PLACEHOLDERS: replay_policy.py searches theta / weights against the Week 6
traces and writes the chosen values to a params JSON; load it with Params.from_dict.

Selftest:  python3 policy.py --selftest
"""
import json
import sys
from dataclasses import asdict, dataclass
from typing import Optional

ACTIVE, BACKUP, WITHDRAWN = "ACTIVE", "BACKUP", "WITHDRAWN"
MARK_BACKUP, RESTORE, WITHDRAW, RECREATE = "MARK_BACKUP", "RESTORE", "WITHDRAW", "RECREATE"


@dataclass(frozen=True)
class Params:
    w_rtt: float = 0.2            # weight on rtt_i / rtt_min
    w_retx: float = 1.0           # weight per 1% retransmit rate
    theta: float = 4.0            # demote threshold on score
    hyst: float = 1.5             # restore only below theta - hyst
    confirm_n: int = 3            # consecutive samples beyond a threshold before acting
    min_interval_s: float = 5.0   # minimum time between actions on one subflow
    ewma_alpha: float = 0.5       # smoothing of the per-sample retransmit rate
    min_segs: int = 10            # samples with fewer new data segments carry no rate information
    stale_after_s: float = 3.0    # no NEW sample in this long => treat the subflow as failed
    warmup_s: float = 3.0         # ignore a subflow this long after it (re)appears
    min_active: int = 1           # never leave fewer healthy ACTIVE subflows than this
    fail_retx: float = 0.30       # retransmit rate treated as path failure
    withdraw_after_s: float = 10.0
    reprobe_s: float = 30.0       # WITHDRAWN -> RECREATE after this long
    trial_after_s: float = 60.0   # BACKUP -> trial RESTORE after this long (0 = off)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_json(self):
        return json.dumps(asdict(self), indent=2)


@dataclass
class Sample:
    t: float
    rtt_ms: Optional[float]
    cwnd: Optional[int] = None
    retx: int = 0                 # cumulative retransmitted segments
    segs: Optional[int] = None    # cumulative data segments sent


@dataclass
class Action:
    t: float
    name: str
    kind: str
    score: float
    why: str
    prev: str = ACTIVE            # state before the action (for revert)


class Policy:
    def __init__(self, names, params=None, primary=None):
        self.p = params or Params()
        self.names = list(names)
        n0 = self.names
        self.state = {n: ACTIVE for n in n0}
        self.prev = {n: None for n in n0}
        self.last_progress = {n: None for n in n0}
        self.ewma = {n: 0.0 for n in n0}
        self.primary = primary if primary is not None else (n0[0] if n0 else None)  # subflow id 0: kernel forbids SUBFLOW_DESTROY on it
        self.first_seen = {n: None for n in n0}
        self.last_action = {n: -1e9 for n in n0}
        self.state_since = {n: 0.0 for n in n0}
        self.above = {n: 0 for n in n0}
        self.below = {n: 0 for n in n0}
        self.bad_since = {n: None for n in n0}
        self.withdrawn_at = {n: None for n in n0}
        self.scores, self.rates = {}, {}

    # ---------------------------------------------------------------- sense --
    def _ingest(self, samples):
        p = self.p
        for n, smp in samples.items():
            if smp is None or n not in self.state or self.state[n] == WITHDRAWN:
                continue
            if self.first_seen[n] is None:
                self.first_seen[n] = smp.t
            if self.last_progress[n] is None:
                self.last_progress[n] = smp.t
            pv = self.prev[n]
            if pv is not None and smp.segs is not None and pv.segs is not None:
                d_seg, d_retx = smp.segs - pv.segs, smp.retx - pv.retx
                if d_seg > 0:
                    self.last_progress[n] = smp.t
                inst = None
                if d_seg >= p.min_segs:
                    inst = min(1.0, max(0.0, d_retx / d_seg))
                elif d_retx > 0:
                    # sparse sample (e.g. RTO back-off on a dead path): retransmissions dominate
                    inst = min(1.0, d_retx / max(d_seg, d_retx))
                if inst is not None:
                    self.ewma[n] = p.ewma_alpha * inst + (1 - p.ewma_alpha) * self.ewma[n]
            self.prev[n] = smp

    # --------------------------------------------------------------- decide --
    def step(self, t, samples):
        """samples: {name: Sample|None}. Returns a list with 0 or 1 Action."""
        p = self.p
        self._ingest(samples)
        rtt, rate, ready, stale = {}, {}, {}, {}
        for n in self.names:
            cur = self.prev[n]
            if self.state[n] == WITHDRAWN or cur is None or cur.rtt_ms is None:
                continue
            rtt[n], rate[n] = cur.rtt_ms, self.ewma[n]
            ready[n] = (cur.t - self.first_seen[n]) >= p.warmup_s
            stale[n] = (t - (self.last_progress[n] or cur.t)) >= p.stale_after_s
        failing = {n: rate[n] >= p.fail_retx or stale[n] for n in rate}
        pool = [n for n in rtt if ready[n] and not failing[n]] or [n for n in rtt if ready[n]]
        score = {}
        if pool:
            rmin = max(0.1, min(rtt[n] for n in pool))
            score = {n: p.w_rtt * rtt[n] / rmin + p.w_retx * rate[n] * 100 for n in rtt if ready[n]}
        self.scores, self.rates = score, rate

        for n in score:
            if score[n] > p.theta:
                self.above[n] += 1
                self.below[n] = 0
            elif score[n] < p.theta - p.hyst:
                self.below[n] += 1
                self.above[n] = 0
            else:
                self.above[n] = self.below[n] = 0
            if failing[n]:
                self.bad_since[n] = t if self.bad_since[n] is None else self.bad_since[n]
            else:
                self.bad_since[n] = None

        can = lambda n, gap=None: t - self.last_action[n] >= (p.min_interval_s if gap is None else gap)
        healthy_active = lambda excl: [m for m in score if m != excl and self.state[m] == ACTIVE and not failing[m]]

        # 1. emergency: nothing healthy is carrying traffic -> promote the healthiest BACKUP
        if score and not healthy_active(None):
            for n in sorted(score, key=score.get):
                if self.state[n] == BACKUP and not failing[n] and can(n, min(1.0, p.min_interval_s)):
                    return self._do(t, n, RESTORE, score[n], "no healthy active subflow")
        # 2. immediately demote an ACTIVE subflow that has gone stale (silence, not a
        #    retransmit signal -- see stale[] above), ahead of the confirm_n/theta path so a
        #    dead link is deprioritised as soon as stale_after_s elapses, not after several
        #    more score-threshold samples on top of it
        for n in sorted(score, key=score.get, reverse=True):
            if (self.state[n] == ACTIVE and stale.get(n, False) and can(n)
                    and len(healthy_active(n)) >= p.min_active):
                return self._do(t, n, MARK_BACKUP, score[n], "stale path")
        # 3. withdraw a persistently failing subflow (only with a healthy alternative).
        #    NEVER the primary (subflow id 0): the kernel rejects SUBFLOW_DESTROY on it
        #    (confirmed empirically -- EINVAL every time); mark-backup is as far as it goes.
        for n in sorted(score, key=score.get, reverse=True):
            if (n != self.primary and self.state[n] in (ACTIVE, BACKUP) and self.bad_since[n] is not None
                    and t - self.bad_since[n] >= p.withdraw_after_s
                    and len(healthy_active(n)) >= p.min_active and can(n)):
                return self._do(t, n, WITHDRAW, score[n], f"failing for {t - self.bad_since[n]:.0f}s")
        # 4. demote the worst ACTIVE subflow if a healthy ACTIVE alternative remains
        for n in sorted(score, key=score.get, reverse=True):
            if (self.state[n] == ACTIVE and self.above[n] >= p.confirm_n and can(n)
                    and len(healthy_active(n)) >= p.min_active):
                return self._do(t, n, MARK_BACKUP, score[n], f"score {score[n]:.2f} > {p.theta}")
        # 5. restore a recovered (or trial) BACKUP subflow. A subflow still flagged failing
        #    (stale/high-retx) is never restored on the ordinary score threshold -- a frozen,
        #    stale sample can compute a deceptively low score -- only an explicit trial probe
        #    (item 5b) may re-measure it.
        for n in self.names:
            if self.state[n] != BACKUP or n not in score or not can(n):
                continue
            if failing.get(n, False):
                if p.trial_after_s and t - self.state_since[n] >= p.trial_after_s:
                    return self._do(t, n, RESTORE, score[n], "trial re-measure")
                continue
            if self.below[n] >= p.confirm_n:
                return self._do(t, n, RESTORE, score[n], f"score {score[n]:.2f} < {p.theta - p.hyst}")
            if p.trial_after_s and t - self.state_since[n] >= p.trial_after_s:
                return self._do(t, n, RESTORE, score[n], "trial re-measure")
        # 6. re-create a withdrawn subflow
        for n in self.names:
            if (self.state[n] == WITHDRAWN and self.withdrawn_at[n] is not None
                    and t - self.withdrawn_at[n] >= p.reprobe_s):
                return self._do(t, n, RECREATE, 0.0, "re-probe after withdraw")
        return []

    def _do(self, t, n, kind, score, why):
        a = Action(t, n, kind, round(score, 3), why, prev=self.state[n])
        self.state[n] = {MARK_BACKUP: BACKUP, RESTORE: ACTIVE, WITHDRAW: WITHDRAWN, RECREATE: ACTIVE}[kind]
        self.state_since[n] = t
        self.last_action[n] = t
        self.above[n] = self.below[n] = 0
        self.bad_since[n] = None
        if kind == WITHDRAW:
            self.withdrawn_at[n] = t
            self._reset(n)
        elif kind == RECREATE:
            self.withdrawn_at[n] = None
            self._reset(n)
        return [a]

    def _reset(self, n):
        self.prev[n], self.ewma[n], self.first_seen[n] = None, 0.0, None

    def revert(self, action):
        """Call when executing `action` failed (e.g. netlink error): restore the prior state."""
        self.state[action.name] = action.prev
        self.last_action[action.name] = action.t


def count_flaps(actions, window_s):
    """Count reversals (MARK_BACKUP <-> RESTORE) on one subflow closer together than window_s.
    A scheduled trial re-measure legitimately reverses after warm-up + confirm, i.e. > min_interval."""
    flaps, last = 0, {}
    for a in actions:
        if a.kind in (MARK_BACKUP, RESTORE):
            prev = last.get(a.name)
            if prev and prev.kind != a.kind and a.t - prev.t < window_s:
                flaps += 1
            last[a.name] = a
    return flaps


# ------------------------------------------------------------------ selftest --
class _Gen:
    """Cumulative-counter sample generator from rtt(t) and retx_rate(t) functions."""
    def __init__(self, rtt_fn, retx_fn, seg_rate=500):
        self.rtt_fn, self.retx_fn, self.seg_rate = rtt_fn, retx_fn, seg_rate
        self.segs = self.retx = 0.0
        self.t_prev = None

    def __call__(self, t):
        dt = 1.0 if self.t_prev is None else t - self.t_prev
        self.t_prev = t
        sr = self.seg_rate(t) if callable(self.seg_rate) else self.seg_rate
        self.segs += sr * dt
        self.retx += sr * dt * self.retx_fn(t)
        return Sample(t, self.rtt_fn(t), 10, int(self.retx), int(self.segs))


def _run(policy, t_end, gens, dt=1.0):
    acts, t = [], 0.0
    while t <= t_end:
        acts += policy.step(t, {n: g(t) for n, g in gens.items()})
        t += dt
    return acts


def _kinds(acts):
    return [(a.name, a.kind) for a in acts]


def selftest():
    p = Params()
    c = lambda v: (lambda t: v)

    # T1: baseline-symmetric-like (RTT x5 but low loss) -> no action for 120 s
    acts = _run(Policy(["wifi", "cell"], p), 120, {"wifi": _Gen(c(10), c(0.02)), "cell": _Gen(c(50), c(0.001))})
    assert acts == [], _kinds(acts)

    # T2: degraded cell (RTT x18, 6% retx) -> exactly one MARK_BACKUP of cell, early
    pol = Policy(["wifi", "cell"], p)
    acts = _run(pol, 25, {"wifi": _Gen(c(10), c(0.02)), "cell": _Gen(c(180), c(0.06))})
    assert _kinds(acts) == [("cell", MARK_BACKUP)], _kinds(acts)
    assert acts[0].t <= 10, acts[0]

    # T3: cell stays bad for 100 s -> demote, then only bounded trial cycles (>= trial_after_s apart)
    acts = _run(Policy(["wifi", "cell"], p), 100, {"wifi": _Gen(c(10), c(0.02)), "cell": _Gen(c(180), c(0.06))})
    kinds = [a.kind for a in acts]
    assert kinds[0] == MARK_BACKUP and count_flaps(acts, p.min_interval_s) == 0, _kinds(acts)
    assert all(b.t - a.t >= 5 for a, b in zip(acts, acts[1:])), [a.t for a in acts]
    assert len(acts) <= 1 + 2 * (100 // int(p.trial_after_s)) + 1, len(acts)

    # T4a: 1 s retransmit spikes (10x baseline) are smoothed/debounced -> no action
    spike = lambda t: 0.20 if int(t) % 10 == 0 else 0.001
    acts = _run(Policy(["wifi", "cell"], p), 80, {"wifi": _Gen(c(10), c(0.0)), "cell": _Gen(c(10), spike)})
    assert acts == [], _kinds(acts)
    # T4b: hysteresis: demoted subflow whose score sits inside the band (theta-hyst .. theta) stays demoted
    pol = Policy(["wifi", "cell"], p)
    pol.state["cell"] = BACKUP
    acts = _run(pol, 50, {"wifi": _Gen(c(10), c(0.0)), "cell": _Gen(c(10), c(0.03))})   # score 0.2 + 3.0 = 3.2
    assert acts == [], _kinds(acts)

    # T5: Wi-Fi (the primary/id-0 subflow) dies at t=20 (retx -> 90%, RTT frozen), cell
    # healthy ACTIVE. Primary is protected from WITHDRAW (kernel rejects SUBFLOW_DESTROY on
    # subflow id 0), so the ceiling here is MARK_BACKUP -- never a WITHDRAW/RECREATE cycle.
    acts = _run(Policy(["wifi", "cell"], p), 70,
                {"wifi": _Gen(c(10), lambda t: 0.9 if t >= 20 else 0.01), "cell": _Gen(c(50), c(0.001))})
    assert _kinds(acts) == [("wifi", MARK_BACKUP)], _kinds(acts)
    assert acts[0].t <= 26, acts[0]

    # T5e: the SAME failure on a non-primary subflow ("cell") gets the full cycle -- primary
    # protection must not block withdrawal of an ordinary secondary subflow.
    acts = _run(Policy(["wifi", "cell"], p), 70,
                {"wifi": _Gen(c(10), c(0.01)), "cell": _Gen(c(50), lambda t: 0.9 if t >= 20 else 0.001)})
    assert _kinds(acts)[:3] == [("cell", MARK_BACKUP), ("cell", WITHDRAW), ("cell", RECREATE)], _kinds(acts)
    assert all(a.name == "cell" for a in acts), "healthy wifi must never be touched"

    # T5b: cell already demoted, THEN Wi-Fi dies -> cell restored within a few seconds
    pol = Policy(["wifi", "cell"], p)
    pol.state["cell"] = BACKUP
    acts = _run(pol, 40, {"wifi": _Gen(c(10), lambda t: 0.9 if t >= 20 else 0.01), "cell": _Gen(c(50), c(0.001))})
    assert acts and acts[0].name == "cell" and acts[0].kind == RESTORE and acts[0].t <= 24, [(a.name, a.kind, a.t) for a in acts]

    # T5c: dead path with RTO back-off: only ~2 segments/s, all of them retransmissions
    acts = _run(Policy(["wifi", "cell"], p), 40,
                {"wifi": _Gen(c(10), lambda t: 1.0 if t >= 20 else 0.01, lambda t: 2 if t >= 20 else 500),
                 "cell": _Gen(c(50), c(0.001))})
    assert acts and acts[0].name == "wifi" and acts[0].kind == MARK_BACKUP and acts[0].t <= 27, [(a.name, a.kind, a.t) for a in acts]

    # T5d: interface fully vanishes from `ss` (real ip-link-down behaviour) -- no retransmit
    # signal at all, just silence. Wi-Fi stops reporting samples entirely at t=20.
    pol = Policy(["wifi", "cell"], p)
    t, acts = 0.0, []
    wg, cg = _Gen(c(10), c(0.01)), _Gen(c(50), c(0.001))
    while t <= 40:
        samples = {"wifi": (wg(t) if t < 20 else None), "cell": cg(t)}
        acts += pol.step(t, samples)
        t += 1.0
    assert acts and acts[0].name == "wifi" and acts[0].t >= 20, [(a.name, a.kind, a.t) for a in acts]
    assert all(a.name == "wifi" for a in acts), "healthy cell must never be touched"

    # T6: a lone subflow is never demoted
    acts = _run(Policy(["wifi"], p), 60, {"wifi": _Gen(c(10), c(0.5))})
    assert acts == [], _kinds(acts)

    # T7: revert restores the previous state
    pol = Policy(["wifi", "cell"], p)
    acts = _run(pol, 12, {"wifi": _Gen(c(10), c(0.02)), "cell": _Gen(c(180), c(0.06))})
    assert pol.state["cell"] == BACKUP
    pol.revert(acts[0])
    assert pol.state["cell"] == ACTIVE

    print("policy selftest: PASS (no false action, single demote, bounded trials, "
          "debounce, hysteresis, Wi-Fi failure, emergency restore, last-subflow guard, revert)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(Params().to_json())
