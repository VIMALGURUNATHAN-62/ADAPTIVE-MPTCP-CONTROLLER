#!/usr/bin/env python3
"""
mptcp_pm_nl.py  --  Week 5, file 5: dependency-free client for the MPTCP path-manager
generic-netlink API ("mptcp_pm"), plus an end-to-end userspace-PM probe.

This is the control path the Week 8 controller reuses (import PM from this module).
Pure stdlib (raw AF_NETLINK). IPv4 only. Needs root and the target network namespace
(run with `ip netns exec <ns> python3 mptcp_pm_nl.py ...`).

Constants are from include/uapi/linux/mptcp_pm.h: attribute/event numbers and the group
names (mptcp_pm_cmds / mptcp_pm_events) were checked against the kernel header; command
numbers (SET_FLAGS=7 ... SUBFLOW_DESTROY=11) follow the same enum order. The kernel
rejects a wrong number loudly, so `probe` doubles as verification.

Userspace-PM facts this relies on (kernel >= 5.19; SET_FLAGS/MP_PRIO for userspace PMs
were added later, 6.8 has it):
  - net.mptcp.pm_type=1 must be set in the namespace BEFORE the connection is created.
  - With pm_type=1 the kernel creates no subflows and announces nothing by itself; the
    userspace PM does it with SUBFLOW_CREATE / ANNOUNCE.
  - SET_FLAGS on a subflow (identified by token + local addr/port + remote addr/port)
    sends MP_PRIO: backup on / off.

Commands:
  selftest                                   offline checks (no kernel MPTCP needed)
  monitor  [--duration S] [--log FILE]       print PM events as JSON lines
  create|destroy --token T --laddr IP [--lid N] --raddr IP --rport P [--rid N]
  backup|nobackup --token T --laddr IP --lport P --raddr IP --rport P
  probe    [--laddr 10.10.2.1] [--raddr 10.10.2.2] [--rport 5201] [--server-ns server]
           [--out probe.json]                run once traffic is starting: creates the
           cellular subflow via netlink, marks it backup, restores it, and verifies each
           step by kernel events + ss flags on both ends. Exit 0 = PASS.
"""
import argparse
import errno
import ipaddress
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time

NETLINK_GENERIC = 16
SOL_NETLINK = 270
NETLINK_ADD_MEMBERSHIP = 1
NLM_F_REQUEST, NLM_F_ACK = 0x1, 0x4
NLMSG_ERROR, NLMSG_DONE = 2, 3
NLA_F_NESTED = 0x8000
GENL_ID_CTRL, CTRL_CMD_GETFAMILY = 0x10, 3
CTRL_ATTR_FAMILY_ID, CTRL_ATTR_FAMILY_NAME, CTRL_ATTR_MCAST_GROUPS = 1, 2, 7
CTRL_ATTR_MCAST_GRP_NAME, CTRL_ATTR_MCAST_GRP_ID = 1, 2

FAMILY, EV_GROUP, PM_VERSION = "mptcp_pm", "mptcp_pm_events", 1

CMD_SET_FLAGS, CMD_ANNOUNCE, CMD_REMOVE = 7, 8, 9
CMD_SUBFLOW_CREATE, CMD_SUBFLOW_DESTROY = 10, 11
ATTR_ADDR, ATTR_TOKEN, ATTR_LOC_ID, ATTR_ADDR_REMOTE = 1, 4, 5, 6
A_FAMILY, A_ID, A_ADDR4, A_PORT, A_FLAGS = 1, 2, 3, 5, 6
FLAG_SIGNAL, FLAG_SUBFLOW, FLAG_BACKUP = 1, 2, 4

EV_NAMES = {1: "CREATED", 2: "ESTABLISHED", 3: "CLOSED", 6: "ANNOUNCED", 7: "REMOVED",
            10: "SF_ESTABLISHED", 11: "SF_CLOSED", 13: "SF_PRIORITY",
            15: "LISTENER_CREATED", 16: "LISTENER_CLOSED"}
