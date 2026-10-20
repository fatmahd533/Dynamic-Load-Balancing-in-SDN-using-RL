# Dynamic Load Balancing in SDN using Reinforcement Learning

## 1. What this project is, in plain words

Think of a computer network as a road system. Data travels in small
chunks (packets) from one computer to another, and between two computers
there are usually **several possible routes**. If everyone takes the same
route, it jams while the others sit empty. **Load balancing** means
spreading traffic across the routes so none of them jams.

In an ordinary network each router decides for itself. In a **Software-
Defined Network (SDN)** the switches are simple and obedient, and one
central program, the **controller**, sees the whole network and tells the
switches which route every flow should take. That makes it possible to
write a smart routing policy in ordinary code.

This project builds a small emulated SDN and asks:

1. **Is a learning algorithm (reinforcement learning) better at choosing
   routes than simple rules?** Answer found: **no. In the tests it was
   trained for, it matches the best simple rule and beats load-blind
   routing, but it never exceeds the simple rule, and it does not carry
   over to eight simultaneous flows or to mixed TCP/UDP traffic** (see
   section 7). The first version of its state, which could not tell *which*
   link was busy, did worse than random choice.
2. **Can the network treat important traffic (latency-sensitive UDP)
   differently from bulk traffic (TCP) without hurting the bulk traffic?**
   Answer: **yes**, and it was verified link by link.
3. **Can an anomaly detector automatically steer traffic away from links
   behaving abnormally, without making things worse?** Answer: not safely at
   first. Without damping it **oscillated** under a sustained flood and made
   one path unreachable 99.4% of the time. Damping (hysteresis and a rule
   never to cut a switch off completely) made it stable in a check run, but
   it still cannot tell a flood from legitimate full-rate traffic, so it is
   not a useful flood detector (see section 7, RQ3).

On top of the experiments there is a **live web dashboard**: pick an
algorithm, pick two hosts, click Run, and watch the controller choose
routes in real time.

This is the same class of problem as traffic engineering in telecom
transport and backhaul networks. It is **not** a 5G, network-slicing or NFV
project.

## 2. Key ideas in two minutes

| Term | Plain meaning |
|---|---|
| **Switch** | A box that forwards packets according to rules it is given. |
| **Controller** | The central program (here: Ryu) that decides routes and installs the rules in the switches. |
| **OpenFlow** | The language the controller uses to talk to the switches. |
| **Flow** | One conversation between two hosts, e.g. one iperf test from h1 to h5. |
| **Path** | The list of switches a flow goes through, e.g. 7 -> 1 -> 5 -> 3 -> 9. |
| **Fat-tree** | A layered network design (core, aggregation, edge) with many equal routes between hosts. |
| **Load balancing** | Spreading flows over the available paths. |
| **TCP / UDP** | TCP = reliable bulk transfer (files). UDP = fast, latency-sensitive (voice, video). |
| **Meter (QoS)** | An OpenFlow rate limiter. Here it caps TCP at 5 Mbps and leaves UDP alone. |
| **Reinforcement learning (RL)** | A program learns by trial and error: it tries actions, gets a reward (good) or penalty (bad), and gradually prefers actions that paid off. |
| **Q-learning** | A simple form of RL that keeps a table of "how good is action A in situation S". Used here instead of deep learning because the network is small. |
| **Epsilon** | How often Q-learning explores a random route instead of its best-known one. It starts high and decays. |
| **Jain's fairness index** | A score from 0 to 1 for how evenly flows share bandwidth. 1.0 = perfectly equal. |
| **Isolation Forest** | An unsupervised anomaly detector: it flags readings that look unlike normal traffic. |
| **Hysteresis** | "Don't react to one bad reading." Require several in a row and wait a cooldown before reacting again. Prevents flapping. |
| **Mininet / Open vSwitch (OVS)** | Software that emulates a whole network of hosts and switches on one machine. |
| **iperf** | A tool that generates test traffic between two hosts and measures throughput. |

## 3. The network we built

A small fat-tree: **10 switches, 8 hosts, every link 10 Mbps.**

```
core:          [5]                    [6]        every core switch connects
              / | \ \               / | \ \      to all 4 aggregation switches
aggregation:  [1] [2]    [3] [4]                 every aggregation switch connects
               | X |      | X |                  to both edge switches of its pod
edge:         [7] [8]    [9] [10]
              h1 h2 h3 h4  h5 h6 h7 h8           2 hosts per edge switch
              \_ pod 1 _/  \_ pod 2 _/
```

