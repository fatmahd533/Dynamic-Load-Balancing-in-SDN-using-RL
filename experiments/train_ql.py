"""
Trains the Q-learning agent on seeded random UDP scenarios and saves its
Q-table, so it can be evaluated FROZEN (no learning, epsilon 0) against the
baselines with experiments/gate_matrix.py.

One controller process and one Mininet session run for the whole training
(so the agent keeps learning across episodes). Each episode launches N
cross-pod UDP flows chosen from the seed; the stagger between flow starts
is drawn per episode from --staggers, so the agent sees both simultaneous
and spaced arrivals. Training seeds (default 9000+) differ from the
evaluation seeds (1001+).

Run from the project root, as root, nothing else running:

    sudo python3 experiments/train_ql.py --state busiest   # original state
    sudo python3 experiments/train_ql.py --state per_link  # improved state
    sudo python3 experiments/train_ql.py --state pending --flows 6 --staggers 0 0 1  # per-link state + pending load

Writes models/ql_<state>.json and results/ql_train_<state>.csv (one row
per episode: delivered Mbps, Q-table size). Anomaly detection must be OFF.
"""

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), 'experiments'))
import gate_matrix as G  # noqa: E402


def table_size(path):
    try:
        with open(path) as f:
            return len(json.load(f)['q'])
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', required=True, choices=['busiest', 'per_link', 'pending'])
    ap.add_argument('--episodes', type=int, default=40)
    ap.add_argument('--seed', type=int, default=9000)
    ap.add_argument('--flows', type=int, default=4)
    ap.add_argument('--rate', type=float, default=8.0)
    ap.add_argument('--duration', type=int, default=25)
    ap.add_argument('--staggers', type=float, nargs='+', default=[0, 1.25, 2.5])
    ap.add_argument('--bw', type=float, default=10)
    ap.add_argument('--poll', type=float, default=0.5)
    ap.add_argument('--settle', type=float, default=4)
    ap.add_argument('--gap', type=float, default=3, help='idle seconds between episodes')
    ap.add_argument('--resume', action='store_true', help='continue from the existing table')
    ap.add_argument('--allow-anomaly', action='store_true')
    args = ap.parse_args()

    if os.getuid() != 0:
        sys.exit("Run with sudo, from the project root.")
    if not os.path.exists('controller.py'):
        sys.exit("Run from the project root (the folder containing controller.py).")
    if os.path.exists('models/anomaly_model.joblib') and not args.allow_anomaly:
        sys.exit("Anomaly detection is ON. Run: mv models/anomaly_model.joblib "
                 "models/anomaly_model.joblib.off  (or pass --allow-anomaly)")

    table = os.path.join(G.ROOT, G.ARM_TABLES[args.state])
    os.makedirs(os.path.dirname(table), exist_ok=True)
    if args.resume and not os.path.exists(table):
        sys.exit(f"--resume but {table} does not exist")
    if not args.resume and os.path.exists(table):
        os.remove(table)

    env = {'SDN_STATS_POLL': str(args.poll),
           'SDN_QL_STATE': 'per_link' if args.state == 'pending' else args.state,
           'SDN_QL_SAVE': table, 'SDN_QL_SAVE_EVERY': '5',
           'SDN_QL_TIEBREAK': 'random'}
    if args.state == 'pending':
        env['SDN_QL_PENDING'] = '1'
    if args.resume:
        env['SDN_QL_LOAD'] = table

    os.makedirs('results', exist_ok=True)
    log_csv = os.path.join('results', f'ql_train_{args.state}.csv')
    fields = ['episode', 'stagger', 'flows', 'agg_delivered_mbps',
              'elapsed_s', 'q_states']
    G.cleanup()
    os.makedirs('results/gate_logs', exist_ok=True)
    ctrl_log = f'results/gate_logs/ctrl_train_{args.state}.log'
    ctrl = G.start_controller('q_learning', ctrl_log, env)
    net = None
    rng = random.Random(args.seed)
    t_start = time.time()
    try:
        G.wait_controller(ctrl, ctrl_log)
        net, hosts = G.setup_net(args.bw, args.settle)
        for ep in range(1, args.episodes + 1):
            stagger = rng.choice(args.staggers)
            flows = G.pick_flows(args.seed + ep, args.flows)
            _, _, rows = G.launch_and_collect(hosts, flows, args.rate,
                                              args.duration, stagger)
            agg = sum(r['thr_mbps'] for r in rows)
            G.append_row(log_csv, fields, {
                'episode': ep, 'stagger': stagger,
                'flows': ';'.join(f'{s}>{d}' for s, d in flows),
                'agg_delivered_mbps': round(agg, 3),
                'elapsed_s': round(time.time() - t_start, 1),
                'q_states': table_size(table)})
            print(f"episode {ep}/{args.episodes} stagger {stagger}: "
                  f"delivered {agg:.1f} Mbps | Q states {table_size(table)}",
                  flush=True)
            time.sleep(args.gap)
        time.sleep(2)  # let the last periodic save land
    finally:
        if net is not None:
            net.stop()
        ctrl.terminate()
        try:
            ctrl.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ctrl.kill()
        G.cleanup()
    print(f"\nSaved {table} ({table_size(table)} Q-states). "
          f"Per-episode log: {log_csv}")


if __name__ == '__main__':
    main()
