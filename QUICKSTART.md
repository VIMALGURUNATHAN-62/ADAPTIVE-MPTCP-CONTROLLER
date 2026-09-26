# Quickstart — how to run this

One machine, one VM, root access, kernel ≥ 5.19 with MPTCP. Do these in order. Each step
tells you what "it worked" looks like before you move to the next one.

## 0. Setup (one time)

Put every file from the zip in one folder on the VM, alongside your Week 3–4 files
(`testbed_bringup.sh`, `testbed_teardown.sh`, `netem_shaping.sh`). Same folder, no subfolders.

```bash
cd ~/mptcp-project        # wherever you put everything
sudo apt install -y iperf3 mptcpd iproute2 python3
pip install matplotlib --break-system-packages     # optional, only needed for the heatmap PNGs
```

## 1. Quick checks (a few seconds each, no long runs)

```bash
python3 policy.py --selftest          # -> "policy selftest: PASS"
python3 controller.py --selftest      # -> "controller selftest: PASS"
python3 mptcp_pm_nl.py selftest       # -> "selftest: PASS" + kernel family info
sudo python3 sweep_env.py --selftest  # -> "selftest: PASS" (needs root, touches real netem)
```

If any of these fail, stop and send me the exact output — don't go further until they pass.

## 2. Prove the controller can talk to the kernel (~1 minute)

```bash
sudo ./pm_userspace_probe.sh
```

Look for `WEEK 5 PM CHECK: PASS` at the end. This is the single most important checkpoint —
everything after this depends on it working.

## 3. Dry-run the big sweep before committing hours to it (~5 minutes)

```bash
sudo python3 run_sweep.py --dry-run --out results/dry_A
python3 parse_sweep.py results/dry_A
```

Check `results/dry_A/sweep.log` — you should see real Mbps numbers for both MPTCP and
single-path on each line, no repeated errors. If this looks sane, the full sweep will too.

## 4. Run the full characterization sweep (~6 hours, unattended)

```bash
sudo python3 run_sweep.py --out results/sweep_A
```

Run it in `tmux` or `screen` since it takes hours — reconnecting won't kill it, but closing
the terminal will. It's resumable: if it stops partway, run the exact same command again and
it picks up where it left off.

When it finishes:

```bash
python3 parse_sweep.py results/sweep_A
python3 hol_diagnosis.py results/sweep_A
```

Look at `results/sweep_A/harmful_region_heatmap.png` — this is the Week 6 deliverable.

## 5. Tune the controller on that data (~1 minute)

```bash
python3 replay_policy.py results/sweep_A
```

Look for `WEEK 7 VALIDATION: PASS` or `FAIL` — either way it writes
`results/sweep_A/controller_params.json`, which the next two steps need.

## 6. Wi-Fi-kill demo (~5 minutes)

```bash
sudo ./failover_demo.sh both 3 24 8
```

Watch the `SUMMARY` at the end — it prints how long the transfer stalled after Wi-Fi died,
for stock MPTCP vs. the controller, side by side.

## 7. Re-run the sweep with the controller active (~6 hours, unattended)

```bash
sudo python3 run_sweep_ctl.py --src results/sweep_A
```

Same idea as step 4 — run it in `tmux`, it's resumable.

## 8. Final comparison (~10 seconds)

```bash
python3 compare_configs.py --stock results/sweep_A --controller results/sweep_A_ctl
```

Look for `results/sweep_A_ctl/comparison/`:
- `comparison_report.txt` — the headline numbers and the WEEK 10 CLAIM CHECK verdict
- `recovery_heatmap.png`, `three_way_bars.png` — the figures

## If something breaks

Every script prints what it needs before it fails — a missing tool, a missing file
(`REQUEST FILE: ...`), or a field-coverage line showing 0 parsed samples. Copy the exact
error and the command you ran, and send both. Don't try to guess-fix the regexes yourself;
paste one real log line and I'll fix the parser.

## What you get out of this, in order

1. `results/sweep_A/harmful_region_heatmap.png` — where stock MPTCP loses to single-path TCP
2. `results/sweep_A/hol_diagnosis.txt` — why: receive-buffer stalls, path loss, or both
3. `results/sweep_A/controller_params.json` — the tuned detection thresholds
4. `results/failover_*/summary.json` — how fast the controller reacts to a dead link
5. `results/sweep_A_ctl/comparison/comparison_report.txt` — how much of the loss the
   controller recovers, and where it doesn't
