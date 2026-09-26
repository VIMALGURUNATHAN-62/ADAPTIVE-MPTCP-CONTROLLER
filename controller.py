#!/usr/bin/env python3
"""
controller.py  --  Week 8, file 1: the live Sense -> Decide -> Act daemon.

Runs INSIDE the client namespace on the userspace path manager and drives MPTCP purely
through the generic-netlink PM API (mptcp_pm_nl.py). The kernel packet scheduler is never
touched: only which subflows are eligible (MP_PRIO backup / destroy / create) is controlled.

  Sense   every --interval s: `ss -tni` -> per-subflow srtt, cwnd, cumulative retrans/segments
  Decide  policy.Policy (validated offline by replay_policy.py, same code)
  Act     MARK_BACKUP / RESTORE -> SET_FLAGS (MP_PRIO), WITHDRAW -> SUBFLOW_DESTROY,
          RECREATE -> SUBFLOW_CREATE. A failed netlink call reverts the policy state.

Path manager duty: with pm_type=1 the kernel creates no subflows itself, so on every new
client-side MPTCP connection the daemon creates the missing subflows (paths below), using the
server's ADD_ADDR (ANNOUNCED event) when it arrives.
Multiple connections (e.g. iperf3 control + data) are tracked independently by token.

Every decision is written to a JSONL log (--log): connection lifecycle, a per-tick record
(rtt, retransmit rate, score, state per path) and each action with its result. This is the
audit trail for Weeks 9-10.

Usage (root, inside the client namespace, BEFORE the traffic starts):
  ip netns exec client python3 controller.py --params controller_params.json --log ctl.jsonl
  ip netns exec client python3 controller.py --dry-run          # observe only, no PM commands
  python3 controller.py --selftest                              # offline, no kernel needed
Options: --path NAME=LOCAL_IP:REMOTE_IP (repeatable; default wifi=10.10.1.1:10.10.1.2
         cell=10.10.2.1:10.10.2.2), --interval 1.0, --duration S, --no-create, --ns NAME
"""
import argparse
import json
import os
import re
import select
import signal
import subprocess
import sys
import time

import mptcp_pm_nl as nl
from policy import MARK_BACKUP, RECREATE, RESTORE, WITHDRAW, Params, Policy, Sample

RTT = re.compile(r"\brtt:([\d.]+)/")
RETRANS = re.compile(r"\bretrans:\d+/(\d+)")
SEGS = re.compile(r"\bdata_segs_out:(\d+)")
SEGS_ALL = re.compile(r"\bsegs_out:(\d+)")
CWND = re.compile(r"\bcwnd:(\d+)")


# ------------------------------------------------------------------ sense ---
def parse_ss(text):
    """`ss -tni` text -> list of dicts for ESTAB MPTCP subflow sockets."""
    blocks, cur = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace() and not line.startswith("State"):
            cur = [line]
            blocks.append(cur)
        elif cur is not None:
            cur.append(line.strip())
    out = []
    for b in blocks:
        head, body = b[0].split(), " ".join(b)
        if len(head) < 5 or head[0] != "ESTAB" or "tcp-ulp-mptcp" not in body:
            continue
        try:
            lip, lport = head[3].rsplit(":", 1)
            rip, rport = head[4].rsplit(":", 1)
            rt, rx = RTT.search(body), RETRANS.search(body)
            sg, cw = SEGS.search(body) or SEGS_ALL.search(body), CWND.search(body)
            out.append({"lip": lip, "lport": int(lport), "rip": rip, "rport": int(rport),
                        "rtt": float(rt.group(1)) if rt else None,
                        "cwnd": int(cw.group(1)) if cw else None,
                        "retx": int(rx.group(1)) if rx else 0,
                        "segs": int(sg.group(1)) if sg else None})
        except ValueError:
            continue
    return out


def run_ss(ns=None):
    cmd = ["ss", "-tni"]
    if ns:
        cmd = ["ip", "netns", "exec", ns] + cmd
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ------------------------------------------------------------------- state ---
class Conn:
    def __init__(self, token, t0, params, names):
        self.token, self.t0 = token, t0
        self.policy = Policy(names, params)
        self.eps = {}            # name -> {lip, lport, rip, rport} (latest known)
        self.created = set()
        self.create_at = None
        self.rem_ids = {}        # name -> announced remote address id
        self.dport = None
        self.actions = 0


