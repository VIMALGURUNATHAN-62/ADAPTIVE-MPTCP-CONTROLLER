# MPTCP Controller Project — Phase 1–4: Testbed, Baseline, Controller, Comparison

Reproducing and measuring the "harmful region" of stock Multipath TCP (MPTCP)
on an emulated dual-path (Wi-Fi + cellular) Linux testbed, then building a
kernel-unmodified userspace controller that recovers most of the lost
goodput — using nothing but network namespaces, `tc netem`, `iperf3`, and
the standard MPTCP path-manager netlink API on unmodified Linux ≥ 5.19.

## What this is

MPTCP (RFC 8684) lets a single TCP connection run over multiple network
paths at once. Two paths should mean more throughput — but when the paths
are asymmetric enough (very different RTT, loss, or bandwidth), head-of-line
blocking and path loss can make MPTCP **slower than plain single-path TCP**.

This project:
1. Builds an emulated two-path testbed on one Linux machine and reproduces
   that harmful region under controlled, swept conditions, statistically
   confirmed with repeated trials gated on kernel-level subflow-join events.
2. Diagnoses *why* specific cells are harmful (receive-buffer stalls vs.
   path loss vs. both).
3. Builds a userspace controller — Sense (poll `ss -tni`) → Decide (RTT +
   retransmit-rate scoring with hysteresis) → Act (`MP_PRIO`/`SUBFLOW_CREATE`/
   `SUBFLOW_DESTROY` via the standard MPTCP path-manager netlink API,
   **no kernel modification**) — that demotes the degraded path to backup.
4. Re-runs the full characterization sweep with the controller active and
   compares single-path TCP vs. stock MPTCP vs. controller-managed MPTCP.

See `docs/MPTCP_Learning_Guide.md` for MPTCP fundamentals, terminology, and
the debugging story behind several design decisions below.

## Requirements

- Linux kernel ≥ 5.19 (userspace Path Manager mode)
- Root/sudo (namespaces, `tc`, `ip mptcp`, and the PM netlink API all need it)
- Packages:

```bash
sudo apt update
sudo apt install -y iproute2 iperf3 tshark mptcpd mptcpize python3-pip
pip install matplotlib --break-system-packages   # optional, for heatmap/plot figures
```

Verify MPTCP is available before doing anything else:

```bash
uname -r
sysctl net.mptcp.enabled
```

If the sysctl call errors, your kernel build doesn't have MPTCP enabled —
that's a hard blocker, not something the scripts can work around.

## Files

### Phase 1–2: testbed and baseline

| File | Purpose |
|---|---|
| `testbed_bringup.sh` | Creates `client`/`server` namespaces, wires up two veth pairs (`wifi` and `cell` legs), enables MPTCP, registers endpoints, and self-tests that a second subflow genuinely joins (`ip mptcp monitor` → `SF_ESTABLISHED`) before declaring the testbed ready. |
| `testbed_teardown.sh` | Removes the `client`/`server` namespaces. Safe to run anytime, including when nothing exists. |
| `netem_shaping.sh` | Defines shaping scenarios applied via `tc qdisc ... netem`; can also be run standalone (`set`/`clear`/`show`). |
| `week4_baseline.sh` | Rebuilds the testbed, applies a scenario, and runs N repetitions of back-to-back MPTCP vs. single-path TCP transfers, capturing `ip mptcp monitor` and `ss -Mai` logs per rep. |
| `parse_baseline.py` | Parses that output into a CSV and prints a goodput comparison, gated on the monitor log. |

### Phase 3: characterization sweep and controller

