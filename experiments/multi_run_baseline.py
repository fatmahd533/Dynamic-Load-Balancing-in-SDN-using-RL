"""
Runs congestion scenarios repeatedly in a single continuous Mininet
session, capturing achieved throughput each trial. Supports two modes:

  --flows 2 (default): the original 2-flow same-pod scenario. h1->h3
  is the congesting flow, h2->h4 is measured. Only 2 candidate paths
  exist between this switch pair (e1<->e2, via a1 or a2), so this is
  the scenario that produced the Step 1/2b "statistically indistinguishable
  at 2 flows" finding -- kept unchanged in behavior from the original
  script (a variable-naming bug that mislabeled h3/h4 as h5/h6 in prints
  is fixed here; the actual hosts used were always correct).

  --flows 3: the Step 2b pigeonhole scenario. Three distinct host-pair
  flows (h1->h3, h2->h4, h1->h4) all traverse the same e1<->e2 switch
  pair, which has only 2 candidate paths -- so by pigeonhole, at least
  one path MUST carry 2 flows, regardless of how intelligent the
  algorithm is. Flows are staggered 4s apart (~2 stats-poll cycles) so
  congestion-aware algorithms get a real port-stats reading before each
  subsequent decision.

Each trial's iperf calls use fresh ephemeral source ports per host pair,
so every trial naturally gets its own flow entries and independent path
decisions -- no need to wait for old flows to idle-timeout between trials
beyond the cooldown gap already built in.

Run once per algorithm (set SDN_ALGORITHM in the controller's terminal
before starting it, matching what you pass here with --algorithm):

    sudo python3 experiments/multi_run_baseline.py --algorithm round_robin --trials 5
    sudo python3 experiments/multi_run_baseline.py --algorithm least_congested --trials 5
    sudo python3 experiments/multi_run_baseline.py --algorithm least_congested_qos --trials 5
    sudo python3 experiments/multi_run_baseline.py --algorithm q_learning --trials 5

    sudo python3 experiments/multi_run_baseline.py --algorithm round_robin --trials 5 --flows 3
    sudo python3 experiments/multi_run_baseline.py --algorithm least_congested --trials 5 --flows 3
    sudo python3 experiments/multi_run_baseline.py --algorithm least_congested_qos --trials 5 --flows 3
    sudo python3 experiments/multi_run_baseline.py --algorithm q_learning --trials 5 --flows 3

--flows 2 appends to results/baseline_comparison.csv (unchanged schema).
--flows 3 appends to results/congestion_3flow_comparison.csv.
"""

import sys
import os
import re
import csv
import time
import argparse

sys.path.insert(0, '.')

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel

from fattree_topo import FatTreeTopo

RESULTS_DIR = 'results'
RESULTS_FILE_2FLOW = os.path.join(RESULTS_DIR, 'baseline_comparison.csv')
RESULTS_FILE_3FLOW = os.path.join(RESULTS_DIR, 'congestion_3flow_comparison.csv')

STAGGER_SECONDS = 4   # gap between flow starts, ~2 stats-poll cycles (STATS_POLL_INTERVAL=2 in controller.py)
MEASURE_SECONDS = 10  # how long the final flow (the measured one) runs, concurrent with the others


def parse_throughput_mbps(iperf_output):
    """Extract client-side throughput, in Mbps, from iperf's text output.
    iperf reports in whatever unit fits the magnitude -- Kbits/sec when
    throughput drops below ~1 Mbit (common for a flow that's being
    heavily squeezed by congestion, exactly the case this experiment is
    designed to produce), Mbits/sec at normal rates, Gbits/sec on very
    fast links. The original version only matched Mbits/sec, so any
    heavily-squeezed flow silently parsed as None instead of a small
    positive number -- which corrupted downstream fairness-index
    calculations rather than just leaving a blank cell (see
    jains_fairness_index: it drops None values instead of treating them
    as very-low-but-real throughput, so a dropped value falsely makes
    the remaining flows look more evenly matched than they actually were)."""
    matches = re.findall(r'([\d.]+)\s*([KMG])bits/sec', iperf_output)
    if not matches:
        return None
    value, unit = matches[-1]
    value = float(value)
    if unit == 'K':
        return value / 1000.0
    elif unit == 'G':
        return value * 1000.0
    return value  # 'M'


def jains_fairness_index(values):
    """Standard Jain's fairness index: (sum(x))^2 / (n * sum(x^2)).
    1.0 = perfectly fair (all flows got equal throughput); lower values
    indicate some flows were squeezed much harder than others. None
    values (failed throughput parses) are dropped before computing --
    with the Kbits/sec fix above, this should now only trigger on a
    genuine parse failure (e.g. a connection that never established),
    not on legitimately-low-but-real throughput."""
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    n = len(valid)
    return (sum(valid) ** 2) / (n * sum(v ** 2 for v in valid))