class Controller:
    def __init__(self, pm, params, paths, log, dry_run=False, create=True, create_wait=1.0):
        self.pm, self.params, self.paths = pm, params, paths
        self.log_fn, self.dry, self.create, self.create_wait = log, dry_run, create, create_wait
        self.name_of_lip = {p["lip"]: n for n, p in paths.items()}
        self.conns, self.by_local = {}, {}

    def log(self, rec):
        self.log_fn(dict(rec, ts=round(time.time(), 3)))

    # ---- events ---------------------------------------------------------------
    def _bind(self, c, name, lip, lport, rip, rport):
        c.eps[name] = {"lip": lip, "lport": lport, "rip": rip, "rport": rport}
        self.by_local[(lip, lport)] = (c.token, name)

    def _unbind(self, c, lip, lport):
        self.by_local.pop((lip, lport), None)
        for n, ep in list(c.eps.items()):
            if (ep["lip"], ep["lport"]) == (lip, lport):
                del c.eps[n]

    def on_event(self, ev, now):
        kind, tok = ev["event"], ev.get("token")
        if kind == "ESTABLISHED" and ev.get("server_side", 0) != 1:
            name = self.name_of_lip.get(ev.get("saddr4"))
            if name is None:
                self.log({"ev": "ignored_connection", "token": tok, "saddr": ev.get("saddr4")})
                return
            c = Conn(tok, now, self.params, list(self.paths))
            c.dport = ev["dport"]
            self._bind(c, name, ev["saddr4"], ev["sport"], ev["daddr4"], ev["dport"])
            c.create_at = now + self.create_wait if self.create else None
            self.conns[tok] = c
            self.log({"ev": "connection", "token": tok, "initial_path": name,
                      "lport": ev["sport"], "dport": ev["dport"]})
            return
        c = self.conns.get(tok)
        if c is None:
            return
        if kind == "ANNOUNCED":
            for name, pth in self.paths.items():
                if pth["rip"] == ev.get("daddr4") and ev.get("rem_id") is not None:
                    c.rem_ids[name] = ev["rem_id"]
                    if self.create and name not in c.eps:
                        c.create_at = now
            self.log({"ev": "announced", "token": tok, "addr": ev.get("daddr4"), "rem_id": ev.get("rem_id")})
        elif kind == "SF_ESTABLISHED":
            name = self.name_of_lip.get(ev.get("saddr4"))
            if name:
                self._bind(c, name, ev["saddr4"], ev["sport"], ev["daddr4"], ev["dport"])
                self.log({"ev": "subflow_up", "token": tok, "path": name, "lport": ev["sport"]})
        elif kind == "SF_CLOSED":
            self._unbind(c, ev.get("saddr4"), ev.get("sport"))
            self.log({"ev": "subflow_closed", "token": tok, "saddr": ev.get("saddr4"), "error": ev.get("error")})
        elif kind == "SF_PRIORITY":
            self.log({"ev": "mp_prio_event", "token": tok, "saddr": ev.get("saddr4"), "backup": ev.get("backup")})
        elif kind == "CLOSED":
            for (lip, lport), (t, _) in list(self.by_local.items()):
                if t == tok:
                    del self.by_local[(lip, lport)]
            del self.conns[tok]
            self.log({"ev": "connection_closed", "token": tok, "actions": c.actions})

    # ---- act ------------------------------------------------------------------
    def due(self, now):
        """Delayed subflow creation (waits briefly for the server's ADD_ADDR)."""
        for c in list(self.conns.values()):
            if c.create_at is not None and now >= c.create_at:
                c.create_at = None
                for name, pth in self.paths.items():
                    if name in c.eps or name in c.created:
                        continue
                    c.created.add(name)
                    self._call(c, name, "create", lambda: self.pm.subflow_create(
                        c.token, {"ip": pth["lip"], "id": pth["lid"]},
                        {"ip": pth["rip"], "id": c.rem_ids.get(name, pth["rid"]), "port": c.dport}))

    def _call(self, c, name, what, fn):
        if self.dry:
            self.log({"ev": "pm_call", "token": c.token, "path": name, "what": what, "result": "dry-run"})
            return True
        try:
            fn()
            self.log({"ev": "pm_call", "token": c.token, "path": name, "what": what, "result": "ok"})
            return True
        except nl.PMError as e:
            self.log({"ev": "pm_call", "token": c.token, "path": name, "what": what, "result": f"error: {e}"})
            return False

    def _execute(self, c, a):
        pth, ep = self.paths[a.name], c.eps.get(a.name)
        rid = c.rem_ids.get(a.name, pth["rid"])
        if a.kind in (MARK_BACKUP, RESTORE):
            if ep is None:
                ok = self._call(c, a.name, a.kind, lambda: (_ for _ in ()).throw(nl.PMError("subflow endpoint unknown")))
            else:
                ok = self._call(c, a.name, a.kind, lambda: self.pm.set_backup(
                    c.token, {"ip": ep["lip"], "port": ep["lport"]},
                    {"ip": ep["rip"], "port": ep["rport"]}, backup=(a.kind == MARK_BACKUP)))
        elif a.kind == WITHDRAW:
            if ep is None:
                ok = self._call(c, a.name, a.kind,
                                lambda: (_ for _ in ()).throw(
                                    nl.PMError("subflow endpoint unknown")))
            else:
                ok = self._call(c, a.name, a.kind, lambda: self.pm.subflow_destroy(
                    c.token,
                    {"ip": ep["lip"], "port": ep["lport"]},
                    {"ip": ep["rip"], "port": ep["rport"]}))
        else:  # RECREATE
            ok = self._call(c, a.name, a.kind, lambda: self.pm.subflow_create(
                c.token, {"ip": pth["lip"], "id": pth["lid"]}, {"ip": pth["rip"], "id": rid, "port": c.dport}))
        if not ok and not self.dry:
            c.policy.revert(a)
        c.actions += bool(ok)
        self.log({"ev": "action", "token": c.token, "path": a.name, "kind": a.kind, "score": a.score,
                  "why": a.why, "t": round(a.t, 1), "executed": bool(ok) and not self.dry, "dry_run": self.dry})

    # ---- sense + decide ---------------------------------------------------------
    def tick(self, now, ss_text):
        per_conn = {}
        for s in parse_ss(ss_text):
            hit = self.by_local.get((s["lip"], s["lport"]))
            c = self.conns.get(hit[0]) if hit else None
            if c is None:
                continue
            name = hit[1]
            c.eps[name] = {"lip": s["lip"], "lport": s["lport"], "rip": s["rip"], "rport": s["rport"]}
            per_conn.setdefault(c.token, {})[name] = Sample(now - c.t0, s["rtt"], s["cwnd"], s["retx"], s["segs"])
        for tok, c in list(self.conns.items()):
            samples = {n: per_conn.get(tok, {}).get(n) for n in self.paths}
            # Always step, even when every current sample is missing: policy.py's staleness
            # detection depends on being called to notice "no new sample," and a socket for a
            # downed interface keeps reappearing in `ss` with frozen counters rather than
            # vanishing outright, so this can't be short-circuited on "all None" either.
            acts = c.policy.step(now - c.t0, samples)
            pol = c.policy
            self.log({"ev": "tick", "token": tok, "t": round(now - c.t0, 1),
                      "rtt": {n: s.rtt_ms for n, s in samples.items() if s},
                      "retx_ewma": {n: round(pol.ewma[n], 4) for n in self.paths},
                      "score": {n: round(v, 2) for n, v in pol.scores.items()},
                      "state": dict(pol.state)})
            for a in acts:
                self._execute(c, a)


