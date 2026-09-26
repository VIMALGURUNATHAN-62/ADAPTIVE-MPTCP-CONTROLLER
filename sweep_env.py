#!/usr/bin/env python3
"""
sweep_env.py  --  Week 5, file 2 of the sweep harness.

Testbed control for the sweep. Wraps the EXISTING Week 3/4 scripts
(testbed_bringup.sh / testbed_teardown.sh) and applies, verifies and reads back
per-cell state defined in sweep_config.py:
  - netem shaping on all four veth ends (per-rep netem seed when supported)
  - receiver-side receive buffer (tcp_rmem pin + tcp_moderate_rcvbuf off)
  - iperf3 command builders (MPTCP arm via mptcpize, single-path arm plain)
  - hygiene between runs (kill stray iperf3, flush tcp_metrics cache)

Must run as root (network namespaces). Keep this file in the same directory as
testbed_bringup.sh, testbed_teardown.sh and sweep_config.py.

Intended call order in run_sweep.py (next file):
  bringup()                                  once per sweep
  for cell in run order, for rep in 1..REPS:
      kill_iperf3(); flush_tcp_metrics()
      apply_shaping(cell, rep); shaping = verify_shaping(cell)
      buf = set_receive_buffer(cell)
      start_server(cell, mptcp=...); client_cmd(mptcp=...) ...
  teardown()

Self-test (root):  sudo python3 sweep_env.py --selftest
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import sweep_config as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT_NS = "client"
SERVER_NS = "server"
SERVER_WIFI_IP = "10.10.1.2"   # both arms connect to the Wi-Fi address (as in Week 4)

# (namespace, interface, leg) -- same four ends netem_shaping.sh shapes
IFACES = (
    (CLIENT_NS, "c-wifi", "wifi"),
    (SERVER_NS, "s-wifi", "wifi"),
    (CLIENT_NS, "c-cell", "cell"),
    (SERVER_NS, "s-cell", "cell"),
)

_state = {}   # per-bring-up cache: original rmem / moderate_rcvbuf, netem seed support


class EnvError(RuntimeError):
    pass


# ------------------------------------------------------------ primitives ---
def require_root():
    if os.geteuid() != 0:
        raise EnvError("must run as root (sudo): network namespaces need it")


def sh(cmd, ns=None, check=True, timeout=60):
    """Run a command, optionally inside a namespace. Returns CompletedProcess."""
    full = ["ip", "netns", "exec", ns, *cmd] if ns else list(cmd)
    p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise EnvError(f"command failed ({p.returncode}): {' '.join(full)}\n{p.stderr.strip()}")
    return p


def ns_capture(ns, cmd, timeout=30):
    """stdout of a command run inside a namespace (e.g. ss -Mai)."""
    return sh(cmd, ns=ns, timeout=timeout).stdout


# -------------------------------------------------------------- lifecycle --
def teardown():
    subprocess.run(["bash", str(SCRIPT_DIR / "testbed_teardown.sh")],
                   capture_output=True, text=True, timeout=60)
    _state.clear()


def bringup():
    """Teardown + bring-up via the Week 3 script; raises unless its self-test passes."""
    require_root()
    teardown()
    p = subprocess.run(["bash", str(SCRIPT_DIR / "testbed_bringup.sh")],
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0 or "PASS" not in p.stdout:
        raise EnvError("bring-up failed:\n" + p.stdout[-2000:] + p.stderr[-1000:])


def kill_iperf3():
    subprocess.run(["pkill", "-9", "-x", "iperf3"], capture_output=True)


def flush_tcp_metrics():
    """Stop cached ssthresh/RTT from leaking between reps and cells."""
    for ns in (CLIENT_NS, SERVER_NS):
        sh(["ip", "tcp_metrics", "flush"], ns=ns, check=False)


# ---------------------------------------------------------------- shaping --
def netem_seed_supported():
    """Probe once per bring-up whether this iproute2 accepts `netem ... seed N`."""
    if "seed_ok" not in _state:
        p = sh(["tc", "qdisc", "replace", "dev", "c-wifi", "root", "netem",
                "delay", "1ms", "seed", "1"], ns=CLIENT_NS, check=False)
        _state["seed_ok"] = (p.returncode == 0)
        sh(["tc", "qdisc", "del", "dev", "c-wifi", "root"], ns=CLIENT_NS, check=False)
    return _state["seed_ok"]


def apply_shaping(cell, rep):
    """Replace netem on all four ends for this (cell, rep). Distinct seed per end."""
    legs = cfg.legs(cell)
    use_seed = netem_seed_supported()
    base = cfg.cell_seed(cell.cell_id, rep)
    for i, (ns, iface, leg) in enumerate(IFACES):
        seed = ((base + i) & 0x7FFFFFFF) if use_seed else None
        sh(["tc", "qdisc", "replace", "dev", iface, "root", "netem",
            *cfg.netem_args(legs[leg], seed)], ns=ns)
    return {"netem_seed_used": use_seed, "base_seed": base}


def clear_shaping():
    for ns, iface, _ in IFACES:
        sh(["tc", "qdisc", "del", "dev", iface, "root"], ns=ns, check=False)


_UNIT_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0}
_UNIT_MBIT = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}


def parse_tc_netem(text):
    """Parse `tc qdisc show` netem output -> dict(delay_ms, jitter_ms, loss_pct, rate_mbit).
    Missing fields are None (tc omits `loss` when it is 0)."""
    out = {"delay_ms": None, "jitter_ms": None, "loss_pct": None, "rate_mbit": None}
    m = re.search(r"delay\s+([\d.]+)(us|ms|s)\s+([\d.]+)(us|ms|s)", text)
    if m:
        out["delay_ms"] = float(m.group(1)) * _UNIT_MS[m.group(2)]
        out["jitter_ms"] = float(m.group(3)) * _UNIT_MS[m.group(4)]
    else:
        m = re.search(r"delay\s+([\d.]+)(us|ms|s)", text)
        if m:
            out["delay_ms"] = float(m.group(1)) * _UNIT_MS[m.group(2)]
    m = re.search(r"loss\s+([\d.]+)%", text)
    if m:
        out["loss_pct"] = float(m.group(1))
    m = re.search(r"rate\s+([\d.]+)([KMG]?)bit", text)
    if m:
        out["rate_mbit"] = float(m.group(1)) * _UNIT_MBIT[m.group(2)]
    return out


def verify_shaping(cell):
    """Read back tc on all four ends and compare with the config. Raises EnvError on
    any mismatch. Returns {iface: raw `tc -s qdisc show` text} for the run record."""
    legs = cfg.legs(cell)
    raw, problems = {}, []
    for ns, iface, leg in IFACES:
        text = ns_capture(ns, ["tc", "-s", "qdisc", "show", "dev", iface])
        raw[iface] = text
        got, want = parse_tc_netem(text), legs[leg]
        checks = (
            ("delay_ms", 0.01), ("jitter_ms", 0.01), ("loss_pct", 0.02),
        )
        for key, tol in checks:
            g = got[key] if got[key] is not None else 0.0
            if abs(g - want[key]) > tol:
                problems.append(f"{iface} {key}: want {want[key]} got {got[key]}")
        g_rate = got["rate_mbit"]
        if g_rate is None or abs(g_rate - want["rate_mbit"]) > 0.01 * want["rate_mbit"]:
            problems.append(f"{iface} rate_mbit: want {want['rate_mbit']} got {g_rate}")
    if problems:
        raise EnvError(f"shaping mismatch for {cell.cell_id}:\n  " + "\n  ".join(problems))
    return raw


# ---------------------------------------------------------- receive buffer -
def _read_rmem(ns):
    return tuple(int(x) for x in sh(["sysctl", "-n", "net.ipv4.tcp_rmem"], ns=ns).stdout.split())


def _read_moderate(ns):
    p = sh(["sysctl", "-n", "net.ipv4.tcp_moderate_rcvbuf"], ns=ns, check=False)
    return int(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


def set_receive_buffer(cell):
    """Apply the cell's receive-buffer level in the receiver namespace and read it back.
    'small'  : tcp_rmem pinned to (min, X, X) and tcp_moderate_rcvbuf=0 (autotuning off);
               the iperf3 server additionally gets -w X (SO_RCVBUF), see server_cmd().
    'default': original tcp_rmem / tcp_moderate_rcvbuf captured at bring-up are restored.
    Returns the effective settings for the run record."""
    ns = cfg.RECEIVER_NS
    if "rmem0" not in _state:
        _state["rmem0"] = _read_rmem(ns)
        _state["moderate0"] = _read_moderate(ns)
    lvl = cfg.BUFFER_LEVELS[cell.buffer]
    if lvl["tcp_rmem"] is None:
        rmem, moderate = _state["rmem0"], _state["moderate0"]
    else:
        rmem, moderate = lvl["tcp_rmem"], 0
    sh(["sysctl", "-qw", "net.ipv4.tcp_rmem=" + " ".join(map(str, rmem))], ns=ns)
    if moderate is not None:
        sh(["sysctl", "-qw", f"net.ipv4.tcp_moderate_rcvbuf={moderate}"], ns=ns, check=False)
    eff = {"buffer": cell.buffer, "tcp_rmem": _read_rmem(ns),
           "tcp_moderate_rcvbuf": _read_moderate(ns),
           "iperf_window_bytes": lvl["iperf_window_bytes"]}
    if eff["tcp_rmem"] != tuple(rmem):
        raise EnvError(f"tcp_rmem readback {eff['tcp_rmem']} != requested {tuple(rmem)}")
    if cell.buffer == "small" and eff["tcp_moderate_rcvbuf"] not in (0, None):
        raise EnvError("tcp_moderate_rcvbuf could not be disabled in the receiver namespace")
    return eff


# ------------------------------------------------------------ iperf3 cmds --
def server_cmd(cell, mptcp):
    """One-shot daemonised iperf3 server.
    NOTE: iperf3's `-w` (SO_RCVBUF) is CLIENT-ONLY -- the server rejects it
    ("parameter error - some option you are trying to set is client only";
    a long-standing iperf3 limitation, still present upstream: esnet/iperf#143).
    The 'small' receive-buffer level is enforced entirely by the tcp_rmem pin in
    set_receive_buffer() with autotuning (tcp_moderate_rcvbuf) off, applied to the
    receiver namespace before the server starts. No -w flag is passed here."""
    cmd = ["iperf3", "-s", "-1", "-D"]
    return (["mptcpize", "run"] + cmd) if mptcp else cmd


def client_cmd(mptcp, duration=cfg.DURATION_S):
    """Client sends (client -> server), JSON output on stdout. No -w: the buffer under
    test is the receiver's."""
    cmd = ["iperf3", "-c", SERVER_WIFI_IP, "-t", str(duration), "-J"]
    return (["mptcpize", "run"] + cmd) if mptcp else cmd


def start_server(cell, mptcp):
    p = subprocess.run(["ip", "netns", "exec", SERVER_NS, *server_cmd(cell, mptcp)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
    if p.returncode != 0:
        raise EnvError(f"iperf3 server failed to start: {p.stderr.strip()}")


# -------------------------------------------------------------- self-test --
def _selftest():
    require_root()
    print("bring-up ..."); bringup(); print("  OK")
    probes = [cfg.Cell(1, 1, 0, "default"), cfg.Cell(4, 5, 3, "small"),
              cfg.anchor_cells()[0]]
    try:
        for c in probes:
            info = apply_shaping(c, 1)
            verify_shaping(c)
            eff = set_receive_buffer(c)
            print(f"  {c.cell_id}: shaping verified, seed={info['netem_seed_used']}, "
                  f"rmem={eff['tcp_rmem']}, moderate={eff['tcp_moderate_rcvbuf']}")
        # restoring default must give back the original values
        set_receive_buffer(cfg.Cell(1, 1, 0, "default"))
        assert _read_rmem(cfg.RECEIVER_NS) == _state["rmem0"], "default rmem not restored"
        print("  default rmem restored: OK")
    finally:
        clear_shaping(); teardown()
    print("selftest: PASS")


def _parser_test():
    t = ("qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms  2ms loss 3.1% "
         "rate 20Mbit seed 123\n Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)")
    r = parse_tc_netem(t)
    assert r == {"delay_ms": 20.0, "jitter_ms": 2.0, "loss_pct": 3.1, "rate_mbit": 20.0}, r
    r = parse_tc_netem("qdisc netem 8002: root delay 5ms  1ms loss 1% rate 100Mbit")
    assert r["rate_mbit"] == 100.0 and r["loss_pct"] == 1.0
    print("parse_tc_netem: OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _parser_test()