- Hosts are `h1`..`h8` with IPs `10.0.0.1`..`10.0.0.8`.
- Host to edge switch: h1,h2 -> 7; h3,h4 -> 8; h5,h6 -> 9; h7,h8 -> 10.
- Two hosts under different edge switches in the same pod (e.g. h1 -> h3)
  have **2** possible paths. Hosts in different pods (e.g. h1 -> h5) have
  **8**. That multi-path choice is what the algorithms compete over.
- The single source of truth for this wiring is `topology.py`
  (`ADJACENCY` and `HOST_LOCATION`). The controller, the experiments and the
  dashboard's Mininet network are all built from it, so they cannot drift
  apart.

## 4. How a flow gets its route

1. A host starts sending (say h1 -> h5). The first packet reaches edge
   switch 7, which has **no rule** for it, so it asks the controller.
2. The controller (`controller.py`) works out the candidate paths from
   switch 7 to switch 9 using the live network graph, and asks the
   **selected algorithm** to pick one (`pick_path()`).
   A flow is decided once: packets that reach other switches before the new
   rules do reuse the same decision (cache, 5 s).
3. The controller installs a rule on every switch along the chosen path.
   From then on the packets of that flow follow it with no further
   controller involvement. Rules expire after 10 s idle.
4. The controller also polls every switch's port counters, which gives each
   link's live traffic rate. Those rates are what the smarter algorithms
   use to judge "congested".
5. If a link goes down (or is excluded by the anomaly detector), the
   controller removes it from the graph, deletes the rules that used it,
   and the next packets are routed around it.

Every decision, anomaly, link up/down and rule eviction is also written as
a structured line to `events/events_<algorithm>_<starttime>.jsonl`, and the
port counters to `stats/*.csv`. The dashboard reads exactly these files.

## 5. The routing algorithms

| Algorithm | How it picks a path |
|---|---|
| **Shortest path** | Always the same first shortest path. Single-path floor. |
| **Random path** | Random among equal-length paths. The control: any algorithm claiming to use network state must beat this, not just single-path. |
| **Round-Robin** | Takes turns among equal-length paths, with one counter **per switch pair**. When no two flows share a pair this behaves like shortest path. |
| **Global round-robin** (`global_rr`) | One counter shared by all flows, so consecutive flows really use different paths. Load-blind. |
| **Least-Congested-Path (LCP)** | Picks the path whose busiest link currently has the lowest traffic. The strong non-learning baseline. |
| **LCP with placement memory** (`lcp_pending`) | LCP plus a short-lived reservation of each new flow's expected load on its path, so flows that start together do not all choose the same path. The best algorithm in the simultaneous-start tests. Q-learning can use the same memory (`SDN_QL_PENDING=1`). |
| **LCP-QoS** | Same as LCP for UDP. TCP additionally avoids any link already carrying UDP. |
| **Q-learning** | Learns from reward. State = (switch, destination, congestion level, traffic class). Chooses hop by hop, explores with decaying epsilon, learns by a multi-step backup along the whole route. Reward is weighted so congestion hurts UDP twice as much as TCP. Two state variants: `busiest` (congestion bucket of the single busiest candidate link, the original) and `per_link` (one bucket per candidate link, so the agent knows *which* link is busy). A trained table can be saved, loaded and frozen (`SDN_QL_SAVE`, `SDN_QL_LOAD`, `SDN_QL_FREEZE`, `SDN_QL_STATE`). The dashboard loads the trained `per_link` table. |

Each algorithm is a separate file in `algorithms/`; the controller picks
one at start-up from the `SDN_ALGORITHM` environment variable.

## 6. The extra layers on top of routing

- **QoS enforcement.** Traffic class is detected from the IP protocol
  (6 = TCP, 17 = UDP). An OpenFlow meter caps TCP at 5 Mbps; UDP is not
  metered. This works the same no matter which algorithm is routing.
- **Failure handling.** The controller listens for port-status events.
  When a link dies it deletes flow rules through it immediately, so recovery
  comes from real rerouting rather than waiting for rules to time out.
  Measured (10 repetitions, ping every 5 ms): a flow on the failed link is
  cut for about 30 ms; first reply after links are restored 30-37 ms.