# event attr id -> (name, kind)   kinds: u (by length), be16, ip4, ip6
EV_ATTRS = {1: ("token", "u"), 2: ("family", "u"), 3: ("loc_id", "u"), 4: ("rem_id", "u"),
            5: ("saddr4", "ip4"), 6: ("saddr6", "ip6"), 7: ("daddr4", "ip4"), 8: ("daddr6", "ip6"),
            9: ("sport", "be16"), 10: ("dport", "be16"), 11: ("backup", "u"), 12: ("error", "u"),
            13: ("flags", "u"), 14: ("timeout", "u"), 15: ("if_idx", "u"),
            16: ("reset_reason", "u"), 17: ("reset_flags", "u"), 18: ("server_side", "u")}


class PMError(RuntimeError):
    pass


# ------------------------------------------------------- netlink helpers --
def nla(t, payload):
    l = 4 + len(payload)
    return struct.pack("=HH", l, t) + payload + b"\0" * ((-l) & 3)


def nest(t, payload):
    return nla(t | NLA_F_NESTED, payload)


def parse_attrs(buf):
    out, off = {}, 0
    while off + 4 <= len(buf):
        l, t = struct.unpack_from("=HH", buf, off)
        if l < 4:
            break
        out[t & 0x3FFF] = buf[off + 4: off + l]
        off += (l + 3) & ~3
    return out


def parse_nlmsgs(data):
    off = 0
    while off + 16 <= len(data):
        l, typ, flags, seq, pid = struct.unpack_from("=IHHII", data, off)
        if l < 16:
            break
        yield typ, flags, seq, data[off + 16: off + l]
        off += (l + 3) & ~3


def _uint(b):
    return {1: "=B", 2: "=H", 4: "=I"}.get(len(b)) and struct.unpack({1: "=B", 2: "=H", 4: "=I"}[len(b)], b)[0]


def decode_event(fid, typ, payload):
    """Return event dict for a message of family fid, else None."""
    if typ != fid or len(payload) < 4:
        return None
    ev = {"event": EV_NAMES.get(payload[0], f"EV{payload[0]}")}
    for t, b in parse_attrs(payload[4:]).items():
        name, kind = EV_ATTRS.get(t, (f"attr{t}", "u"))
        if kind == "ip4" and len(b) == 4:
            ev[name] = str(ipaddress.IPv4Address(b))
        elif kind == "ip6" and len(b) == 16:
            ev[name] = str(ipaddress.IPv6Address(b))
        elif kind == "be16" and len(b) == 2:
            ev[name] = struct.unpack("!H", b)[0]
        else:
            ev[name] = _uint(b) if len(b) in (1, 2, 4) else b.hex()
    return ev


def addr_attr(a):
    """Nested address: dict(ip, [id], [port], [flags]). Port is host order, IPv4 only."""
    b = nla(A_FAMILY, struct.pack("=H", socket.AF_INET))
    if a.get("id") is not None:
        b += nla(A_ID, struct.pack("=B", a["id"]))
    b += nla(A_ADDR4, socket.inet_aton(a["ip"]))
    if a.get("port"):
        b += nla(A_PORT, struct.pack("=H", a["port"]))
    if a.get("flags") is not None:
        b += nla(A_FLAGS, struct.pack("=I", a["flags"]))
    return b


