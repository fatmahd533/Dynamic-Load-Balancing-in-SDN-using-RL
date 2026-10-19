"""
Q-learning convergence test (Step 5/6): the load-bearing test the
project has never actually run. Every prior Q-learning test used a
single run lasting 10-20 seconds -- with EPS_DECAY_RATE=0.01
(wall-clock based, algorithms/q_learning.py), epsilon needs roughly
8 minutes of continuous runtime to approach its floor (EPS_MIN=0.05).
Nothing so far has come close, so whether the agent can learn a stable,
useful policy -- as opposed to just "run without crashing" -- has never
actually been tested.

Two phases:
  1. TRAINING (default 8 min): diverse traffic across several host
     pairs (cross-pod and same-pod, TCP and UDP), cycling continuously,
     so the Q-table sees many distinct states while epsilon decays.
  2. EVALUATION: repeated flows to two fixed test pairs, back-to-back,
     during the low-epsilon tail end -- if the agent has learned
     something, these repeated near-identical decisions should mostly
     converge to the same path.

This script does NOT do the analysis itself -- it only generates
traffic. Run experiments/analyze_qlearning_log.py afterward against
the controller's captured stdout log and its own
results/q_learning_training_*.csv to see whether it actually converged.

IMPORTANT: start the controller with its output captured first:
    SDN_ALGORITHM=q_learning ryu-manager controller.py 2>&1 | tee ryu_qlearning_convergence.log
Then run this script in a second terminal.

Run with: sudo python3 experiments/q_learning_convergence_test.py
Optional: --train-seconds 480 --eval-reps 15
"""

import sys
import os
import time
import random
import argparse

sys.path.insert(0, '.')

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel

from fattree_topo import FatTreeTopo

# (src_host_name, dst_ip, dst_host_name) -- mix of cross-pod (rich,
# 8-candidate state space) and same-pod (2-candidate) pairs so the
# Q-table sees a genuinely varied set of states, not just one topology
# shape.
TRAINING_PAIRS = [
    ('h1', '10.0.0.5', 'h5'),   # cross-pod
    ('h2', '10.0.0.6', 'h6'),   # cross-pod
    ('h3', '10.0.0.7', 'h7'),   # cross-pod
    ('h1', '10.0.0.3', 'h3'),   # same-pod
    ('h2', '10.0.0.4', 'h4'),   # same-pod
]

# Fixed pairs used for the evaluation tail -- kept separate from (but
# overlapping) the training set so we have a clean, well-visited state
# to check policy consistency on.
EVAL_PAIRS = [
    ('h1', '10.0.0.5', 'h5', 'tcp'),
    ('h2', '10.0.0.6', 'h6', 'udp'),
]


def resolve_arp(net):
    print("\n=== Resolving ARP for all training/eval pairs ===")
    seen = set()
    for src_name, dst_ip, _ in TRAINING_PAIRS:
        key = (src_name, dst_ip)
        if key in seen:
            continue
        seen.add(key)
        net.get(src_name).cmd(f'ping -c1 {dst_ip}')


def run_training(net, train_seconds):
    print(f"\n=== Training phase: {train_seconds}s of diverse traffic ===")
    end_at = time.time() + train_seconds
    iteration = 0
    while time.time() < end_at:
        iteration += 1
        src_name, dst_ip, dst_name = random.choice(TRAINING_PAIRS)
        proto = random.choice(['tcp', 'udp'])
        bw = random.choice([1, 2, 4, 6, 8])
        src = net.get(src_name)
        dst = net.get(dst_name)

        remaining = end_at - time.time()
        if remaining <= 0:
            break
        burst = min(2, max(1, remaining))

        if proto == 'tcp':
            dst.cmd('iperf -s -D 2>/dev/null')
            src.cmd(f'iperf -c {dst_ip} -t {burst} -b {bw}M > /dev/null 2>&1')
        else:
            dst.cmd('iperf -u -s -D 2>/dev/null')
            src.cmd(f'iperf -u -c {dst_ip} -t {burst} -b {bw}M > /dev/null 2>&1')

        if iteration % 20 == 0:
            elapsed = train_seconds - (end_at - time.time())
            print(f"  ...{elapsed:.0f}s / {train_seconds}s elapsed, "
                  f"{iteration} flows fired so far")


def run_evaluation(net, eval_reps):
    print(f"\n=== Evaluation phase: {eval_reps} reps per pair, "
          f"epsilon should now be near its floor ===")
    for src_name, dst_ip, dst_name, proto in EVAL_PAIRS:
        print(f"  {src_name} -> {dst_name} ({proto}), {eval_reps} reps...")
        src = net.get(src_name)
        dst = net.get(dst_name)
        if proto == 'tcp':
            dst.cmd('iperf -s -D 2>/dev/null')
        else:
            dst.cmd('iperf -u -s -D 2>/dev/null')
        for i in range(eval_reps):
            if proto == 'tcp':
                src.cmd(f'iperf -c {dst_ip} -t 1 > /dev/null 2>&1')
            else:
                src.cmd(f'iperf -u -c {dst_ip} -t 1 -b 2M > /dev/null 2>&1')
            time.sleep(1.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--train-seconds', type=int, default=480,
                         help='Training phase duration (default 480 = 8 min, '
                              'chosen so epsilon approaches its floor given '
                              'EPS_DECAY_RATE=0.01 in algorithms/q_learning.py)')
    parser.add_argument('--eval-reps', type=int, default=15)
    args = parser.parse_args()

    setLogLevel('info')

    topo = FatTreeTopo()
    net = Mininet(topo=topo, link=TCLink,
                   controller=lambda name: RemoteController(
                       name, ip='127.0.0.1', port=6633))
    net.start()

    print("\n=== Waiting for controller to install initial table-miss rules ===")
    time.sleep(2)

    resolve_arp(net)
    run_training(net, args.train_seconds)
    run_evaluation(net, args.eval_reps)

    print("\n=== Cleaning up ===")
    for name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'h7', 'h8']:
        net.get(name).cmd('pkill -9 -f iperf 2>/dev/null')
    net.stop()

    print("\nDone. Now run: python3 experiments/analyze_qlearning_log.py "
          "ryu_qlearning_convergence.log")


if __name__ == '__main__':
    main()