- **Anomaly-aware exclusion.** An Isolation Forest, trained offline on
  this project's own legitimate traffic (`experiments/train_anomaly_valid.py`),
  scores each link's live rate. A link flagged as anomalous is excluded from
  routing through the same mechanism used for physical failures, and
  re-admitted when it looks normal again. Damping: 3 anomalous readings in a
  row to exclude, 10 normal ones to restore, and a switch is never cut off
  completely. It cannot tell a flood from legitimate full-rate traffic (see
  RQ3 in section 7). The model is **off by default** (`models/anomaly_model.joblib.off`).

## 7. What we found

### Which results can be trusted

An audit found that the experiment network originally had **no link
capacity limit** (105 Mbps passed with no loss), and that the statistics
file contained a few corrupt counters (2^64-1). Every routing comparison
made before that was fixed measured nothing and **is not evidence**. The
fixes: all 24 links are shaped to 10 Mbps, corrupt counters are discarded,
and experiments run through an automated, paired runner (same flows for
every algorithm in a repetition, algorithm order shuffled, fresh
controller each run, static ARP, anomaly detector off).

A second bug was found later: the controller made **dozens of repeat
routing decisions for the same flow in its first second** (every packet
that reached a switch before the new rules did triggered another
decision, overwriting the rules). It is fixed: a flow is now decided once
and repeats reuse that decision (`experiments/patch_dedupe.py`). The
background-load test was re-run after the fix and moved by 2 Mbps or less
(least-congested +14.8 -> +15.0 over random, shortest path -13.1 -> -14.9),
so the earlier conclusions stand; the tables below are all post-fix.

All numbers: mean +/- 95% confidence interval, 15 paired repetitions,
UDP flows of 8 Mbps offered (about 8.39 Mbps measured), 10 Mbps links.
The maximum a six-flow test can deliver is 50.3 Mbps.

### Test 1: six flows started at the same moment

| Algorithm | Delivered (Mbps) | Difference vs random |
|---|---|---|
| **Least-Congested-Path with placement memory (`lcp_pending`)** | 50.39 +/- 0.01 | **+14.2 +/- 2.8** (better in 15/15) |
| Q-learning, per-link state + placement memory (trained, frozen) | 50.39 +/- 0.01 | **+14.2 +/- 2.8** (better in 15/15) |
| Random path | 36.18 +/- 2.83 | baseline |
| Q-learning, per-link state (no memory) | 34.59 +/- 0.88 | -1.6 +/- 2.9 (no difference) |
| Global round-robin | 33.67 +/- 0.78 | -2.5 +/- 2.9 (no difference) |
| Least-Congested-Path (no memory) | 24.95 +/- 3.07 | -11.2 +/- 3.8 (worse in 14/15) |
| Shortest path | 19.44 +/- 0.00 | -16.7 +/- 2.8 (worse in 15/15) |

Plain Least-Congested-Path is **worse than random** here: flows that start
together all see the same idle network (port statistics refresh every
0.5 s), so they all pick the same path. The fix is **placement memory**:
when a flow is routed, its expected load (8 Mbps, 2 s) is reserved on the
links of its path, so the next flow sees them as busy
(`algorithms/pending.py`, `algorithms/lcp_pending.py`). Both algorithms
that use it reach the maximum possible in every run.

### Test 2: one background flow pinned to a path, six flows 2.5 s apart

| Algorithm | Delivered (Mbps) | Difference vs random | Background flow (of 8.39) |
|---|---|---|---|
| Least-Congested-Path | 50.34 +/- 0.00 | +15.0 +/- 1.6 (15/15) | 8.39 |
| `lcp_pending` | 50.34 +/- 0.00 | +15.0 +/- 1.6 (15/15) | 8.39 |
| Q-learning, per-link state | 48.57 +/- 1.68 | +13.2 +/- 1.9 (15/15) | 7.84 |
| Q-learning, per-link state + placement memory | 45.22 +/- 2.43 | +9.8 +/- 3.0 (14/15) | 7.21 |
| Global round-robin | 37.77 +/- 2.43 | +2.4 +/- 2.7 (no difference) | 4.65 |
| Random path | 35.37 +/- 1.63 | baseline | 4.72 |
| Q-learning, busiest-link state | 33.33 +/- 1.55 | -2.0 +/- 2.6 (no difference) | 4.72 |
| Shortest path | 20.47 +/- 3.16 | -14.9 +/- 3.7 (15/15 worse) | 2.39 |

### Test 3: wider conditions