# --------------------------------------------------------------------- CLI ---
def parse_paths(specs):
    if not specs:
        specs = ["wifi=10.10.1.1:10.10.1.2", "cell=10.10.2.1:10.10.2.2"]
    paths = {}
    for i, spec in enumerate(specs):
        name, addrs = spec.split("=", 1)
        lip, rip = addrs.split(":", 1)
        paths[name] = {"lip": lip, "rip": rip, "lid": i, "rid": i}   # first path = initial subflow (id 0)
    return paths


def read_events(pm, sock, timeout):
    r, _, _ = select.select([sock], [], [], max(0.0, timeout))
    evs = []
    if r:
        for typ, _, _, payload in nl.parse_nlmsgs(sock.recv(65536)):
            ev = nl.decode_event(pm.fid, typ, payload)
            if ev:
                evs.append(ev)
    return evs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--params", help="controller_params.json from replay_policy.py")
    ap.add_argument("--log", default="controller_decisions.jsonl")
    ap.add_argument("--path", action="append", help="NAME=LOCAL_IP:REMOTE_IP (repeatable)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, help="exit after this many seconds")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-create", action="store_true", help="do not create missing subflows")
    ap.add_argument("--ns", help="run ss inside this namespace instead of the current one")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    params = Params.from_dict(json.load(open(a.params))) if a.params else Params()
    paths = parse_paths(a.path)
    logf = open(a.log, "a", buffering=1)

    def log(rec):
        logf.write(json.dumps(rec) + "\n")

    mode = nl.pm_mode()
    if mode not in ("1", "userspace") and not a.dry_run:
        try:
            open("/proc/sys/net/mptcp/pm_type", "w").write("1")
            mode = nl.pm_mode()
        except OSError:
            pass
        if mode not in ("1", "userspace"):
            print("userspace PM not enabled in this namespace (net.mptcp.pm_type=1) and could not be set")
            return 1
        print("net.mptcp.pm_type set to 1 (applies to connections created from now on)")
    try:
        pm = nl.PM()
        sock = pm.open_events()
    except (nl.PMError, OSError) as e:
        print(f"cannot open MPTCP PM netlink: {e}")
        return 1

    ctl = Controller(pm, params, paths, log, dry_run=a.dry_run, create=not a.no_create)
    ctl.log({"ev": "start", "params": json.loads(params.to_json()), "paths": paths,
             "dry_run": a.dry_run, "pm_mode": mode})
    print(f"controller running (dry_run={a.dry_run}); log -> {a.log}; Ctrl-C to stop", flush=True)

    stop = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.append(1))
    t_start = time.monotonic()
    next_tick = t_start
    while not stop:
        now = time.monotonic()
        if a.duration and now - t_start >= a.duration:
            break
        for ev in read_events(pm, sock, min(0.2, next_tick - now)):
            ctl.on_event(ev, time.monotonic())
        now = time.monotonic()
        ctl.due(now)
        if now >= next_tick:
            next_tick += a.interval
            if ctl.conns:
                ctl.tick(now, run_ss(a.ns))
    ctl.log({"ev": "stop", "connections": len(ctl.conns), "actions": sum(c.actions for c in ctl.conns.values())})
    print("controller stopped")
    return 0