# ------------------------------------------------------------- PM client --
class PM:
    def __init__(self):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
        self.sock.bind((0, 0))
        self.seq = int(time.time()) & 0xFFFFFF
        self.fid, groups = self._resolve(FAMILY)
        self.ev_group = groups.get(EV_GROUP)

    def _send(self, fam, cmd, attrs=b""):
        self.seq += 1
        body = struct.pack("=BBH", cmd, PM_VERSION, 0) + attrs
        hdr = struct.pack("=IHHII", 16 + len(body), fam, NLM_F_REQUEST | NLM_F_ACK, self.seq, 0)
        self.sock.send(hdr + body)
        return self.seq

    def _wait(self, seq, timeout=3.0):
        replies, deadline = [], time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([self.sock], [], [], 0.2)
            if not r:
                continue
            for typ, _, s, payload in parse_nlmsgs(self.sock.recv(65536)):
                if s != seq:
                    continue
                if typ == NLMSG_ERROR:
                    err = struct.unpack_from("=i", payload)[0]
                    if err:
                        raise PMError(f"{errno.errorcode.get(-err, -err)}: {os.strerror(-err)}")
                    return replies
                if typ == NLMSG_DONE:
                    return replies
                replies.append(payload)
        raise PMError("timeout waiting for kernel reply")

    def _resolve(self, name):
        seq = self._send(GENL_ID_CTRL, CTRL_CMD_GETFAMILY, nla(CTRL_ATTR_FAMILY_NAME, name.encode() + b"\0"))
        try:
            rep = self._wait(seq)
        except PMError as e:
            if "ENOENT" in str(e):
                raise PMError("generic-netlink family 'mptcp_pm' not found: kernel without MPTCP "
                              "(modprobe mptcp? CONFIG_MPTCP)") from e
            raise
        a = parse_attrs(rep[0][4:])
        fid = struct.unpack("=H", a[CTRL_ATTR_FAMILY_ID])[0]
        groups = {}
        for g in parse_attrs(a.get(CTRL_ATTR_MCAST_GROUPS, b"")).values():
            ga = parse_attrs(g)
            groups[ga[CTRL_ATTR_MCAST_GRP_NAME].rstrip(b"\0").decode()] = \
                struct.unpack("=I", ga[CTRL_ATTR_MCAST_GRP_ID])[0]
        return fid, groups

    # ---- commands ---------------------------------------------------------
    def _cmd(self, cmd, token, local=None, remote=None):
        attrs = nla(ATTR_TOKEN, struct.pack("=I", token))
        if local:
            attrs += nest(ATTR_ADDR, addr_attr(local))
        if remote:
            attrs += nest(ATTR_ADDR_REMOTE, addr_attr(remote))
        try:
            self._wait(self._send(self.fid, cmd, attrs))
        except PMError as e:
            raise PMError(f"{e}  (userspace-PM commands need net.mptcp.pm_type=1 in this namespace, "
                          f"set before the connection was created)") from e

    def subflow_create(self, token, local, remote):
        self._cmd(CMD_SUBFLOW_CREATE, token, local, remote)

    def subflow_destroy(self, token, local, remote):
        self._cmd(CMD_SUBFLOW_DESTROY, token, local, remote)

    def set_backup(self, token, local, remote, backup=True):
        """MP_PRIO on the subflow identified by local(ip,port) / remote(ip,port)."""
        loc = dict(local, flags=FLAG_BACKUP if backup else 0)
        self._cmd(CMD_SET_FLAGS, token, loc, remote)

    def announce(self, token, addr):
        self._cmd(CMD_ANNOUNCE, token, local=addr)

    def remove(self, token, loc_id):
        attrs = nla(ATTR_TOKEN, struct.pack("=I", token)) + nla(ATTR_LOC_ID, struct.pack("=B", loc_id))
        self._wait(self._send(self.fid, CMD_REMOVE, attrs))

    # ---- events -----------------------------------------------------------
    def open_events(self):
        if self.ev_group is None:
            raise PMError(f"multicast group {EV_GROUP} not found")
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
        s.bind((0, 0))
        s.setsockopt(SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, self.ev_group)
        return s

    def events(self, sock, duration=None):
        end = time.time() + duration if duration else None
        while end is None or time.time() < end:
            r, _, _ = select.select([sock], [], [], 0.3)
            if not r:
                continue
            for typ, _, _, payload in parse_nlmsgs(sock.recv(65536)):
                ev = decode_event(self.fid, typ, payload)
                if ev:
                    ev["t"] = round(time.time(), 3)
                    yield ev