| Algorithm | 8 flows at once (offered 67.1) | 6 flows 1 s apart (offered 50.3) |
|---|---|---|
| `lcp_pending` | **67.24 +/- 0.01** | 49.98 +/- 0.78 |
| Least-Congested-Path | 29.09 +/- 3.96 | **50.34 +/- 0.00** |
| Q-learning, per-link state (trained on 6 flows) | 38.90 +/- 0.01 | 48.62 +/- 1.68 |
| Random path | 41.34 +/- 3.04 | 35.77 +/- 2.19 |
| Shortest path | 19.45 +/- 0.01 | 21.94 +/- 0.23 |

With eight flows the trained agent is **no better than random**
(-2.4 +/- 3.0 Mbps): it was trained on six flows and does not generalise.

Two follow-up checks (15 repetitions each, eight flows at once):

| Agent | Delivered (offered 64.0 Mbps) | vs random (37.78 +/- 2.26) |
|---|---|---|
| Per-link agent **retrained on 8 flows** | 29.17 | **-11.9 +/- 2.8** (15/15 worse) |
| Agent whose state includes the placement memory (`ql_frozen_pending`, trained on 6 flows) | **67.18 +/- 0.01** | **+29.4 +/- 2.3** (15/15 better) |

Retraining the per-link agent on eight flows made it worse: without a
placement memory it sends flows that start together down the same path,
and more training cannot fix that. The agent that sees the placement
memory does carry over from six to eight flows, but it delivers the same
67.18 Mbps as `lcp_pending` in every run: its state contains the
heuristic's own memory, so it reproduces the heuristic rather than
improving on it. The per-link table used by the dashboard is the six-flow
one (`models/ql_per_link.json`; backup `models/ql_per_link_6flows.json`).

### Test 4: mixed TCP and UDP (15 repetitions)

Six cross-pod flows at once: 3 UDP at 8 Mbps and 3 TCP, same seeded flows
for every algorithm. Offered UDP 25.2 Mbps.

| Algorithm | UDP delivered (Mbps) | Total vs random |
|---|---|---|
| `lcp_pending` | **25.19** (0% loss) | **+6.11 +/- 2.41** (14/15 better) |
| Q-learning, per-link state | 20.21 | +0.65 +/- 3.22 (no difference) |
| Random path | 19.10 | reference |
| Least-congested, class-aware | 18.32 | -2.03 +/- 2.93 (no difference) |
| Least-congested | 17.48 | -3.31 +/- 2.63 |
| Shortest path | 16.52 | -5.94 +/- 1.82 |

Only the placement memory helps here. Class-aware routing is again no
better than random. Read the UDP column as the clean result: every TCP
flow is capped by a meter shared per switch, and a few runs lost a TCP
client report (`failed_flows` in `results/gate_runs_mixed.csv`), so the
TCP and total columns are noisier.

### Failure recovery and delay (10 repetitions each, fresh controller)

| Question | Result |
|---|---|
| Single link failure, flow on the failed link | The flow is cut for **28 +/- 6 ms** (least-congested) and **34 +/- 1 ms** (shortest path), measured with a ping every 5 ms. |
| Both uplinks of the source switch down | **0 replies in all 20 runs**: the switch is genuinely isolated and the controller reports no path. |
| Links restored | First reply after 30 to 37 ms. |
| UDP probe next to 3 TCP bulk flows, delay | Ping RTT: Least-Congested-Path **0.30 +/- 0.03 ms**, class-aware version 0.31 +/- 0.03 ms, shortest path **7.57 +/- 0.48 ms**. |
| UDP probe, jitter and loss | Jitter 0.032 / 0.037 / **1.55 ms** (same order); loss 0% for all. |

Class-aware routing is **not measurably better** than plain
Least-Congested-Path in this scenario (the differences are inside the
error bars). Both cut delay about 25 times and jitter about 50 times
compared with shortest-path routing.

### Other results

| Question | Result |
|---|---|
| **RQ2** Does QoS enforcement work? | **Yes.** TCP held at ~5.3 Mbps by an OpenFlow meter, UDP passed at ~8.4 Mbps with 0% loss; the TCP and UDP paths shared a switch but **zero links**. The meter is shared by all TCP flows on a switch, so TCP cannot be used to compare routing algorithms. |
| **RQ3** Is anomaly exclusion safe? | **Not originally; stabilised after a fix, but not a useful flood detector.** The undamped version made path 7 -> 9 unreachable for **29.71 s of 29.88 s (99.4%)** and once partitioned a switch (34% loss). After adding hysteresis (3 anomalous readings to exclude, 10 normal to restore), a rule never to cut a switch off completely, and retraining on valid data, one check run gave 0 'No path' failures, 300/300 pings received and 2 detections with 2 clearances. It still excluded one link during normal traffic: on a 10 Mbps link a flood looks like legitimate line-rate traffic, so a rate-based detector cannot tell them apart. One run, not repeated. The detector is switched off (`models/anomaly_model.joblib.off`) for routing experiments. |
| Decision latency | Mean per `pick_path()` call: RR 0.185 ms, LCP 0.312 ms, LCP-QoS 0.326 ms, Q-learning 0.472 ms. All sub-millisecond. |

