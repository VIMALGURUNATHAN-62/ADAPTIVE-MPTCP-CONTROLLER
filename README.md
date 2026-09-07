# MPTCP Controller Project — Phase 1–2: Emulated Testbed & Baseline

Reproducing and measuring the "harmful region" of stock Multipath TCP (MPTCP)
on an emulated dual-path (Wi-Fi + cellular) Linux testbed, using nothing but
network namespaces, `tc netem`, and unmodified Linux ≥ 5.19.

## What this is

MPTCP (RFC 8684) lets a single TCP connection run over multiple network
paths at once. Two paths should mean more throughput — but when the paths
are asymmetric enough (very different RTT, loss, or bandwidth), head-of-line
blocking can make MPTCP **slower than plain single-path TCP**. This repo
builds an emulated two-path testbed on one Linux machine, reproduces that
harmful region under controlled conditions, and statistically confirms it
with repeated trials gated on kernel-level subflow-join events (not a
polling heuristic).

This is Phases 1–2 of a larger project; Phase 3 onward will build a
userspace controller that decides whether to enable/disable subflows to try
to recover the lost throughput. See `MPTCP_Learning_Guide.md` for full
background, terminology, and the debugging story behind the design
decisions below.

## Requirements

- Linux kernel ≥ 5.6 (ideally ≥ 5.19, for userspace Path Manager mode)
- Root/sudo (namespaces, `tc`, and `ip mptcp` all require it)
- Packages:
  ```bash
  sudo apt update
  sudo apt install -y iproute2 iperf3 tshark mptcpd mptcpize
  ```

Verify MPTCP is available before doing anything else:
```bash
uname -r
sysctl net.mptcp.enabled
```
If the sysctl call errors, your kernel build doesn't have MPTCP enabled —
that's a hard blocker, not something the scripts can work around.

## Files

| File | Purpose |
|---|---|
| `testbed_bringup.sh` | Creates `client`/`server` namespaces, wires up two veth pairs (`wifi` and `cell` legs), enables MPTCP, registers endpoints, and self-tests that a second subflow genuinely joins (`ip mptcp monitor` → `SF_ESTABLISHED`) before declaring the testbed ready. |
| `testbed_teardown.sh` | Removes the `client`/`server` namespaces. Safe to run anytime, including when nothing exists. |
| `netem_shaping.sh` | Defines three shaping scenarios (`baseline-symmetric`, `asymmetric-cell-degraded`, `asymmetric-wifi-lossy`) applied via `tc qdisc ... netem`, and can also be run standalone (`set`/`clear`/`show`). |
| `week4_baseline.sh` | Rebuilds the testbed, applies a scenario, and runs N repetitions of back-to-back MPTCP vs single-path TCP transfers (`iperf3`), capturing `ip mptcp monitor` and `ss -Mai` logs for each rep. |
| `parse_baseline.py` | Parses the JSON/log output into a CSV and prints a goodput comparison, gated on the monitor log (see below). |

## Running it

```bash
git clone <this-repo>
cd <this-repo>
chmod +x *.sh

# Bring up and self-test the testbed
sudo ./testbed_bringup.sh

# Run a baseline: <scenario> <reps> <duration_sec_per_rep>
sudo ./week4_baseline.sh asymmetric-cell-degraded 20 15

# Parse results (needs sudo too, since the results dir is root-owned
# from the previous step — or chown it back to your user first)
sudo python3 parse_baseline.py results/asymmetric-cell-degraded_<timestamp>/

# Tear down
sudo ./testbed_teardown.sh
```

Valid scenarios: `baseline-symmetric`, `asymmetric-cell-degraded`,
`asymmetric-wifi-lossy`.

## Why the monitor log, not `ss`, is the gate

Early runs used `ss -Mai` (polled once per second) to confirm a second
subflow had joined. This turned out to be an unreliable signal: the kernel's
own subflow counter doesn't reliably show `subflows:2` at every polling
instant even while the second subflow is genuinely alive. A side-by-side
comparison against `ip mptcp monitor` — a live, event-driven kernel netlink
log — showed the monitor catching real joins that the `ss` poll missed on
the same repetitions.

`parse_baseline.py` therefore treats `ip mptcp monitor`'s `SF_ESTABLISHED`
event as authoritative and **excludes any repetition from the goodput
comparison if that specific rep's monitor log doesn't show it** — a
repetition isn't just assumed dual-path, it's proven. The `ss` log is kept
in the output for visibility only.

## Result (n=20, `asymmetric-cell-degraded`)

Scenario: Wi-Fi leg at 5ms delay / 1% loss / 100 Mbit; cellular leg at 90ms
delay, 15ms jitter, 6% loss, 3 Mbit.

```
Stock MPTCP (dual-path confirmed) goodput:   11.57 Mbps  (95% CI +/- 1.35, n=20)
Single-path TCP                  goodput:   17.00 Mbps  (95% CI +/- 1.43, n=20)

=> Non-overlapping CIs: stock MPTCP goodput is SIGNIFICANTLY BELOW
   single-path TCP in this scenario. Harmful case reproduced.
```

All 20 repetitions passed the dual-subflow gate (`SF_ESTABLISHED` confirmed
in every monitor log), so this isn't an artifact of a flaky testbed — the
second subflow reliably joined, and it still measurably dragged goodput
down relative to plain single-path TCP. This satisfies the Phase 2 goal:
reproduce, with statistical backing, at least one asymmetric case where
stock MPTCP underperforms single-path TCP.

## Project status

| Phase | What happens | Status |
|---|---|---|
| 1 | Build an emulated dual-path testbed on one Linux machine | ✅ Done |
| 2 | Measure stock MPTCP vs single-path TCP under controlled asymmetry | ✅ Done |
| 3 | Design a telemetry-driven controller (Sense → Decide → Act) via the userspace PM API | ⬜ Not started |
| 4 | Re-run experiments with the controller active, compare against Phase 2 baseline | ⬜ Not started |
| 5 | Report and live demo | ⬜ Not started |

## Further reading

`MPTCP_Learning_Guide.md` in this repo covers MPTCP fundamentals, a full
walkthrough of each script, the `ss`-vs-monitor debugging story in detail,
and what Phase 3 (the controller) needs to build on top of this.