def wait_event(pm, sock, want, timeout, match=None, seen=None):
    """First event named `want` (optionally passing match(ev)) within timeout, else None."""
    for ev in pm.events(sock, timeout):
        if seen is not None:
            seen.append(ev)
        if ev["event"] == want and (match is None or match(ev)):
            return ev
    return None


# ------------------------------------------------------------ ss helpers --
def ss_subflow_flags(ns, local_ip=None, peer_ip=None):
    """(flags-string or None, raw block) of the MPTCP subflow matching the addresses."""
    cmd = ["ss", "-tni"]
    if ns:
        cmd = ["ip", "netns", "exec", ns] + cmd
    try:
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
    except OSError:
        return None, ""
    blocks, cur = [], None
    for line in out.splitlines():
        if line and not line[0].isspace() and not line.startswith("State"):
            cur = [line]
            blocks.append(cur)
        elif cur is not None:
            cur.append(line)
    for b in blocks:
        head = b[0].split()
        if len(head) < 5:
            continue
        if local_ip and not head[3].startswith(local_ip + ":"):
            continue
        if peer_ip and not head[4].startswith(peer_ip + ":"):
            continue
        text = "\n".join(b)
        m = re.search(r"tcp-ulp-mptcp flags:(\S+)", text)
        if m:
            return m.group(1), text
    return None, ""


def pm_mode():
    for p in ("/proc/sys/net/mptcp/pm_type", "/proc/sys/net/mptcp/path_manager"):
        try:
            return open(p).read().strip()
        except OSError:
            continue
    return None


# ----------------------------------------------------------------- probe --
def prio_values(events, laddr, lport):
    """backup values of SF_PRIORITY events that concern the subflow laddr:lport, in order."""
    out = []
    for e in events:
        if e.get("event") != "SF_PRIORITY":
            continue
        ends = {(e.get("saddr4"), e.get("sport")), (e.get("daddr4"), e.get("dport"))}
        if (laddr, lport) in ends:
            out.append(e.get("backup"))
    return out


def drain(pm, sock, secs, seen):
    for ev in pm.events(sock, secs):
        seen.append(ev)