### What can and cannot be claimed

- **RQ1. Does RL beat simple rules?** **No.** A trained Q-learning agent
  learns a load-aware policy from scratch and matches the best heuristic
  in the six-flow tests, but it never exceeds it (the heuristic is already
  at the 50.3 Mbps ceiling), it was slightly worse in the background-load
  test, it does not generalise to eight flows (retraining on eight made it
  worse), and in mixed TCP/UDP traffic it is no better than random. Only an
  agent that is given the heuristic's placement memory matches the heuristic,
  at six and at eight flows. Its reward is computed
  from the same load statistics the heuristic reads, so it cannot learn
  more than the heuristic already knows. A real RL win would need an
  outcome-based reward and traffic whose size is unknown in advance.
- **Can:** load-aware multipath routing in SDN that avoids loaded links,
  handles flows starting together once it remembers where it has placed
  flows, reroutes around a failed link in tens of milliseconds, and keeps
  UDP delay and jitter low next to bulk TCP, in a valid, repeatable
  emulated testbed.
- **Cannot:** that RL beats the heuristic; that a flow is moved after it
  has started (it is not); results for real TCP routing comparisons,
  larger topologies or real hardware; that the anomaly detector helps.
- **Limits:** one 10-switch topology; UDP flows of one size; one
  background-flow position; one failure scenario (single and double link);
  one mixed-traffic mix (3 UDP + 3 TCP), with noisy TCP numbers.

### Left open on purpose

- The anomaly layer is damped and cannot partition a switch, but it cannot
  distinguish a flood from legitimate full-rate traffic. The model is
  **off by default** (`models/anomaly_model.joblib.off`). For the anomaly
  demo, train it with `ryu-env/bin/python experiments/train_anomaly_valid.py`
  and rename it to `models/anomaly_model.joblib`; turn it off again for
  routing experiments and the Compare tab.
- One training episode delivered only 1.3 Mbps and was not explained.
- An early n=5 pilot suggested Round-Robin was more consistent; at n=20
  that vanished (small-sample artifact, reported rather than dropped).

### Reproduce

```bash
# from ~/sdn-rl, nothing else running
bash reproduce.sh quick     # ~10 min: checks everything works
bash reproduce.sh full      # ~7 h: the routing, mixed-traffic, failure and QoS experiments
```

The anomaly check (RQ3) is not part of the script. `quick` mode was run
end to end; `full` mode was assembled from experiments that were each run
separately and was not executed in one go from a clean start.

Setup (two Python environments) is in `SETUP.md`. The anomaly model is
moved aside automatically; restore it for the anomaly demo.

## 8. The live dashboard

### What it is

A web page for running an experiment and watching the routing happen.
Primary use is deliberately tiny: **choose algorithm, from host, to host,
max bandwidth, then click Run.** Nothing else is required.

### What Run does

1. Starts the Mininet topology if it is down.
2. Switches the controller to the chosen algorithm, but only if it differs
   from the one running (this restarts Ryu, about 4 s).
3. Starts real iperf UDP traffic from the source to the destination host
   for up to 120 seconds. It keeps flowing until you stop it with the × on its
   chip (or the cap is reached).
4. Filters the whole page to that flow: topology highlight, event feed and
   all four metric charts.

The controller chooses the route; the dashboard only starts traffic and
shows what the controller did.

### Compare and Results tabs

- **Compare** replays the same six UDP flows under up to six algorithms,
  one after another (about 25 s each), and shows delivered Mbps, link load
  on the topology and a one-line verdict. Pick the algorithms, click
  *Run comparison*. The anomaly model must be off
  (`models/anomaly_model.joblib.off`). Checked on the real testbed: Shortest
  path 19.4, Random 34.2, Round-robin 34.2, Least-congested 19.4,
  Least-congested + memory **50.4**, Q-learning 36.3 Mbps. This is a single
  run; the 15-repetition evidence is on the Results tab.