| File | Purpose |
|---|---|
| `sweep_config.py` | Sweep axes (RTT ratio, bandwidth ratio, added loss, receive buffer), cell enumeration, deterministic per-rep seeds. |
| `sweep_env.py` | Testbed control for the sweep: netem shaping, receive-buffer pinning, iperf3 command builders. |
| `run_sweep.py` | Orchestrates the full stock-MPTCP-vs-single-path sweep across every cell; resumable. |
| `parse_sweep.py` | Per-cell statistics (95% CI), HARMFUL/BENEFICIAL/NS verdicts, harmful-region heatmap. |
| `mptcp_pm_nl.py` | Dependency-free client for the MPTCP path-manager generic-netlink API (`SUBFLOW_CREATE`/`DESTROY`, `SET_FLAGS`/`MP_PRIO`), plus an end-to-end control-path probe. |
| `pm_userspace_probe.sh` | Proves the userspace path-manager control path works end to end before trusting anything built on top of it. |
| `hol_diagnosis.py` | Tests whether harmful cells are receive-buffer-limited, loss-limited, or both, and reports which client-visible telemetry signals actually separate harmful from beneficial reps. |
| `policy.py` | The controller's Sense→Decide state machine (pure, no I/O) — RTT/retransmit scoring, hysteresis, staleness detection for a silently-dead subflow, primary-subflow protection. Has its own `--selftest`. |
| `replay_policy.py` | Replays the policy over real sweep traces, searches detection thresholds, reports TPR/FPR, writes `controller_params.json`. |
| `controller.py` | The live Sense→Decide→Act daemon. Has its own `--selftest` and `--dry-run`. |
| `failover_demo.sh` | Kills Wi-Fi mid-download and measures interruption time, stock vs. controller. |

### Phase 4: controller-active sweep and comparison

| File | Purpose |
|---|---|
| `run_sweep_ctl.py` | Mirrors the Phase 3 sweep — same cells, same seeds, same reps — with the controller active. |
| `compare_configs.py` | Three-way comparison (single-path / stock / controller), per-cell recovery figure, final heatmap and bar charts. |

## Running it

```bash
git clone <this-repo>
cd <this-repo>
chmod +x *.sh

# Phase 1-2: baseline reproduction (see original Phase 1-2 section below)
sudo ./testbed_bringup.sh
sudo ./week4_baseline.sh asymmetric-cell-degraded 20 15
python3 parse_baseline.py results/asymmetric-cell-degraded_<timestamp>/
sudo ./testbed_teardown.sh

# Phase 3: prove the controller's control path, then sweep
sudo ./pm_userspace_probe.sh
sudo python3 run_sweep.py --out results/sweep_A
python3 parse_sweep.py results/sweep_A
python3 hol_diagnosis.py results/sweep_A
python3 replay_policy.py results/sweep_A
sudo ./failover_demo.sh both 1 75 8

# Phase 4: controller-active sweep, then compare
sudo python3 run_sweep_ctl.py --src results/sweep_A --out results/sweep_A_ctl_clean
python3 parse_sweep.py results/sweep_A_ctl_clean
python3 compare_configs.py --stock results/sweep_A --controller results/sweep_A_ctl_clean
```

Full step-by-step guide, including what "it worked" looks like at each
stage: `QUICKSTART.md`.

## Why the monitor log, not `ss`, is the gate

Early runs used `ss -Mai` (polled once per second) to confirm a second
subflow had joined. This turned out to be an unreliable signal: the kernel's
own subflow counter doesn't reliably show `subflows:2` at every polling
instant even while the second subflow is genuinely alive. A side-by-side
comparison against `ip mptcp monitor` — a live, event-driven kernel netlink
log — showed the monitor catching real joins that the `ss` poll missed on
the same repetitions. Every script in this project treats `SF_ESTABLISHED`
in the monitor log as the authoritative dual-subflow gate; `ss` is kept for
visibility only.

## Results

### Phase 2: harmful region reproduced (n=20, `asymmetric-cell-degraded`)

```
Stock MPTCP (dual-path confirmed) goodput:   11.57 Mbps  (95% CI +/- 1.35, n=20)
Single-path TCP                  goodput:   17.00 Mbps  (95% CI +/- 1.43, n=20)

=> Non-overlapping CIs: stock MPTCP goodput is SIGNIFICANTLY BELOW
   single-path TCP in this scenario. Harmful case reproduced.
```

### Phase 3: full characterization sweep (560 reps, 54 grid cells + 2 anchors, n=10/cell)

8/54 grid cells were HARMFUL under stock MPTCP, concentrated in
`buffer=small` cells with added cellular loss of 1% or 3% — see
`evidence/week6_harmful_region_heatmap.png` and `evidence/hol_diagnosis.txt`.
The Phase 2 anchor point reproduces cleanly at full statistical rigor.

### Phase 4: controller-managed MPTCP vs. stock (same 560-rep sweep, controller active)

The controller improved goodput in **every one of the 8 harmful cells**.