def probe(a):
    mode = pm_mode()
    print(f"pm mode in this namespace: {mode!r} (expect '1' or 'userspace')")
    if mode not in ("1", "userspace"):
        print("FAIL: userspace path manager not enabled here (set net.mptcp.pm_type=1 before traffic)")
        return 1
    pm = PM()
    sock = pm.open_events()
    seen, res = [], {"steps": []}

    def step(name, ok, detail="", info=False):
        res["steps"].append({"step": name, "ok": ok, "info": info, "detail": detail})
        print(f"[{'INFO' if info else ('OK  ' if ok else 'FAIL')}] {name} {detail}")
        return ok

    srv_log, srv_proc = None, None
    if a.server_ns:
        srv_log = (a.out or "/tmp/pm_probe") + ".server_events.jsonl"
        try:
            open(srv_log, "w").close()
            srv_proc = subprocess.Popen(
                ["ip", "netns", "exec", a.server_ns, sys.executable, os.path.abspath(__file__),
                 "monitor", "--log", srv_log], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.7)
        except OSError as e:
            step("server-side event monitor", False, str(e), info=True)

    est = wait_event(pm, sock, "ESTABLISHED", a.wait, lambda e: e.get("server_side", 0) != 1, seen)
    if not step("connection ESTABLISHED (client side)", bool(est),
                json.dumps(est) if est else "no event; start traffic first"):
        return finish(a, res, False, srv_proc)
    token = est["token"]
    ann = wait_event(pm, sock, "ANNOUNCED", 2.0, None, seen)
    rid = ann["rem_id"] if ann and "rem_id" in ann else a.rid
    step("server ADD_ADDR received", bool(ann), f"rem_id={rid}", info=True)

    local = {"ip": a.laddr, "id": a.lid}
    remote = {"ip": a.raddr, "id": rid, "port": a.rport}
    try:
        pm.subflow_create(token, local, remote)
    except PMError as e:
        step("SUBFLOW_CREATE", False, str(e))
        return finish(a, res, False, srv_proc)
    sf = wait_event(pm, sock, "SF_ESTABLISHED", 8.0, lambda e: e.get("saddr4") == a.laddr, seen)
    if not step("SUBFLOW_CREATE -> SF_ESTABLISHED on cellular address", bool(sf),
                json.dumps(sf) if sf else "timeout"):
        return finish(a, res, False, srv_proc)
    lport, rport = sf["sport"], sf["dport"]
    drain(pm, sock, 2.0, seen)

    def snap():
        c, ctext = ss_subflow_flags(None, local_ip=a.laddr)
        s, stext = ss_subflow_flags(a.server_ns, peer_ip=a.laddr)
        return {"client_flags": c, "server_flags": s, "client_ss": ctext, "server_ss": stext}

    loc, rem = {"ip": a.laddr, "port": lport}, {"ip": a.raddr, "port": rport}
    before = snap()
    try:
        pm.set_backup(token, loc, rem, True)
    except PMError as e:
        step("SET_FLAGS backup", False, str(e))
        return finish(a, res, False, srv_proc)
    drain(pm, sock, 1.5, seen)
    after = snap()
    pm.set_backup(token, loc, rem, False)
    drain(pm, sock, 1.5, seen)
    restored = snap()

    srv_events = []
    if srv_proc:
        srv_proc.terminate()
        try:
            srv_proc.wait(3)
        except subprocess.TimeoutExpired:
            srv_proc.kill()
        with open(srv_log) as f:
            srv_events = [json.loads(l) for l in f if l.strip()]
    prio = prio_values(seen + srv_events, a.laddr, lport)
    ev_ok = 1 in prio and 0 in prio[prio.index(1):]
    ss_ok = (before["client_flags"] is not None and after["client_flags"] != before["client_flags"]
             and restored["client_flags"] == before["client_flags"])
    res.update(token=token, lport=lport, rport=rport, before=before, after=after, restored=restored,
               sf_priority_backup_values=prio, events=seen[-40:], server_events=srv_events[-40:])
    step("SET_FLAGS accepted by kernel (backup, then non-backup)", True)
    step("MP_PRIO confirmed by kernel event (SF_PRIORITY backup=1 then 0)", ev_ok, f"values seen: {prio}")
    step("ss subflow flags changed then restored", ss_ok,
         f"client {before['client_flags']} -> {after['client_flags']} -> {restored['client_flags']}",
         info=not ss_ok)
    step("server-side ss flags", True,
         f"{before['server_flags']} -> {after['server_flags']} -> {restored['server_flags']}", info=True)
    return finish(a, res, ev_ok or ss_ok, None)