- **Results** shows the repeated experiments with confidence intervals.

### Several experiments at once

You can start more than one, e.g. h1 -> h5 and, while it runs, h3 -> h7.

- Each flow has its own colour on the topology, its own chip with a running timer and a × to stop it.
- The view shows the union of all running flows. Starting a new Run when
  nothing is running starts a fresh view.
- Run is blocked only if **the same host pair** is already running, or if
  you pick a **different algorithm** while anything is running (switching
  restarts the controller, which would break the other flows' routing). A
  hint under the form says which.
- Every test uses its own iperf port so two flows to the same destination
  host do not collide.

### What the panels show

| Panel | Meaning |
|---|---|
| Topology and live path | The fat-tree; each focused flow's latest path in its colour; red dashed = anomalous link, amber dashed = physically down. |
| Event feed | Routing decisions, anomaly detected/cleared, link up/down, flow evictions. |
| Decision latency | How long the controller took per decision. |
| Path distribution | How often each path was chosen (top paths). |
| Link utilization | Each fabric link as a percentage of its 10 Mbps. |
| Link downtime | Percentage of the run each link was unusable (anomaly plus failure). |
| Total network throughput | All links, whole session. Not filtered to one flow, because port counters are not per-flow. |

A collapsed **Advanced** section holds a replay/"go live" browser for past
runs. It is for power users and is not part of the normal workflow.

### Architecture

```
 Browser  (frontend/index.html, React via CDN, port 5173)
    |  HTTP polling (live: every ~1.2 s)
    v
 FastAPI backend  (backend/main.py, port 8000, system Python + its own venv)
    |-- every API call first runs sync_lib.sync():
    |       tails events/*.jsonl and stats/*.csv  ->  dashboard.db (SQLite)
    |-- /api/runs, /api/runs/{id}/events?since=, /api/runs/{id}/stats?since=,
    |   /api/live/current       read from SQLite
    '-- /api/lab/*              proxy to the lab daemon
            |  JSON lines over TCP 127.0.0.1:9191
            v
 Lab daemon  (lab/lab_daemon.py, run with sudo, SYSTEM Python)
    |-- holds one persistent Mininet network (built from topology.py)
    |-- starts/stops iperf on the hosts
    '-- starts/restarts the Ryu subprocess (controller.py + chosen algorithm)
            |  OpenFlow, port 6653
            v
 Ryu controller (ryu-env, Python 3.9)  -- writes events/*.jsonl and stats/*.csv
            ^
            '------ read back by sync_lib.py (the loop closes)
```

Design choice worth knowing: **replay and live mode are the same code
path.** The CLI `ingest.py` and every API call use the same `sync()`, so a
finished run and a running one are read identically.

Daemon commands: `start_topology`, `stop_topology`, `set_algorithm`,
`run_traffic`, `stop_traffic`, `list_traffic`, `status`, `pingall`.

SQLite tables: `runs`, `events`, `stats`, `offsets` (where each file was
last read up to, so syncing is incremental).

---

## 9. Running it

### Why there are two Python environments

Mininet and Ryu cannot share one Python:

| Component | Where it lives | Why |
|---|---|---|
| **Mininet 2.3.0 + Open vSwitch 3.3.4** | System Python 3.12 | Installed system-wide. The `mininet` Python package only exists here. |
| **Ryu 4.34** | `ryu-env/`, Python 3.9 venv | Ryu is unmaintained and breaks on Python 3.10+. Pinned: `setuptools==57.5.0`, `eventlet==0.30.2`. **Do not upgrade them.** |
| **Dashboard backend (FastAPI)** | `dashboard/backend/venv` | Its own venv, system Python. |

They only talk over the network (OpenFlow), never by importing each other,
so the split is fine. **The lab daemon must run under the SYSTEM Python
with sudo, not under `ryu-env`** (it imports `mininet`; under `ryu-env` you
get `ModuleNotFoundError: mininet`). The daemon launches Ryu itself using
`ryu-env/bin/ryu-manager`, so you never start Ryu by hand for the dashboard.

### Start everything with one command

```bash
cd ~/sdn-rl
bash start_dashboard.sh
```

It asks for your sudo password once, clears leftovers, starts the lab
daemon, the backend and the frontend, waits until each answers, and prints
`http://localhost:5173`. **Ctrl+C in that terminal stops everything cleanly.**
If something is stuck, run `bash start_dashboard.sh stop` from another
terminal. Logs are in `dashboard_logs/` (daemon.log, backend.log,
frontend.log).

### Or start the three parts by hand (three terminals)

```bash
# Terminal 1 - lab daemon (privileged, SYSTEM python; owns Mininet, manages Ryu)
cd ~/sdn-rl
sudo mn -c
sudo python3 dashboard/lab/lab_daemon.py
```

```bash
# Terminal 2 - backend API
cd ~/sdn-rl/dashboard/backend
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

```bash
# Terminal 3 - frontend
cd ~/sdn-rl/dashboard/frontend
python3 -m http.server 5173
```

Open **http://localhost:5173**, pick algorithm / from / to / bandwidth, click
**Run**. Stop with Ctrl-C in Terminal 1 (it tears down Mininet and Ryu).

Always run `sudo mn -c` before a fresh daemon launch to clear leftover
Mininet state.

### Quick health checks

- Daemon alive: `curl http://localhost:8000/api/lab/status` returns JSON.
- Topology correct: with the topology up and a controller attached,
  Mininet's `pingAll()` should report 0% dropped. (With no controller a
  100% drop is expected, not a bug.)

