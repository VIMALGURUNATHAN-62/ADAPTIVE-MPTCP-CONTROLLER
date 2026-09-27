# MPTCP Controller Project — Learning Guide & Onboarding Handbook

**For:** a new team member with general networking concepts (TCP/IP, sockets, routing) but no prior hands-on experience with MPTCP, Linux network namespaces, or this project specifically.
**Goal of this document:** by the end, you should be able to (1) explain what this project is and why it matters, (2) build and validate the testbed yourself, (3) run the experiments and read the results correctly, (4) understand *why* several early results were wrong and how that was caught, and (5) understand the current controller, evaluation, and reporting stage.

Read Parts 1–3 in order first — they build on each other. Parts 4 onward are hands-on and reference-style; keep them open while you're at a terminal.

---

## Table of Contents

1. [Conceptual Foundations](#part-1-conceptual-foundations)
2. [The Project, In Plain Language](#part-2-the-project-in-plain-language)
3. [The Toolbox](#part-3-the-toolbox--what-each-tool-does-and-why)
4. [Hands-On: Build the Testbed Yourself](#part-4-hands-on-build-the-testbed-yourself)
5. [Hands-On: Run the Baseline Experiments](#part-5-hands-on-run-the-baseline-experiments)
6. [The Debugging Story — Lessons Worth Internalizing](#part-6-the-debugging-story--lessons-worth-internalizing)
7. [Where the Project Stands Today](#part-7-where-the-project-stands-today)
8. [How to Continue: Week 5 Onward](#part-8-how-to-continue-week-5-onward)
9. [Glossary](#part-9-glossary)
10. [Command Cheat Sheet](#part-10-command-cheat-sheet)
11. [Self-Check Questions](#part-11-self-check-questions)

---

## Part 1: Conceptual Foundations

### 1.1 What you already know, and the one gap we need to close

You know TCP: a connection between two endpoints, identified by a 4-tuple (source IP, source port, destination IP, destination port), delivering bytes reliably and **in order**, using one path through the network. That last part — *one path* — is the entire gap. Everything in this project follows from asking: "what if a connection could use more than one path at once, and what goes wrong when it does?"

### 1.2 What is Multipath TCP (MPTCP)?

MPTCP is a real, standardized extension to TCP (RFC 8684) that lets a single logical connection be carried over **multiple network paths simultaneously** — for example, your phone's Wi-Fi and its cellular radio at the same time. To an application (a browser, a video call), it still looks like one ordinary TCP socket. Underneath, the kernel splits it into multiple **subflows**, each of which is a real, independent TCP connection over a different path, and stitches the data back together in order before handing it to the application.

Why would you want this? Two reasons:
- **Throughput:** two paths can carry more data than one.
- **Resilience / seamless handover:** if Wi-Fi drops, the cellular subflow is already established and traffic can continue without the application noticing — this is the "seamless connectivity" promise behind MPTCP on modern smartphones.

### 1.3 Key vocabulary (you'll see these constantly)

| Term | Meaning |
|---|---|
| **Subflow** | One of the individual TCP connections that make up an MPTCP session. A 2-path MPTCP connection has 2 subflows. |
| **MP_CAPABLE** | The TCP option exchanged on the *first* subflow's handshake, announcing "I can speak MPTCP." |
| **MP_JOIN** | The TCP option exchanged when a *second* (or later) subflow joins an existing MPTCP connection. |
| **ADD_ADDR** | A signal one side sends the other, announcing "I also have an address reachable at X — you can open another subflow to it." |
| **Path Manager (PM)** | The component deciding which addresses to advertise and which subflows to create. It can live in the kernel (default) or in **userspace** (a mode added in Linux 5.19) — this distinction is central to this project. |
| **Backup subflow** | A subflow marked as low-priority; the kernel prefers not to schedule fresh data on it unless the primary subflow(s) are unavailable. |
| **Goodput** | Actual application-level throughput (as opposed to raw bitrate, which includes retransmissions and headers). This project always measures goodput. |

### 1.4 The core problem this project investigates — with an analogy

You'd expect "two paths" to always be better than "one path." It often isn't. Here's why, using an analogy:

> Imagine moving house with two trucks: one takes the highway (fast, predictable), one takes the back roads (slower, unpredictable). If you split your boxes evenly between the two trucks, your friends unpacking at the destination can't put the furniture together until *all* the relevant boxes arrive — including the ones stuck on the slow truck. If the highway truck arrives 20 minutes before the back-roads truck, those 20 minutes are wasted waiting, even though "more trucks" was supposed to be faster.

TCP has to deliver bytes to the application **in order**. If a fast subflow and a slow subflow are both carrying pieces of the same byte stream, the receiver must hold the fast subflow's data in a buffer until the slow subflow's data catches up. This is called **head-of-line (HoL) blocking**. If the two paths are asymmetric enough (very different RTT, bandwidth, or loss), the waiting time can outweigh the benefit of the second path entirely — and MPTCP ends up **slower than plain single-path TCP would have been**.

This is not a hypothetical for this project — it is the exact effect this project reproduces and measures in Week 4 (see Part 7).

### 1.5 Where this project's approach sits

There are two ways to fix HoL-blocking-driven underperformance:
1. **Smarter packet scheduling inside the kernel** — decide, packet by packet, which subflow to send each piece of data on (this is what published schedulers like BLEST and ECF do). Requires patching the kernel.
2. **Coarser control from outside the kernel** — decide, subflow by subflow, whether a path should be used at all right now (mark it "backup", withdraw it, restore it), using the **userspace Path Manager API** that ships in unmodified Linux ≥ 5.19. No kernel patch needed.

This project takes approach (2). That single sentence is the project's entire research angle — everything else in this guide supports building, measuring against, and eventually automating that decision.

---

## Part 2: The Project, In Plain Language

**Research question:** *How much of the throughput loss that stock MPTCP shows on asymmetric Wi-Fi + cellular paths can a userspace, kernel-unmodified controller recover by only turning subflows on/off/backup — without touching per-packet scheduling?*

**The plan, in five phases:**

| Phase | What happens | Status |
|---|---|---|
| 1 | Build an emulated dual-path testbed on one Linux machine | ✅ Done (Week 3) |
| 2 | Measure stock MPTCP vs single-path TCP under controlled path asymmetry — reproduce the "harmful region" | ✅ Done (Weeks 4–6) |
| 3 | Design and implement a telemetry-driven controller (Sense → Decide → Act) using the userspace PM API | ✅ Done (Weeks 7–8) |
| 4 | Re-run experiments with the controller active and compare against the stock baseline | ✅ Done (Weeks 9–10) |
| 5 | Report and live demo | 🟡 In progress (Weeks 11–12) |

The detailed hands-on material below is intentionally strongest for Phases 1–2, because that is where the testbed and validation story was established. The current Phase 3–5 status is summarized in Part 7 and in the repository `README.md` and `QUICKSTART.md`.

---

## Part 3: The Toolbox — What Each Tool Does and Why

You do not need real Wi-Fi and cellular hardware for any of this. Everything is emulated on a single Ubuntu VM. Here is every tool you'll touch, what it's *for*, and a minimal example.

### 3.1 Linux network namespaces — "fake separate computers"

A network namespace gives a set of processes their own private network stack: their own IP addresses, routing table, and interfaces, completely isolated from the rest of the machine — as if it were a separate computer with nothing plugged in. This project creates **two** namespaces, `client` and `server`, to emulate two distinct hosts on one VM.

```bash
sudo ip netns add client          # create a namespace
sudo ip netns exec client ip addr # run any command "inside" it
sudo ip netns del client          # destroy it (and everything inside it)
```

### 3.2 veth pairs — "virtual network cables"

A veth pair is a virtual Ethernet cable with two ends. Plug one end into the `client` namespace and the other into `server`, and traffic sent into one end comes out the other — exactly like a real cable connecting two machines. This project creates **two separate veth pairs** — one to emulate the Wi-Fi path, one to emulate the cellular path.

```bash
sudo ip link add c-wifi type veth peer name s-wifi   # create a cable with two named ends
sudo ip link set c-wifi netns client                  # plug one end into 'client'
sudo ip link set s-wifi netns server                  # plug the other into 'server'
```

### 3.3 `ip mptcp` — the MPTCP control surface

The `ip mptcp` command family (part of modern `iproute2`) is how you configure and inspect MPTCP behavior:

```bash
ip mptcp endpoint add 10.10.2.2 dev s-cell signal   # announce this address to peers (ADD_ADDR)
ip mptcp endpoint add 10.10.2.1 dev c-cell subflow  # register a local address as usable for a new subflow
ip mptcp limits set subflows 1 add_addr_accepted 1  # raise the caps that default to 0 (see Part 6)
ip mptcp endpoint show                              # list registered endpoints
ip mptcp limits show                                # show current caps
ip mptcp monitor                                     # live stream of kernel PM events (the authoritative signal — see Part 6)
```

### 3.4 `mptcpize` — force ordinary programs to use MPTCP

Most programs (like `iperf3`) call the standard `socket()` API and get plain TCP by default. `mptcpize run <command>` transparently makes that program's sockets use MPTCP instead, without needing to recompile it.

```bash
sudo ip netns exec server mptcpize run iperf3 -s -1 -D    # MPTCP-enabled iperf3 server
sudo ip netns exec client mptcpize run iperf3 -c 10.10.1.2 -t 10   # MPTCP-enabled client
```

### 3.5 `iperf3` — generate measurable traffic

The standard tool for generating a TCP (or here, MPTCP) transfer and reporting throughput. `-J` gives machine-readable JSON, which is what the analysis scripts parse.

### 3.6 `ss -Mai` — snapshot subflow state

Shows currently-open MPTCP connections and their subflow counts. Useful, but has an important limitation you must understand — covered in detail in Part 6.

```bash
sudo ip netns exec client ss -Mai
# ESTAB ... subflows:2 subflows_max:2 ...   <- two subflows are active on this connection
```

### 3.7 `tshark` / `tcpdump` — packet-level proof

Captures raw packets so you can see the actual MP_CAPABLE / MP_JOIN handshake, not just a kernel-reported summary. This is the strongest form of evidence available and is used to independently confirm what `ss` and `ip mptcp monitor` report.

```bash
sudo ip netns exec client tcpdump -i c-cell -n -c 10
```

### 3.8 `tc` / `netem` — emulate bad networks on purpose

`tc` (traffic control) with the `netem` queuing discipline lets you inject artificial delay, jitter, packet loss, and a rate cap onto an interface — this is how "cellular path, 90 ms RTT, 6% loss, 3 Mbit" gets created without real hardware. **Important:** `netem` only shapes *outgoing (egress)* traffic, so to shape a path realistically you must apply it on **both ends** of the veth pair.

```bash
sudo ip netns exec client tc qdisc replace dev c-cell root netem delay 90ms 15ms distribution normal loss 6% rate 3mbit
```

---

## Part 4: Hands-On — Build the Testbed Yourself

### 4.1 Prerequisites

- Ubuntu Server ≥ 24.04 (this project uses 24.04.4 LTS, kernel `6.8.0-137-generic`). Check yours:
  ```bash
  lsb_release -a
  uname -r
  sysctl net.mptcp.enabled     # should print net.mptcp.enabled = 1 (or return without error)
  ```
  MPTCP needs kernel ≥ 5.6 at a bare minimum, ideally ≥ 5.19 for the userspace PM mode this project is ultimately aiming for.

- Install the toolchain:
  ```bash
  sudo apt update
  sudo apt install -y iproute2 iperf3 tshark mptcpd mptcpize
  ```

### 4.2 The topology you're about to build

```
   Client Namespace                          Server Namespace
  ┌─────────────────────┐                   ┌─────────────────────┐
  │ c-wifi 10.10.1.1/24 │ ── Wi-Fi path ──→ │ s-wifi 10.10.1.2/24 │
  │ c-cell 10.10.2.1/24 │ ─ Cellular path ─→│ s-cell 10.10.2.2/24 │
  └─────────────────────┘                   └─────────────────────┘
```

Two namespaces, two veth pairs, four IP addresses. That's the entire physical picture — everything else is configuration on top of it.

### 4.3 Get the scripts and understand them before running them

You should have `testbed_bringup.sh` and `testbed_teardown.sh`. **Do not just run them blind** — walk through what each does, because you will need to debug this exact logic later.

`testbed_teardown.sh` is short and safe to read first — it just deletes both namespaces (which automatically removes any veth ends still inside them):

```bash
#!/usr/bin/env bash
set -uo pipefail
for ns in client server; do
  if ip netns list | grep -q "^${ns}\b"; then
    ip netns del "$ns"
    echo "Removed namespace: $ns"
  else
    echo "Namespace $ns not present"
  fi
done
```

`testbed_bringup.sh` does six things, in order. Read this as a checklist, not just a script:

1. **Verify MPTCP is available** on the kernel (`sysctl net.mptcp.enabled`).
2. **Create the two namespaces.**
3. **Create the two veth pairs** and place one end of each into each namespace.
4. **Assign IP addresses and bring interfaces up** on both ends of both pairs.
5. **Enable MPTCP and configure endpoints** — this step is the one with the three non-obvious fixes explained fully in Part 6; for now, just know it does three things: tells the *server* to advertise its cellular address (`signal`), tells the *client* it's allowed to actually use a second subflow (`limits set subflows 1 add_addr_accepted 1`), and registers the client's own cellular address as usable (`subflow`).
6. **Self-test** — brings up a real MPTCP transfer and checks, via a live `ip mptcp monitor` capture, that a second subflow genuinely joined, before declaring the testbed ready. If it can't confirm this, it prints diagnostic logs and exits with an error rather than silently continuing.

### 4.4 Run it

```bash
cd ~/mptcp-project
sudo ./testbed_teardown.sh
sudo ./testbed_bringup.sh
```

You are looking for this line at the end:

```
== Step 6: verdict ==
PASS - second subflow joined. Testbed is ready.
```

If you see `FAIL`, do not proceed to experiments — the script will print the client's transfer log and the monitor log so you can see exactly what happened. Common causes are covered in Part 6.

### 4.5 Verify it yourself, independently of the script's own claim

Get in the habit of never trusting a single tool's summary — this project learned that lesson expensively (Part 6). Confirm the testbed three separate ways:

```bash
# 1. Snapshot check — must be run WHILE a transfer is happening (see 6.2 for why timing matters)
sudo ip netns exec server mptcpize run iperf3 -s -1 -D &
sudo ip netns exec client mptcpize run iperf3 -c 10.10.1.2 -t 20 &
sudo ip netns exec client ss -Mai   # run this a few seconds in, while the transfer is still running
# expect: subflows:2 subflows_max:2

# 2. Kernel event log — the authoritative signal
sudo ip netns exec client ip mptcp monitor
# expect to see, in order:
#   [ CREATED] ... [ ESTABLISHED] ...       <- first subflow (Wi-Fi)
#   [ ANNOUNCED] ... daddr4=10.10.2.2 ...   <- server signaled its cellular address
#   [SF_ESTABLISHED] ... saddr4=10.10.2.1 daddr4=10.10.2.2 ...   <- second subflow joined

# 3. Packet capture — the strongest evidence
sudo ip netns exec client tcpdump -i c-cell -n -c 10
# expect to see an MP_JOIN handshake: SYN with "mptcp ... join id 1 token ..." followed by a matching SYN-ACK and ACK
```

If all three agree, your testbed is genuinely working, not just self-reportedly working.

---

## Part 5: Hands-On — Run the Baseline Experiments

### 5.1 What "baseline" means here

Before building any controller, you need a trustworthy answer to: *under a given asymmetry, how does stock (unmodified) MPTCP compare to plain single-path TCP?* That comparison is the baseline. Everything the controller does later will be judged against it.

### 5.2 `netem_shaping.sh` — the scenario library

This script applies `tc`/`netem` shaping to **both ends of both veth pairs** (remember: netem is egress-only). It defines named scenarios:

| Scenario | Wi-Fi leg | Cellular leg |
|---|---|---|
| `baseline-symmetric` | 5 ms delay, 1 ms jitter, 1% loss, 100 Mbit | 25 ms, 2 ms, 0.1%, 20 Mbit |
| `asymmetric-cell-degraded` | 5 ms, 1 ms, 1%, 100 Mbit | 90 ms, 15 ms, 6%, 3 Mbit |
| `asymmetric-wifi-lossy` | 5 ms, 3 ms, 8%, 100 Mbit | 25 ms, 2 ms, 0.1%, 20 Mbit |

Use it standalone to sanity-check what's applied at any time:

```bash
source ./netem_shaping.sh
set_scenario asymmetric-cell-degraded
show_scenario     # always look at this before trusting a result — verifies the shaping actually took
clear_scenario
```

### 5.3 `week4_baseline.sh` — the orchestrator

For each repetition, this script: rebuilds the testbed from scratch, applies the chosen scenario, then runs **one MPTCP transfer and one single-path TCP transfer back-to-back**, capturing:
- the `iperf3 -J` JSON result for each,
- a live `ss -Mai` poll log for the MPTCP run,
- a full `ip mptcp monitor` capture for the MPTCP run (the authoritative subflow-join signal).

```bash
sudo ./week4_baseline.sh baseline-symmetric 10 15
#                        ^scenario          ^reps ^duration(s) per rep
```

### 5.4 `parse_baseline.py` — turning raw runs into a result

This script does two things, and the second one is the single most important piece of engineering in this whole project:

1. Computes mean goodput ± 95% confidence interval for each condition (`mean ± 1.96 × stddev/√n`).
2. **Refuses to count a repetition's MPTCP result unless that repetition's `ip mptcp monitor` log actually shows `SF_ESTABLISHED`** — i.e., unless a second subflow is *proven*, not assumed, to have joined for that specific run.

```bash
python3 parse_baseline.py results/baseline-symmetric_<timestamp>
```

Expected output includes a per-repetition OK/FAIL line and then the comparison:

```
== Dual-subflow validation per MPTCP rep (monitor-log authoritative) ==
  rep1: OK   SF_ESTABLISHED seen (1x)
  ...

Stock MPTCP (dual-path confirmed) goodput:   32.10 Mbps  (95% CI +/- 1.45, n=10)
Single-path TCP                  goodput:   16.87 Mbps  (95% CI +/- 1.42, n=10)

=> Non-overlapping CIs: stock MPTCP goodput is significantly ABOVE single-path TCP here.
```

**Why this matters enough to be its own step:** an earlier version of this pipeline did *not* have this gate, and it silently produced a "significant" result from data where the MPTCP arm never actually used two paths. Part 6 walks through exactly how that happened and how it was caught — read it before you trust any result this pipeline produces, including future ones you generate yourself.

---

## Part 6: The Debugging Story — Lessons Worth Internalizing

This section is arguably more valuable than any script in this project. It is a real account of how the Week 3–4 work went wrong twice, was caught both times, and what general principle each failure teaches — principles that apply far beyond MPTCP. Read it fully before you change anything.

### 6.1 Lesson: kernel state that resets is a trap for "it worked when I tried it by hand"

**What happened:** the first working dual-subflow configuration was found by typing commands into a live shell. It worked. Then, running the automated experiment pipeline, every result showed only one subflow — the fix had silently stopped working.

**Root cause:** `testbed_teardown.sh` deletes the namespaces, and `week4_baseline.sh` calls teardown then bring-up before *every single* repetition. Anything configured by hand in a shell (subflow limits, endpoint registrations) lives inside a namespace and is destroyed the moment that namespace is deleted. The fix has to be *inside* the script that runs every time, not something you do once and assume persists.

**General principle:** if a system can be rebuilt from scratch automatically, any manual fix that isn't captured in the rebuild logic will silently disappear the next time the system rebuilds. Always ask "does this survive a full rebuild?" before considering a fix done.

### 6.2 Lesson: registering something is not the same as advertising it

**What happened:** the client had a local address on the cellular subnet and a registered endpoint for it. The second subflow still never joined.

**Root cause:** in a real deployment, there's normally one server IP reachable via both networks, so the server side of the handshake is automatic. In this two-veth-pair emulation, the server genuinely has *two separate IP addresses* (one per path) — and nothing tells the client that the second one exists unless the server explicitly announces it (`ADD_ADDR`). Registering a local endpoint on the client only solves "I have somewhere to send *from*" — it does nothing about "does the other side know there's somewhere to send *to*."

**General principle:** in any protocol with a discovery/advertisement step, configuring your own side is necessary but not sufficient — check that the *other* side has actually been told what it needs to know.

### 6.3 Lesson: a default of zero is silent, not an error

**What happened:** even with the server correctly advertising its address, the client still wouldn't use a second subflow.

**Root cause:** the kernel's subflow and `add_addr_accepted` limits default to `0`. An announced address that the client isn't allowed to act on produces no error message — it's just quietly ignored. This had to be explicitly raised: `ip mptcp limits set subflows 1 add_addr_accepted 1`.

**General principle:** when something "should be working" and produces no error at all, check for a silently-restrictive default before assuming the visible configuration is wrong.

### 6.4 Lesson: a summary claiming success is not evidence — go one level down

**What happened:** an early baseline run reported statistically "significant" numbers (MPTCP clearly beating single-path TCP, and separately, clearly losing to it in a degraded scenario). Both results looked publishable. Both were wrong.

**Root cause:** the analysis script computed goodput from *any* MPTCP-labeled result file, without ever checking whether a second subflow had actually formed during that run. Directly grepping the raw telemetry across every saved run —

```bash
grep -h "subflows:" results/*/ss_mptcp_rep*.log | grep -o "subflows:[0-9]" | sort | uniq -c
    721 subflows:1
```

— showed that **all 721 sampled lines, across every repetition of every scenario, reported exactly one subflow.** The "MPTCP" arm of the comparison had never actually used two paths; it was single-subflow MPTCP (carrying MPTCP's own protocol overhead) racing against plain TCP on the same single path. Different, far less interesting experiment than the one being claimed.

**General principle:** a script printing "done" or a clean-looking summary table is not evidence anything worked correctly. Before trusting a result, check the *rawest* available signal directly — here, that meant grepping the actual per-repetition telemetry files instead of reading the printed mean.

### 6.5 Lesson: your validation check can itself be wrong — cross-check with an independent method

**What happened:** after fixing 6.1–6.4 and adding a per-repetition dual-subflow gate (`ss -Mai` must show `subflows:2`), roughly **half of all repetitions in both scenarios** started failing that gate — even though the testbed's own bring-up self-test passed reliably. This looked like a serious, unresolved reliability problem with the testbed itself.

**Root cause — and this is the subtle one:** it wasn't the testbed. It was the check. `ss -Mai` takes a **snapshot once per second**. The kernel's own accounting doesn't reliably show `subflows:2` at every single polling instant even while the second subflow is genuinely alive and carrying data — it's a sampling/timing limitation of that specific counter. This was proven by capturing `ip mptcp monitor` (the kernel's own **event-driven** netlink log, not a polled snapshot) on the exact same repetitions: the monitor log showed `SF_ESTABLISHED` on nearly every run the `ss`-based check had called a failure. On one direct comparison batch, the monitor confirmed 5 out of 5 real joins while the polling-based check only caught 2 of 5.

**Fix:** the dual-subflow gate was rewritten to trust the kernel event log (`ip mptcp monitor` → `SF_ESTABLISHED`) as authoritative, keeping the `ss`-based count only as an informational, non-blocking field. Re-running both scenarios at the full sample size with this corrected gate produced the numbers now treated as the project's real Week 4 result (Part 7).

**General principle — the single most important one in this whole project:** *never trust one measurement method on its own, especially a polled/sampled one, for anything you're about to make a conclusion from.* Two independent methods agreeing is real evidence. One method reporting a clean number is not, by itself, evidence of anything. This is why Part 4.5 asks you to verify the testbed three separate ways, not one — that habit is the direct, deliberate outcome of this exact debugging episode.

### 6.6 A smaller but very real lesson: watch for content that wasn't actually said

At two points during this project's development, a block of text appeared in the working conversation formatted to look exactly like an assistant's own earlier reply — including specific technical claims about what had supposedly already been fixed — but it did not match anything that had actually been said earlier. Both times, this was noticed, explicitly flagged, and excluded from being treated as fact; only independently-checkable terminal output and the actual files on disk were used from that point forward.

**General principle:** apply the same "verify independently" habit from 6.5 to information itself, not just to measurements. If something is presented as an established fact or a prior decision, and you can't find where it was actually verified, treat it as unverified until you check.

---

## Part 7: Where the Project Stands Today

### 7.1 Week 3 — Testbed Construction: done and independently validated

All validation checklist items are met, each confirmed by more than one method (Part 4.5): `ss -Mai` shows two active subflows; `tshark` confirms `MP_CAPABLE` on both directions; `tcpdump` confirms `MP_JOIN` on the cellular path, saved as evidence (`week3_mptcp_join.pcap`); bring-up is reproducible across repeated teardown/rebuild cycles.

### 7.2 Week 4 — Baseline Experiments: done, on the third attempt

The final, fully validated result — 10 of 10 repetitions confirmed via the kernel-event-log gate (Part 6.5) in both scenarios:

| Scenario | Stock MPTCP | Single-path TCP | Result |
|---|---|---|---|
| `baseline-symmetric` (healthy paths) | 32.10 Mbps (±1.45) | 16.87 Mbps (±1.42) | MPTCP significantly **better** |
| `asymmetric-cell-degraded` (90 ms, 6% loss, 3 Mbit) | 13.05 Mbps (±2.28) | 17.00 Mbps (±1.02) | MPTCP significantly **worse** — the harmful region from Part 1.4 is reproduced, on trustworthy data |

This satisfies the project's own validation requirement ("at least one asymmetric case shows stock MPTCP at/below single-path TCP goodput") and gives Phase 3 something real to improve on.

**If you pull raw data from the results backup archive:** it contains more than these two folders — several earlier, superseded attempts (including the invalid 721-subflows:1 batch from Part 6.4) were kept for traceability. Only use the two run folders timestamped `20260811_202659` and `20260811_203335` — those are the ones collected after every fix in Part 6 was in place. If you're not sure whether a folder predates the fixes, check whether its `baseline_results.csv` has 8 columns (post-fix, includes `monitor_sf_established`) or 6 (pre-fix) — see Part 10 for the exact check.

### 7.3 File inventory (what's actually on disk, and what each does)

| File | Purpose |
|---|---|
| `testbed_bringup.sh` | Builds the two-namespace, two-veth-pair testbed; applies all 3 MPTCP fixes; self-tests via kernel event log |
| `testbed_teardown.sh` | Deletes both namespaces |
| `netem_shaping.sh` | Path-shaping scenario library (`set`/`clear`/`show`) |
| `week4_baseline.sh` | Orchestrates N repetitions of MPTCP vs single-path TCP under a chosen scenario |
| `parse_baseline.py` | Statistics + the monitor-log-authoritative dual-subflow validation gate |

### 7.4 Weeks 5–10 — Sweep, controller, comparison, and failover: complete

The full stock and controller-active evaluations use the same bounded 54-cell grid with 10 repetitions per cell. The stock sweep produced 8 HARMFUL grid cells; the controller-active sweep produced 4 HARMFUL grid cells. Across the 8 stock-harmful cells, controller cell means are higher in all 8 cases; under the project's non-overlapping-95%-CI rule, 4 cells became statistically indistinguishable from single-path performance, 3 remained PARTIAL, and 1 remained HARMFUL. Mean recovery across the 8 stock-harmful cells is 0.43 (43.1% of the stock-to-single-path gap).

The live Wi-Fi failover demonstration was completed with the controller using staleness-based failure detection. The controller implementation and the known 23 PM-netlink errors from the controller sweep are preserved in the repository evidence for traceability.

---

## Part 8: How to Continue: Weeks 11–12

Weeks 5–10 are complete. The current task is reporting, presentation preparation, and the final live demonstration. The detailed Week 5 sweep and userspace-PM work that originally lived in this section has already been completed and is represented by the repository scripts and evidence.

### 8.1 Final report

Use the repository evidence rather than rerunning experiments. The main quantitative story is:

- Stock characterization: 54 grid cells, 10 repetitions per cell; 8/54 grid cells were classified HARMFUL.
- Controller-active characterization: the same 54 cells and 10 repetitions per cell; 4/54 were classified HARMFUL.
- Across the 8 stock-harmful cells, controller goodput was higher in all 8 cell means; 4 became statistically indistinguishable from the single-path baseline, 3 were partially recovered, and 1 remained harmful under the project CI rule.
- Mean recovery across those 8 stock-harmful cells was 0.43 (43.1% of the stock-to-single-path gap).
- The controller sweep recorded 23 PM-netlink errors; this caveat is retained in the evidence, and a sensitivity analysis was performed.

The report should present the validation method, harmful-region characterization, controller design, three-way comparison, failover behavior, limitations, and the PM-error caveat.

### 8.2 Final presentation

A simple presentation flow is:

1. Problem and motivation
2. MPTCP and heterogeneous paths
3. Testbed and validation method
4. Stock harmful-region results
5. Controller design and userspace PM control
6. Controller vs. stock results
7. Wi-Fi-to-cellular failover demonstration
8. Limitations and lessons learned
9. Conclusion

### 8.3 Reproduction rule

For final verification, prefer parsing the existing sweep outputs and running the code self-tests. Do not rerun the full 560-repetition sweeps unless a new experiment is explicitly required. The README and `QUICKSTART.md` contain the current Phase 3–4 command sequence.

---

## Part 9: Glossary

| Term | Plain-language meaning |
|---|---|
| Subflow | One physical-path TCP connection inside a larger MPTCP connection |
| MP_CAPABLE / MP_JOIN | Handshake signals for "first subflow" / "additional subflow" |
| ADD_ADDR | "I have another address you could connect to" |
| Path Manager (PM) | The part of the system deciding which subflows to create; can be in-kernel or userspace |
| Head-of-line (HoL) blocking | Fast-path data waiting in a buffer for slow-path data to catch up, because delivery must stay in order |
| Goodput | Real, usable application throughput (excludes retransmissions/overhead) |
| Namespace | An isolated, private network stack — used here to emulate two separate hosts on one machine |
| veth pair | A virtual two-ended network cable connecting two namespaces |
| netem | The Linux tool for injecting artificial delay/loss/jitter/rate limits |
| Confidence Interval (CI) | A range around a measured mean; two conditions are called "significantly different" here only when their 95% CIs don't overlap |
| `ip mptcp monitor` | Live, event-driven kernel log of MPTCP subflow lifecycle events — the authoritative signal in this project (Part 6.5) |

**Further reading, in order of usefulness:**
- RFC 8684 — the MPTCP protocol specification itself.
- Linux kernel documentation for MPTCP (`docs.kernel.org/networking/mptcp.html`).
- The `mptcpd` project documentation, specifically its plugin API and the `MPTCP_PM_CMD_SET_FLAGS` / `MPTCP_PM_CMD_SET_LIMITS` netlink commands — required reading before Week 5's PM-mode switch.

---

## Part 10: Command Cheat Sheet

```bash
# --- Bring the testbed up / down ---
sudo ./testbed_teardown.sh && sudo ./testbed_bringup.sh    # always teardown first — safe, idempotent

# --- Manually verify dual-subflow operation (do this, don't just trust a script's PASS) ---
sudo ip netns exec server mptcpize run iperf3 -s -1 -D &
sudo ip netns exec client mptcpize run iperf3 -c 10.10.1.2 -t 20 &
sudo ip netns exec client ss -Mai                # snapshot check (run mid-transfer)
sudo ip netns exec client ip mptcp monitor       # authoritative event log
sudo ip netns exec client tcpdump -i c-cell -n -c 10   # packet-level proof

# --- Run a scenario ---
source ./netem_shaping.sh && set_scenario asymmetric-cell-degraded && show_scenario
sudo ./week4_baseline.sh asymmetric-cell-degraded 10 15
python3 parse_baseline.py results/asymmetric-cell-degraded_<timestamp>

# --- Check whether a results folder predates the ss-vs-monitor fix (Part 7.2) ---
head -1 results/<folder>/baseline_results.csv | tr ',' '\n' | wc -l
# 8 columns = post-fix (trustworthy) · 6 columns = pre-fix (do not cite)

# --- If dual-subflow join fails, check these three things in order ---
sudo ip netns exec server ip mptcp endpoint show   # server must show the cellular addr with 'signal'
sudo ip netns exec client ip mptcp limits show     # must show add_addr_accepted >=1 and subflows >=1
sudo ip netns exec client ip mptcp endpoint show   # client must show its cellular addr with 'subflow'
```

---

## Part 11: Self-Check Questions

Try answering these from memory before moving on to independent work. If any feel shaky, re-read the linked part.

1. In your own words, why can adding a second network path make a *connection* slower instead of faster? (Part 1.4)
2. What's the difference between registering an endpoint on the client versus signaling one on the server, and why did this project need both? (Part 6.2)
3. Why is `ss -Mai` alone not sufficient to prove a second subflow is active, and what's used instead? (Part 6.5)
4. If you rebuild the testbed and dual-subflow join suddenly stops working, what's the first category of cause you should suspect, based on this project's own history? (Part 6.1, Part 6.3)
5. What exactly does `netem` shape, and why must it be applied on both ends of a veth pair? (Part 3.8)
6. Why does this project deliberately avoid modifying the kernel scheduler, and what does it use instead? (Part 1.5, Part 2)
7. Which two result folders in the backup archive are actually trustworthy, and how would you check a folder you're not sure about? (Part 7.2, Part 10)

If you can answer all seven without looking back, you're ready to run Part 5 yourself and start on Part 8.