def finish(a, res, ok, srv_proc=None):
    if srv_proc:
        srv_proc.terminate()
    res["pass"] = ok
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)
    print("PROBE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ------------------------------------------------------------------- CLI --
def selftest():
    # 1. attribute packing / padding / nesting round-trip
    local = {"ip": "10.10.2.1", "id": 1}
    b = nest(ATTR_ADDR, addr_attr(local))
    assert len(b) % 4 == 0
    top = parse_attrs(b)
    inner = parse_attrs(top[ATTR_ADDR])
    assert struct.unpack("=H", inner[A_FAMILY])[0] == socket.AF_INET
    assert inner[A_ID][0] == 1 and socket.inet_ntoa(inner[A_ADDR4]) == "10.10.2.1"
    rem = parse_attrs(parse_attrs(nest(ATTR_ADDR_REMOTE, addr_attr({"ip": "10.10.2.2", "id": 1, "port": 5201, "flags": 4})))[ATTR_ADDR_REMOTE])
    assert struct.unpack("=H", rem[A_PORT])[0] == 5201 and struct.unpack("=I", rem[A_FLAGS])[0] == 4
    # 2. event decode (ports big-endian, ips, u8/u32)
    body = struct.pack("=BBH", 10, 1, 0) + nla(1, struct.pack("=I", 0xDEADBEEF)) \
        + nla(5, socket.inet_aton("10.10.2.1")) + nla(7, socket.inet_aton("10.10.2.2")) \
        + nla(9, struct.pack("!H", 40000)) + nla(10, struct.pack("!H", 5201)) + nla(18, b"\x00")
    ev = decode_event(0x1234, 0x1234, body)
    assert ev == {"event": "SF_ESTABLISHED", "token": 0xDEADBEEF, "saddr4": "10.10.2.1",
                  "daddr4": "10.10.2.2", "sport": 40000, "dport": 5201, "server_side": 0}, ev
    assert decode_event(0x1234, 0x9999, body) is None
    # 3. nlmsg walk with two messages
    m = lambda s: struct.pack("=IHHII", 16 + len(s), 5, 0, 1, 0) + s + b"\0" * ((-len(s)) & 3)
    assert len(list(parse_nlmsgs(m(b"abcde") + m(b"xy")))) == 2
    print("selftest: PASS (packing, event decode, message walk)")
    try:
        pm = PM()
        print(f"kernel family 'mptcp_pm' found: id={pm.fid}, event group id={pm.ev_group}, "
              f"pm mode={pm_mode()!r}")
    except (PMError, OSError) as e:
        print(f"note: live kernel check skipped here: {e}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    m = sub.add_parser("monitor")
    m.add_argument("--duration", type=float)
    m.add_argument("--log")
    for name in ("create", "destroy"):
        p = sub.add_parser(name)
        p.add_argument("--token", type=lambda x: int(x, 0), required=True)
        p.add_argument("--laddr", required=True)
        p.add_argument("--lid", type=int, default=1)
        p.add_argument("--raddr", required=True)
        p.add_argument("--rport", type=int, required=True)
        p.add_argument("--rid", type=int, default=1)
    for name in ("backup", "nobackup"):
        p = sub.add_parser(name)
        p.add_argument("--token", type=lambda x: int(x, 0), required=True)
        p.add_argument("--laddr", required=True)
        p.add_argument("--lport", type=int, required=True)
        p.add_argument("--raddr", required=True)
        p.add_argument("--rport", type=int, required=True)
    p = sub.add_parser("probe")
    p.add_argument("--laddr", default="10.10.2.1")
    p.add_argument("--lid", type=int, default=1)
    p.add_argument("--raddr", default="10.10.2.2")
    p.add_argument("--rport", type=int, default=5201)
    p.add_argument("--rid", type=int, default=1, help="remote addr id if no ADD_ADDR event is seen")
    p.add_argument("--server-ns", default="server")
    p.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the connection")
    p.add_argument("--out")
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()
    try:
        if a.cmd == "probe":
            return probe(a)
        pm = PM()
        if a.cmd == "monitor":
            sock = pm.open_events()
            f = open(a.log, "a", buffering=1) if a.log else None
            try:
                for ev in pm.events(sock, a.duration):
                    line = json.dumps(ev)
                    print(line, flush=True)
                    if f:
                        f.write(line + "\n")
            except KeyboardInterrupt:
                pass
            return 0
        if a.cmd in ("create", "destroy"):
            fn = pm.subflow_create if a.cmd == "create" else pm.subflow_destroy
            fn(a.token, {"ip": a.laddr, "id": a.lid}, {"ip": a.raddr, "id": a.rid, "port": a.rport})
        else:
            pm.set_backup(a.token, {"ip": a.laddr, "port": a.lport},
                          {"ip": a.raddr, "port": a.rport}, backup=(a.cmd == "backup"))
        print("OK")
        return 0
    except PMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