### If something goes wrong

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError: mininet` | Daemon was started under `ryu-env`. Use system `sudo python3`. |
| Page says "Can't reach the API" | Terminal 2 (backend) is not running. |
| "Lab daemon not reachable" (HTTP 503) | Terminal 1 (daemon) is not running. |
| Odd Mininet errors on start | Run `sudo mn -c`, then restart the daemon. |
| "Address already in use" when starting the daemon | An old daemon holds port 9191. Run `sudo pkill -f lab_daemon.py ; sudo mn -c`. The current daemon exits cleanly on Ctrl+C and answers status within 0.3 s even during long operations (`busy: true`). An old copy from before this fix ignores the normal stop signal: use `sudo kill -9 <pid>` once (`sudo ss -ltnp \| grep 9191` shows the pid). |
| Run button greyed with a hint | Same host pair already running, or a different algorithm is in use while a test runs. Stop the running flow with the × on its chip. |
| Charts show old data | Focus filter is on a previous flow. Click Run again, or use Advanced to browse runs. |

### Rules that bit us once (do not reintroduce)

- **Ryu import quirk:** `ryu-manager` resets `sys.path` between module-level
  imports and `__init__`. Anything imported *inside* `__init__` must be
  loaded with `importlib.util.spec_from_file_location` + `exec_module`,
  never a plain `import`.
- Database: OVS reports "counter unsupported" as 2^64-1, which overflows
  SQLite. `sync_lib.py` clamps it to 0 (`_safe_int`).
- Decision events carry `src_ip`; the dashboard needs it to attribute a
  decision to the right host.
- When editing the dashboard frontend, **read the actual file first**. During
  development, half-built code turned up that had not been written in that
  step. Do not trust memory of what is "supposed" to be there.

---

## 10. Repository layout

```
sdn-rl/
|-- README.md
|-- topology.py            # ADJACENCY + HOST_LOCATION; live link up/down state
|-- controller.py          # Ryu app: routing, QoS meters, failure handling,
|                          #   anomaly exclusion, structured event output
|-- algorithms/            # one file per path-selection algorithm
|                          #   (incl. q_learning.py, least_congested_qos.py,
|                          #   random_path.py, global_rr.py, lcp_pending.py,
|                          #   pending.py)
|-- experiments/           # gate_matrix.py, gate_bg.py (background-load test),
|                          #   gate_mixed.py (mixed TCP/UDP test),
|                          #   repeat_extra2.py (failure + QoS repeats),
|                          #   patch_dedupe.py, train_ql.py, summarize_gate.py
|-- reproduce.sh           # bash reproduce.sh quick|full
|-- start_dashboard.sh     # one command: daemon + backend + frontend
|-- SETUP.md, requirements-ryu.txt, requirements-system.txt
|-- models/                # trained Q-tables (ql_per_link.json, ql_busiest.json),
|                          #   anomaly model (currently .off)
|-- results/               # CSVs from the experiments above
|-- events/                # events_<algorithm>_<starttime>.jsonl  (controller output)
|-- stats/                 # port-stat CSVs                       (controller output)
|-- ryu-env/               # Python 3.9 venv for Ryu (pinned, do not upgrade)
|-- dashboard_lab_ryu.log  # Ryu's log when started by the lab daemon
'-- dashboard/
    |-- backend/           # FastAPI: main.py, sync_lib.py, ingest.py,
    |                      #   dashboard.db, venv/
    |-- lab/lab_daemon.py  # privileged Mininet + Ryu + traffic manager
    '-- frontend/index.html