def run_2flow_trial(h1, h2, bw, writer, f, algorithm, trial):
    print(f"\n=== Trial {trial} (2-flow) ===")

    print("  Starting congesting flow h1 -> h3 (target %.1fMbps, 15s)..." % bw)
    h1.cmd(f'iperf -c 10.0.0.3 -t 15 -b {bw}M > /tmp/h1_iperf.log &')

    print(f"  Waiting {STAGGER_SECONDS + 2}s for load to register...")
    time.sleep(STAGGER_SECONDS + 2)

    print("  Measuring h2 -> h4 (target %.1fMbps, 8s)..." % bw)
    result = h2.cmd(f'iperf -c 10.0.0.4 -t 8 -b {bw}M')
    throughput = parse_throughput_mbps(result)
    print(f"  h2 achieved: {throughput} Mbps")

    writer.writerow([algorithm, trial, throughput, time.time()])
    f.flush()

    print("  Waiting for h1's flow to finish, then a short gap...")
    time.sleep(10)


def run_3flow_trial(h1, h2, bw, writer, f, algorithm, trial):
    print(f"\n=== Trial {trial} (3-flow pigeonhole) ===")

    duration_a = STAGGER_SECONDS * 2 + MEASURE_SECONDS + 5
    print(f"  Starting flow A: h1 -> h3 (target {bw}Mbps, {duration_a}s)...")
    h1.cmd(f'iperf -c 10.0.0.3 -t {duration_a} -b {bw}M > /tmp/flowA_iperf.log &')

    print(f"  Waiting {STAGGER_SECONDS}s for load to register...")
    time.sleep(STAGGER_SECONDS)

    duration_b = STAGGER_SECONDS + MEASURE_SECONDS + 5
    print(f"  Starting flow B: h2 -> h4 (target {bw}Mbps, {duration_b}s)...")
    h2.cmd(f'iperf -c 10.0.0.4 -t {duration_b} -b {bw}M > /tmp/flowB_iperf.log &')

    print(f"  Waiting {STAGGER_SECONDS}s for load to register...")
    time.sleep(STAGGER_SECONDS)

    print(f"  Starting flow C: h1 -> h4 (target {bw}Mbps, {MEASURE_SECONDS}s) -- forces collision...")
    result_c = h1.cmd(f'iperf -c 10.0.0.4 -t {MEASURE_SECONDS} -b {bw}M')
    throughput_c = parse_throughput_mbps(result_c)
    print(f"  Flow C achieved: {throughput_c} Mbps")

    print("  Waiting for flows A and B to finish...")
    time.sleep(6)

    log_a = h1.cmd('cat /tmp/flowA_iperf.log')
    log_b = h2.cmd('cat /tmp/flowB_iperf.log')
    throughput_a = parse_throughput_mbps(log_a)
    throughput_b = parse_throughput_mbps(log_b)
    print(f"  Flow A achieved: {throughput_a} Mbps")
    print(f"  Flow B achieved: {throughput_b} Mbps")

    fairness = jains_fairness_index([throughput_a, throughput_b, throughput_c])
    print(f"  Jain's fairness index (A,B,C): {fairness}")

    writer.writerow([algorithm, trial, throughput_a, throughput_b, throughput_c,
                      fairness, time.time()])
    f.flush()

    print("  Cooldown before next trial...")
    time.sleep(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm', required=True,
                         help='Label for these results -- must match the '
                              'SDN_ALGORITHM the controller is currently running')
    parser.add_argument('--trials', type=int, default=5)
    parser.add_argument('--bandwidth', type=float, default=8.0,
                         help='Target Mbps per flow (default 8)')
    parser.add_argument('--flows', type=int, choices=[2, 3], default=2,
                         help='2 = original congesting/measured pair. '
                              '3 = pigeonhole scenario forcing a collision '
                              'on a 2-path switch pair (Step 2b resolution).')
    args = parser.parse_args()
    bw = args.bandwidth

    setLogLevel('info')
    os.makedirs(RESULTS_DIR, exist_ok=True)

    results_file = RESULTS_FILE_3FLOW if args.flows == 3 else RESULTS_FILE_2FLOW
    write_header = not os.path.exists(results_file)

    topo = FatTreeTopo()
    net = Mininet(topo=topo, link=TCLink,
                   controller=lambda name: RemoteController(
                       name, ip='127.0.0.1', port=6633))
    net.start()

    print("\n=== Waiting for controller to install initial table-miss rules ===")
    time.sleep(2)

    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

    print("\n=== Resolving ARP ===")
    h1.cmd('ping -c1 10.0.0.3')
    h2.cmd('ping -c1 10.0.0.4')
    if args.flows == 3:
        h1.cmd('ping -c1 10.0.0.4')
    time.sleep(1)

    print("\n=== Starting iperf servers on h3, h4 (persist across all trials) ===")
    h3.cmd('iperf -s &')
    h4.cmd('iperf -s &')
    time.sleep(1)

    with open(results_file, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            if args.flows == 3:
                writer.writerow(['algorithm', 'trial', 'flowA_mbps', 'flowB_mbps',
                                  'flowC_mbps', 'fairness_index', 'timestamp'])
            else:
                writer.writerow(['algorithm', 'trial', 'h2_throughput_mbps', 'timestamp'])

        for trial in range(1, args.trials + 1):
            if args.flows == 3:
                run_3flow_trial(h1, h2, bw, writer, f, args.algorithm, trial)
            else:
                run_2flow_trial(h1, h2, bw, writer, f, args.algorithm, trial)

    print("\n=== Cleaning up ===")
    h1.cmd('kill %iperf')
    h2.cmd('kill %iperf')
    h3.cmd('kill %iperf')
    h4.cmd('kill %iperf')
    net.stop()

    print(f"\nResults appended to {results_file}")


if __name__ == '__main__':
    main()