The values in the following table are the 10-repetition cell means from `results/sweep_A`
(stock) and `results/sweep_A_ctl_clean` (controller), with the paired single-path baseline
from the corresponding Week 6/stock results.

| Cell | Stock (Mbps) | Controller (Mbps) | Single-path (Mbps) |
|---|---|---|---|
| rtt4_bw2_loss1_buf-small | 11.87 | 13.71 | 14.84 |
| rtt4_bw5_loss1_buf-small | 12.68 | 12.75 | 13.95 |
| rtt2_bw1_loss3_buf-small | 12.58 | 13.01 | 14.24 |
| rtt2_bw2_loss3_buf-small | 11.93 | 13.51 | 15.03 |
| rtt2_bw5_loss3_buf-small | 11.77 | 13.23 | 14.38 |
| rtt4_bw1_loss3_buf-small | 9.52 | 11.39 | 14.40 |
| rtt4_bw2_loss3_buf-small | 9.34 | 12.04 | 14.36 |
| rtt4_bw5_loss3_buf-small | 9.96 | 11.95 | 13.74 |

Four of the eight stock-harmful cells became statistically indistinguishable from single-path performance under the project CI rule. The mean recovery across the eight cells was **0.43** (43.1% when expressed as a percentage of the stock-to-single-path gap).

On the Phase 2 anchor cells specifically:

```
anchor-w4degraded_buf-default:  10.56 -> 12.73 Mbps  (+21%, RECOVERED)
anchor-w4degraded_buf-small:     6.07 -> 10.30 Mbps  (+70%, PARTIAL)
```

Of the 46 non-harmful cells, only 1 regressed — the controller mostly stays
out of the way where it should. See `evidence/comparison_report.txt`,
`evidence/week10_recovery_heatmap.png`, and `evidence/week10_three_way_bars.png`.

### Phase 3: Wi-Fi failover

Killing the Wi-Fi link mid-transfer, the controller detects the failure via
staleness (no new telemetry sample, not just a retransmit spike — a
downed interface's socket keeps reporting frozen counters rather than
disappearing) and demotes the dead path via `MP_PRIO`. See
`evidence/week8_failover_plot.png`.

## Design notes worth knowing before extending this

- **The controller never attempts `SUBFLOW_DESTROY` on the primary
  (subflow id 0) path** — only `MARK_BACKUP`. Its `WITHDRAW` netlink call
  must use the subflow's live, negotiated local/remote IP:port, not the
  path's static config — the two are not interchangeable, and using the
  wrong one produces `EINVAL` that looks like a kernel restriction but isn't.
- **Failure detection is staleness-based, not purely retransmit-based.** A
  hard link failure (`ip link set ... down`) doesn't reliably produce fresh
  retransmit samples — the socket can keep appearing in `ss` output with
  frozen counters. `policy.py` tracks time-since-last-forward-progress
  (`data_segs_out` actually advancing), not time-since-last-sample.
- **A long-lived controller daemon must evict connections whose kernel
  state silently disappeared** (namespace rebuild, forceful teardown) —
  it will never receive a clean `CLOSED` netlink event for them. See
  `CONN_IDLE_TIMEOUT_S` in `controller.py`.
- **Detection is not perfect.** `replay_policy.py` on real sweep traces:
  TPR ≈ 0.78, FPR ≈ 0.22 for the tuned parameters. RTT inflation and
  retransmit rate — the only signals visible from the client's `ss` output
  — do not perfectly separate harmful from beneficial conditions. Report
  this limitation alongside the recovery numbers; it's the honest ceiling
  on subflow-level, client-only detection.

## Project status

| Phase | What happens | Status |
|---|---|---|
| 1 | Build an emulated dual-path testbed on one Linux machine | ✅ Done |
| 2 | Measure stock MPTCP vs. single-path TCP under controlled asymmetry | ✅ Done |
| 3 | Telemetry-driven controller (Sense → Decide → Act) via the userspace PM API | ✅ Done |
| 4 | Re-run experiments with the controller active, compare against Phase 2/3 baseline | ✅ Done |
| 5 | Report and live demo | ⬜ In progress |

## Further reading

`docs/MPTCP_Learning_Guide.md` covers MPTCP fundamentals, a full walkthrough
of the Phase 1–2 scripts, and the `ss`-vs-monitor debugging story in detail.
`QUICKSTART.md` covers the full Phase 3–4 pipeline step by step.