```

Experiment scripts and the trained anomaly model live alongside these;
see the build log below for what each experiment measured.

---

# Appendix - original planning notes and detailed build log

> **Note:** the fairness numbers and "no algorithm differentiates" findings
> in this log (Steps 1-2b) were measured on a network whose links had no
> capacity limit and are **superseded by section 7**. They are kept as
> history. The QoS, failure-recovery and latency measurements do not depend
> on link capacity and remain valid.

Everything below is the project's original README, kept for the record
(headings demoted one level). The experiments in it are finished, the
write-up is done, and the dashboard in section 8 was built afterwards.

QoS-aware dynamic load balancing in SDN. Comparing a Q-learning controller
against strong non-RL baselines, and testing whether QoS-aware routing
protects latency-sensitive traffic without hurting bulk traffic.

### Research Questions

(Section 7 adds a third question, RQ3, on anomaly-aware exclusion.)

1. Does RL-based load balancing beat simple heuristics — and where
   specifically (congestion, bursts, failures) does that advantage show
   up or disappear?
2. Does QoS-aware routing protect latency-sensitive traffic without
   significantly hurting bulk traffic, vs. class-agnostic routing?

### Algorithms Compared

- **Round-Robin** — naive floor baseline
- **Least-Congested-Path** — strong heuristic baseline
- **Q-Learning** — the project's contribution (tabular, NOT deep RL —
  DQN/PPO deliberately out of scope, overkill at this network size)

### Topology

Small fat-tree: 10 switches / 8 hosts. Chosen to give genuine multi-path
routing (enough real choice for algorithms to differ) while staying fast
to iterate on.

Hardware: i5-1235U, 12 threads, 11GB RAM, Ubuntu 6.8 kernel — confirmed
comfortable for this topology size.

### Build Order

1. **Core network + baselines** — topology, Ryu/Mininet wiring,
   Round-Robin + Least-Congested-Path controllers, clean stats collection.
   *Milestone: any baseline runs, clean CSVs, plots working.*
2. **Q-learning, class-agnostic** — congestion-aware state (not just
   ingress port), relative/multi-metric reward, correct multi-step
   Bellman bootstrapping, time-based (not per-decision) epsilon decay.
   *Milestone (planned): RL beats Least-Congested-Path in a congestion scenario. Outcome: it matches it but does not beat it; see section 7.*
3. **Add 2 traffic classes** — latency-sensitive UDP + bulk TCP, enforced
   via OpenFlow meters or OVS queues.
   *Milestone: classes behave differently under load.*
4. **Make routing class-aware** — QoS-aware reward + QoS-aware baseline
   for fair comparison.
   *Milestone: class-agnostic vs. class-aware comparison, second finding.*
5. **Run experiments** — baseline, congestion (bottleneck link), traffic
   burst, single link failure, overlapping double failure, low-load
   control. Multiple seeded repetitions per scenario/algorithm.
6. **Write-up** — both findings stated honestly, plus limitations (small
   topology, tabular RL, emulation vs. real hardware).

### KPIs

**Must-have:** throughput/utilization, packet loss, latency, recovery
time after failure

**Should-have:** fairness across flows (Jain's index), convergence time,
decision latency, reroute count

### Environment Setup

- **Open vSwitch** 3.3.4 — system-wide
- **Mininet** 2.3.0 — system-wide, runs on system Python 3.12
- **Ryu** 4.34 — isolated in a Python 3.9 venv at `ryu-env/`, because
  vanilla Ryu is unmaintained and breaks on Python 3.10+

  Compatibility pins needed inside the venv (do not remove/upgrade):
  - `setuptools==57.5.0` (newer setuptools removed the `easy_install`
    hook Ryu's old build script needs)
  - `eventlet==0.30.2` (newer eventlet removed `ALREADY_HANDLED`,
    which Ryu 4.34 imports)

Ryu talks to Mininet/OVS over OpenFlow (network protocol), not shared
Python — so the two different Python versions coexisting is fine.

### Activating Ryu's environment

```bash
cd ~/sdn-rl
source ryu-env/bin/activate
```

Deactivate with `deactivate` when done. Mininet commands don't need the
venv active at all.
