# Week 10 — Comparative Analysis & Statistics

## 1. Objective

Compare stock MPTCP and controller-managed MPTCP under the same 54 network conditions, using 10 repetitions per condition.

Statistical rule used throughout the project:
a difference is considered significant only when the two 95% confidence intervals do not overlap.

## 2. Overall Results

| Verdict | Stock MPTCP | Controller |
|---|---:|---:|
| BENEFICIAL | 33 | 29 |
| NS | 13 | 21 |
| HARMFUL | 8 | 4 |

The harmful region decreased from 8/54 (14.8%) to 4/54 (7.4%).

Four of the eight stock-harmful cells moved to NS.
Four remained HARMFUL.
No BENEFICIAL or NS cell became HARMFUL.

## 3. Controller vs Stock Goodput

Across the 54 grid cells:

- CI-separated improvement: 4/54
- CI-separated regression: 1/54
- CI-overlapping: 49/54

The four CI-separated improvements were:

- rtt2_bw2_loss3_buf-small
- rtt2_bw5_loss3_buf-small
- rtt4_bw2_loss3_buf-small
- rtt4_bw5_loss3_buf-small

The single CI-separated regression was:

- rtt1_bw5_loss3_buf-default

## 4. Stock-Harmful Region

| Condition | Stock | Controller | Single Path | Recovery | Final Verdict |
|---|---:|---:|---:|---:|---|
| RTT2 / BW1 / 3% | 12.58 | 13.01 | 14.24 | 25.8% | HARMFUL |
| RTT2 / BW2 / 3% | 11.93 | 13.51 | 15.03 | 51.1% | HARMFUL |
| RTT2 / BW5 / 3% | 11.77 | 13.23 | 14.38 | 56.1% | NS |
| RTT4 / BW1 / 3% | 9.52 | 11.39 | 14.40 | 38.3% | NS |
| RTT4 / BW2 / 1% | 11.87 | 13.71 | 14.84 | 61.9% | NS |
| RTT4 / BW2 / 3% | 9.34 | 12.04 | 14.36 | 53.7% | HARMFUL |
| RTT4 / BW5 / 1% | 12.68 | 12.75 | 13.95 | 5.4% | NS |
| RTT4 / BW5 / 3% | 9.96 | 11.95 | 13.74 | 52.6% | HARMFUL |

Mean recovery across the eight stock-harmful cells: 43.1%.

The controller improved mean goodput in all eight previously harmful cells.

## 5. Controller Behaviour

In the 80 repetitions belonging to the stock-harmful cells:

- Wi-Fi traffic share: 78.66% -> 89.16%
- Cellular traffic share: 21.34% -> 10.84%
- Jain subflow byte-share fairness: 0.7526 -> 0.6230

The controller therefore shifted more traffic toward Wi-Fi in the harmful region.

Cellular-path retransmission rate decreased in 6/8 stock-harmful cells and increased in 2/8.

RTT inflation decreased in 3/8 and increased in 5/8.

Therefore, the recovery cannot be explained by a simple reduction in either RTT inflation or retransmissions alone.

## 6. Remaining Limitation

The four conditions that remained HARMFUL all used:

small buffer + 3% added loss

Corresponding default-buffer cases were NS under both stock and controller.

This identifies the remaining limitation as a small-buffer/high-loss region rather than a single RTT or bandwidth setting.

## 7. Figures

- week10_verdict_distribution.png
- week10_controller_vs_stock.png
- week10_harmful_recovery.png

## 8. Main Conclusion

The userspace controller partially mitigated the harmful MPTCP region.

The number of harmful grid cells was reduced from 8 to 4, and four previously harmful cells became statistically indistinguishable from the single-path baseline.

However, four small-buffer/3% loss conditions remained harmful, showing a boundary where subflow-level control alone did not fully recover single-path performance.
