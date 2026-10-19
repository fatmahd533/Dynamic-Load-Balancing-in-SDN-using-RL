"""
Burst scenario (Step 5): 4 flows fired with near-zero stagger onto a
switch pair with only 2 candidate paths -- unlike the earlier 3-flow
pigeonhole test, which staggered each flow's start by ~2 stats-poll
cycles so congestion-aware algorithms had a fresh port-stats reading
before every decision. Here all 4 decisions happen back-to-back,
before the controller's next poll (STATS_POLL_INTERVAL=2s in
controller.py) can reflect any of the other simultaneous flows -- a
harder test of decision quality under a genuine burst, where a "smart"
algorithm has no more real-time information than a naive one at
decision time.

Host pairs: all 4 combinations between {h1, h2} (switch e1) and
{h3, h4} (switch e2). Same 2-path switch pair as the pigeonhole test,
but with 4 flows instead of 3 -- by pigeonhole, at least 2 flows share
each of the 2 paths even under a perfectly balanced split.

A concurrent ping (h1->h3) runs through the same burst window, covering
the packet-loss/latency KPIs alongside throughput fairness.

Run once per algorithm:

    sudo python3 experiments/burst_test.py --algorithm round_robin --trials 5
    sudo python3 experiments/burst_test.py --algorithm least_congested --trials 5
    sudo python3 experiments/burst_test.py --algorithm least_congested_qos --trials 5
    sudo python3 experiments/burst_test.py --algorithm q_learning --trials 5

Appends to results/burst_comparison.csv.
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
RESULTS_FILE = os.path.join(RESULTS_DIR, 'burst_comparison.csv')

BURST_SECONDS = 10
BANDWIDTH_MBPS = 8.0


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
    loss_match = re.search(r'([\d.]+)%\s*packet loss', ping_output)
    loss_pct = float(loss_match.group(1)) if loss_match else None
    rtt_match = re.search(r'=\s*[\d.]+/([\d.]+)/[\d.]+/[\d.]+\s*ms', ping_output)
    avg_rtt = float(rtt_match.group(1)) if rtt_match else None
    return avg_rtt, loss_pct


def jains_fairness_index(values):
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    n = len(valid)
    return (sum(valid) ** 2) / (n * sum(v ** 2 for v in valid))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm', required=True)
    parser.add_argument('--trials', type=int, default=5)
    parser.add_argument('--bandwidth', type=float, default=BANDWIDTH_MBPS)
    args = parser.parse_args()
    bw = args.bandwidth

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

    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

    print("\n=== Resolving ARP for all 4 flow pairs ===")
    h1.cmd('ping -c1 10.0.0.3')
    h1.cmd('ping -c1 10.0.0.4')
    h2.cmd('ping -c1 10.0.0.3')
    h2.cmd('ping -c1 10.0.0.4')
    time.sleep(1)

    print("\n=== Starting iperf servers on h3, h4 (persist across all trials) ===")
    h3.cmd('iperf -s &')
    h4.cmd('iperf -s &')
    time.sleep(1)

    with open(RESULTS_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(['algorithm', 'trial', 'flow_h1h3_mbps',
                              'flow_h1h4_mbps', 'flow_h2h3_mbps',
                              'flow_h2h4_mbps', 'fairness_index',
                              'avg_rtt_ms', 'packet_loss_pct', 'timestamp'])

        for trial in range(1, args.trials + 1):
            print(f"\n=== Trial {trial}/{args.trials} ===")

            h1.cmd('rm -f /tmp/burst_ping.log')
            h1.cmd(f'ping -c {BURST_SECONDS * 5} -i 0.2 10.0.0.3 '
                    f'> /tmp/burst_ping.log 2>&1 &')

            print(f"  Firing all 4 flows near-simultaneously "
                  f"(target {bw}Mbps each, {BURST_SECONDS}s)...")
            h1.cmd(f'iperf -c 10.0.0.3 -t {BURST_SECONDS} -b {bw}M '
                    f'> /tmp/burst_h1h3.log 2>&1 &')
            h1.cmd(f'iperf -c 10.0.0.4 -t {BURST_SECONDS} -b {bw}M '
                    f'> /tmp/burst_h1h4.log 2>&1 &')
            h2.cmd(f'iperf -c 10.0.0.3 -t {BURST_SECONDS} -b {bw}M '
                    f'> /tmp/burst_h2h3.log 2>&1 &')
            h2.cmd(f'iperf -c 10.0.0.4 -t {BURST_SECONDS} -b {bw}M '
                    f'> /tmp/burst_h2h4.log 2>&1 &')

            print(f"  Waiting {BURST_SECONDS + 4}s for all flows and ping to finish...")
            time.sleep(BURST_SECONDS + 4)

            t_h1h3 = parse_throughput_mbps(h1.cmd('cat /tmp/burst_h1h3.log'))
            t_h1h4 = parse_throughput_mbps(h1.cmd('cat /tmp/burst_h1h4.log'))
            t_h2h3 = parse_throughput_mbps(h2.cmd('cat /tmp/burst_h2h3.log'))
            t_h2h4 = parse_throughput_mbps(h2.cmd('cat /tmp/burst_h2h4.log'))
            fairness = jains_fairness_index([t_h1h3, t_h1h4, t_h2h3, t_h2h4])

            ping_output = h1.cmd('cat /tmp/burst_ping.log')
            avg_rtt, loss_pct = parse_ping_summary(ping_output)

            print(f"  h1->h3: {t_h1h3} Mbps | h1->h4: {t_h1h4} Mbps | "
                  f"h2->h3: {t_h2h3} Mbps | h2->h4: {t_h2h4} Mbps")
            print(f"  Fairness: {fairness} | Avg RTT: {avg_rtt} ms | "
                  f"Packet loss: {loss_pct}%")

            writer.writerow([args.algorithm, trial, t_h1h3, t_h1h4, t_h2h3,
                              t_h2h4, fairness, avg_rtt, loss_pct, time.time()])
            f.flush()

            print("  Cooldown before next trial...")
            time.sleep(8)

    print("\n=== Cleaning up ===")
    h1.cmd('pkill -9 -f iperf 2>/dev/null; pkill -9 -f "ping -c" 2>/dev/null')
    h2.cmd('pkill -9 -f iperf 2>/dev/null')
    h3.cmd('pkill -9 -f iperf 2>/dev/null')
    h4.cmd('pkill -9 -f iperf 2>/dev/null')
    net.stop()

    print(f"\nResults appended to {RESULTS_FILE}")


if __name__ == '__main__':
    main()
