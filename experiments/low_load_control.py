"""
Low-load control scenario (Step 5): a single flow, well below link
capacity, with no contention at all -- the reference point every other
Step 5 scenario (congestion, burst, failure) gets compared against.
Without this, "throughput dropped under congestion" has no clean
baseline to drop FROM.

Host pair: h1 <-> h5 (cross-pod, matches failure_recovery_test.py's
topology footprint, 8 candidate shortest paths).

Target rate is well under the 10Mbps link cap AND the 5Mbps TCP meter
(BULK_METER_RATE_KBPS in controller.py), so this measures the network's
best-case behavior, not the meter's enforcement ceiling.

Each trial runs a concurrent ping (latency + packet loss) and iperf
(throughput) over the same window.

Run once per algorithm:

    sudo python3 experiments/low_load_control.py --algorithm round_robin --trials 5
    sudo python3 experiments/low_load_control.py --algorithm least_congested --trials 5
    sudo python3 experiments/low_load_control.py --algorithm least_congested_qos --trials 5
    sudo python3 experiments/low_load_control.py --algorithm q_learning --trials 5

Appends to results/low_load_control.csv.
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
RESULTS_FILE = os.path.join(RESULTS_DIR, 'low_load_control.csv')

TARGET_MBPS = 2.0     # well under the 10Mbps link cap and 5Mbps TCP meter
FLOW_SECONDS = 10


def parse_throughput_mbps(iperf_output):
    matches = re.findall(r'([\d.]+)\s*([KMG])bits/sec', iperf_output)
    if not matches:
        return None
    value, unit = matches[-1]
    value = float(value)
    if unit == 'K':
        return value / 1000.0
    elif unit == 'G':
        return value * 1000.0
    return value


def parse_ping_summary(ping_output):
    """Returns (avg_rtt_ms, packet_loss_pct); either can be None if not
    found (e.g. 100% loss produces no rtt line)."""
    loss_match = re.search(r'([\d.]+)%\s*packet loss', ping_output)
    loss_pct = float(loss_match.group(1)) if loss_match else None
    rtt_match = re.search(r'=\s*[\d.]+/([\d.]+)/[\d.]+/[\d.]+\s*ms', ping_output)
    avg_rtt = float(rtt_match.group(1)) if rtt_match else None
    return avg_rtt, loss_pct


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm', required=True)
    parser.add_argument('--trials', type=int, default=5)
    args = parser.parse_args()

    setLogLevel('info')
    os.makedirs(RESULTS_DIR, exist_ok=True)
    write_header = not os.path.exists(RESULTS_FILE)

    topo = FatTreeTopo()
    net = Mininet(topo=topo, link=TCLink,
                   controller=lambda name: RemoteController(
                       name, ip='127.0.0.1', port=6633))
    net.start()

    print("\n=== Waiting for controller to install initial table-miss rules ===")
    time.sleep(2)

    h1, h5 = net.get('h1', 'h5')

    print("\n=== Resolving ARP ===")
    h1.cmd('ping -c1 10.0.0.5')
    time.sleep(1)

    print("\n=== Starting iperf server on h5 (persists across all trials) ===")
    h5.cmd('iperf -s &')
    time.sleep(1)

    with open(RESULTS_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(['algorithm', 'trial', 'throughput_mbps',
                              'avg_rtt_ms', 'packet_loss_pct', 'timestamp'])

        for trial in range(1, args.trials + 1):
            print(f"\n=== Trial {trial}/{args.trials} ===")

            print(f"  Running concurrent ping and iperf "
                  f"({TARGET_MBPS}Mbps target, {FLOW_SECONDS}s)...")
            h1.cmd('rm -f /tmp/lowload_ping.log')
            h1.cmd(f'ping -c {FLOW_SECONDS * 5} -i 0.2 10.0.0.5 '
                    f'> /tmp/lowload_ping.log 2>&1 &')

            iperf_result = h1.cmd(
                f'iperf -c 10.0.0.5 -t {FLOW_SECONDS} -b {TARGET_MBPS}M')
            throughput = parse_throughput_mbps(iperf_result)

            time.sleep(1)  # let the backgrounded ping fully finish and flush its log
            ping_output = h1.cmd('cat /tmp/lowload_ping.log')
            avg_rtt, loss_pct = parse_ping_summary(ping_output)

            print(f"  Throughput: {throughput} Mbps | "
                  f"Avg RTT: {avg_rtt} ms | Packet loss: {loss_pct}%")

            writer.writerow([args.algorithm, trial, throughput, avg_rtt,
                              loss_pct, time.time()])
            f.flush()

            print("  Cooldown before next trial...")
            time.sleep(3)

    print("\n=== Cleaning up ===")
    h1.cmd('pkill -9 -f iperf 2>/dev/null; pkill -9 -f "ping -c" 2>/dev/null')
    net.stop()

    print(f"\nResults appended to {RESULTS_FILE}")


if __name__ == '__main__':
    main()