# ---------------------------------------------------------------- selftest ---
class FakePM:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or set()

    def _rec(self, kind, *args):
        if kind in self.fail:
            self.calls.append((kind + "-FAILED",) + args)
            raise nl.PMError("EINVAL: simulated")
        self.calls.append((kind,) + args)

    def subflow_create(self, tok, local, remote):
        self._rec("create", tok, local, remote)

    def subflow_destroy(self, tok, local, remote):
        self._rec("destroy", tok, local, remote)

    def set_backup(self, tok, local, remote, backup=True):
        self._rec("backup" if backup else "restore", tok, local, remote)


def _ss(t, wifi, cell):
    """Fake `ss -tni` output. wifi/cell = (rtt, retx_rate, seg_rate) accumulating over t."""
    def sock(lip, lport, rip, rport, rtt, segs, retx):
        return (f"ESTAB 0 0 {lip}:{lport} {rip}:{rport}\n\t cubic rtt:{rtt}/3.0 cwnd:10 data_segs_out:{int(segs)} "
                f"segs_out:{int(segs)} retrans:0/{int(retx)} tcp-ulp-mptcp flags:Jec token:1a2b(id:0)/cd34(id:0)\n")
    return ("State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            + sock("10.10.1.1", 40000, "10.10.1.2", 5201, wifi[0], wifi[2] * t, wifi[2] * t * wifi[1])
            + sock("10.10.2.1", 41000, "10.10.2.2", 5201, cell[0], cell[2] * t, cell[2] * t * cell[1])
            + "LISTEN 0 0 0.0.0.0:5201 0.0.0.0:*\n")


