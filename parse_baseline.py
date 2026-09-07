#!/usr/bin/env python3
import json
import re
import sys
import glob
import os
import statistics as stats
import csv


def load_iperf_json(path):
    with open(path) as f:
        data = json.load(f)
    end = data.get("end", {})
    sent = end.get("sum_sent", {})
    received = end.get("sum_received", {})
    bits_per_second = received.get("bits_per_second") or sent.get("bits_per_second")
    retransmits = sent.get("retransmits")
    return {
        "goodput_mbps": (bits_per_second or 0) / 1e6,
        "retransmits": retransmits if retransmits is not None else 0,
    }


def check_ss_subflows(ss_log_path):
    """Legacy signal, kept for visibility only — NOT trusted as the gate.
    A once-per-second ss -Mai poll can miss a live second subflow."""
    if not os.path.exists(ss_log_path):
        return False, "no ss log found"
    text = open(ss_log_path).read()
    values = re.findall(r"subflows:(\d+)", text)
    if not values:
        return False, "ss log has no MPTCP connections at all"
    reached_two = any(int(n) >= 2 for n in values)
    return reached_two, f"values seen: {sorted(set(values))}"


def check_monitor_join(monitor_log_path):
    """Authoritative signal: a direct kernel event (ip mptcp monitor).
    Confirmed against tcpdump independently earlier in this project —
    this is the check goodput comparisons are gated on."""
    if not os.path.exists(monitor_log_path):
        return False, "no monitor log found (older run, before this capture existed)"
    text = open(monitor_log_path).read()
    if "SF_ESTABLISHED" in text:
        joins = text.count("SF_ESTABLISHED")
        return True, f"SF_ESTABLISHED seen ({joins}x)"
    return False, "no SF_ESTABLISHED event in monitor log"


def mean_ci95(values):
    if not values:
        return (None, None)
    m = stats.mean(values)
    if len(values) < 2:
        return (m, 0.0)
    sd = stats.stdev(values)
    ci = 1.96 * sd / (len(values) ** 0.5)
    return (m, ci)


def main():
    if len(sys.argv) != 2:
        print("usage: parse_baseline.py <results_dir>")
        sys.exit(1)

    results_dir = sys.argv[1]
    rows = []

    for kind in ("mptcp", "singlepath"):
        for jf in sorted(glob.glob(os.path.join(results_dir, f"{kind}_rep*.json"))):
            rep = re.search(r"rep(\d+)\.json", jf).group(1)
            try:
                metrics = load_iperf_json(jf)
            except Exception as e:
                print(f"WARNING: could not parse {jf}: {e}")
                continue
            row = {"kind": kind, "rep": rep, **metrics}
            if kind == "mptcp":
                ss_log = os.path.join(results_dir, f"ss_mptcp_rep{rep}.log")
                mon_log = os.path.join(results_dir, f"mptcp_monitor_rep{rep}.log")
                ss_ok, ss_detail = check_ss_subflows(ss_log)
                mon_ok, mon_detail = check_monitor_join(mon_log)
                row["ss_subflow2_seen"] = ss_ok
                row["monitor_sf_established"] = mon_ok
                if os.path.exists(mon_log):
                    row["dual_subflow_confirmed"] = mon_ok
                    row["dual_subflow_detail"] = f"monitor: {mon_detail} | ss: {ss_detail}"
                else:
                    row["dual_subflow_confirmed"] = ss_ok
                    row["dual_subflow_detail"] = f"[NO MONITOR LOG, using weaker ss check] ss: {ss_detail}"
            rows.append(row)

    if not rows:
        print(f"No run JSON files found under {results_dir}")
        sys.exit(1)

    out_csv = os.path.join(results_dir, "baseline_results.csv")
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out_csv}\n")

    mptcp_rows = [r for r in rows if r["kind"] == "mptcp"]
    print("== Dual-subflow validation per MPTCP rep (monitor-log authoritative) ==")
    disagreements = 0
    for r in mptcp_rows:
        status = "OK  " if r["dual_subflow_confirmed"] else "FAIL"
        print(f"  rep{r['rep']}: {status}  {r['dual_subflow_detail']}")
        if r.get("ss_subflow2_seen") != r.get("monitor_sf_established"):
            disagreements += 1

    if disagreements:
        print(f"\nNote: ss-based and monitor-based checks disagreed on {disagreements} "
              f"rep(s) — this confirms the ss -Mai poll is an unreliable signal on its "
              f"own. The monitor log (direct kernel event) is being used as the gate.")

    confirmed_rows = [r for r in mptcp_rows if r["dual_subflow_confirmed"]]
    failed_rows = [r for r in mptcp_rows if not r["dual_subflow_confirmed"]]

    if failed_rows:
        print(f"\n{len(failed_rows)}/{len(mptcp_rows)} MPTCP reps did not confirm a "
              f"second subflow by the monitor log. Excluded from the comparison below.")

    if not confirmed_rows:
        print("\nZERO reps confirmed dual-subflow by the monitor log. No valid MPTCP "
              "data in this results directory.")
        sys.exit(1)

    mptcp_vals = [r["goodput_mbps"] for r in confirmed_rows]
    single_vals = [r["goodput_mbps"] for r in rows if r["kind"] == "singlepath"]

    m_m, m_ci = mean_ci95(mptcp_vals)
    print(f"\nStock MPTCP (dual-path confirmed) goodput: {m_m:7.2f} Mbps  "
          f"(95% CI +/- {m_ci:.2f}, n={len(mptcp_vals)})")
    s_m, s_ci = mean_ci95(single_vals)
    if s_m is not None:
        print(f"Single-path TCP                  goodput: {s_m:7.2f} Mbps  "
              f"(95% CI +/- {s_ci:.2f}, n={len(single_vals)})")

    if mptcp_vals and single_vals:
        if (m_m + m_ci) < (s_m - s_ci):
            print("\n=> Non-overlapping CIs: stock MPTCP goodput is SIGNIFICANTLY BELOW "
                  "single-path TCP in this scenario. Harmful case reproduced.")
        elif (s_m + s_ci) < (m_m - m_ci):
            print("\n=> Non-overlapping CIs: stock MPTCP goodput is significantly ABOVE "
                  "single-path TCP here.")
        else:
            print("\n=> CIs overlap — no significant difference yet.")


if __name__ == "__main__":
    main()