def _setup(pm, dry=False):
    recs = []
    ctl = Controller(pm, Params(), parse_paths(None), recs.append, dry_run=dry, create_wait=1.0)
    ctl.on_event({"event": "ESTABLISHED", "token": 7, "saddr4": "10.10.1.1", "sport": 40000,
                  "daddr4": "10.10.1.2", "dport": 5201}, 0.0)
    ctl.on_event({"event": "ANNOUNCED", "token": 7, "daddr4": "10.10.2.2", "rem_id": 1, "dport": 0}, 0.1)
    ctl.due(0.2)
    ctl.on_event({"event": "SF_ESTABLISHED", "token": 7, "saddr4": "10.10.2.1", "sport": 41000,
                  "daddr4": "10.10.2.2", "dport": 5201}, 0.3)
    return ctl, recs


def selftest():
    # ss parser
    socks = parse_ss(_ss(1, (12.5, 0.02, 500), (180.0, 0.06, 300)))
    assert len(socks) == 2 and socks[1]["lip"] == "10.10.2.1" and socks[1]["rtt"] == 180.0
    assert socks[1]["retx"] == 18 and socks[0]["segs"] == 500, socks

    # 1. path-manager duty + degraded cell -> one MARK_BACKUP through SET_FLAGS
    pm = FakePM()
    ctl, recs = _setup(pm)
    assert pm.calls[0] == ("create", 7, {"ip": "10.10.2.1", "id": 1},
                           {"ip": "10.10.2.2", "id": 1, "port": 5201}), pm.calls
    for t in range(0, 16):
        ctl.tick(float(t), _ss(t, (10.0, 0.02, 500), (180.0, 0.06, 300)))
    backups = [c for c in pm.calls if c[0] == "backup"]
    assert len(backups) == 1 and backups[0][2] == {"ip": "10.10.2.1", "port": 41000} \
        and backups[0][3] == {"ip": "10.10.2.2", "port": 5201} and backups[0][1] == 7, pm.calls
    acts = [r for r in recs if r["ev"] == "action"]
    assert len(acts) == 1 and acts[0]["kind"] == MARK_BACKUP and acts[0]["executed"] and acts[0]["t"] <= 10
    assert sum(r["ev"] == "tick" for r in recs) == 16

    # 2. healthy symmetric paths -> no PM action beyond the initial create
    pm = FakePM()
    ctl, recs = _setup(pm)
    for t in range(0, 40):
        ctl.tick(float(t), _ss(t, (10.0, 0.02, 500), (50.0, 0.001, 300)))
    assert [c[0] for c in pm.calls] == ["create"], pm.calls

    # 3. netlink failure -> policy reverted, retried after min_interval, never silently lost
    pm = FakePM(fail={"backup"})
    ctl, recs = _setup(pm)
    for t in range(0, 25):
        ctl.tick(float(t), _ss(t, (10.0, 0.02, 500), (180.0, 0.06, 300)))
    failed = [c for c in pm.calls if c[0] == "backup-FAILED"]
    assert len(failed) >= 2, pm.calls
    assert all(r["executed"] is False for r in recs if r["ev"] == "action")
    assert ctl.conns[7].policy.state["cell"] == "ACTIVE"

    # 4. dry-run: decisions logged, nothing sent
    pm = FakePM()
    ctl, recs = _setup(pm, dry=True)
    for t in range(0, 16):
        ctl.tick(float(t), _ss(t, (10.0, 0.02, 500), (180.0, 0.06, 300)))
    assert pm.calls == [] and any(r["ev"] == "action" and r["dry_run"] for r in recs)

    # 5. connection close drops state
    ctl.on_event({"event": "CLOSED", "token": 7}, 20.0)
    assert not ctl.conns and not ctl.by_local

    print("controller selftest: PASS (ss parsing, subflow creation, demotion via SET_FLAGS with the "
          "right endpoints, no action when healthy, netlink-failure revert+retry, dry-run, close)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
