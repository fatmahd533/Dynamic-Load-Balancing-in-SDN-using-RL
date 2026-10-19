"""
Discriminating experiment: does an algorithm AVOID a link that is already
loaded?

Scenario (identical for every algorithm within a repetition):
  * One background UDP flow h1 -> h5 (BG_RATE Mbps) is pinned with static
    OpenFlow rules to ONE path: e1-a1-c1-a3-e3. The controller never routes
    it, so its path is the same for all algorithms. It loads these links in
    the forward direction: e1->a1, a1->c1, c1->a3, a3->e3.
  * It starts BG_LEAD seconds before the test flows so the controller's
    port statistics already show the load.
  * 6 test UDP flows between the other 6 hosts (h2,h3,h4,h6,h7,h8), 3 in
    each direction, distinct sources and destinations, staggered.
  * A load-aware algorithm should steer the test flows away from the
    loaded links; a load-blind one (random, round-robin) cannot.

Metrics per run: delivered Mbps of the 6 test flows, delivered Mbps of the
background flow in the steady window (how much the test flows hurt it),
Jain over test flows.

Run from the project root, as root, nothing else running:
    sudo python3 experiments/gate_bg.py --reps 1 --algorithms least_congested   # smoke test
    sudo python3 experiments/gate_bg.py --reps 15
    python3 experiments/summarize_gate.py --file results/gate_runs_bg.csv --ref random_path
Anomaly detection must be OFF (models/anomaly_model.joblib moved away).
"""

import argparse
import os
import random
import re
import subprocess
import sys
import time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'experiments'))

import gate_matrix as gm  # noqa: E402
from analyze_link_load import link_load_metrics, jain  # noqa: E402

BG_SRC, BG_DST = 'h1', 'h5'
BG_PORT = 5090
BG_LEAD = 5.0
TEST_HOSTS = ['h2', 'h3', 'h4', 'h6', 'h7', 'h8']
# switch name, output port along e1 - a1 - c1 - a3 - e3 (ports from topology.py)
BG_HOPS = [('e1', 3), ('a1', 3), ('c1', 3), ('a3', 1), ('e3', 1)]

INTERVAL = re.compile(
    r'\[\s*\d+\]\s+([\d.]+)-\s*([\d.]+)\s+sec\s+[\d.]+\s+\S+\s+([\d.]+)\s+(\w?)bits/sec')

RUN_FIELDS = gm.RUN_FIELDS + ['bg_delivered_mbps', 'bg_rate_mbps']


def pick_test_flows(seed, n=6):
    rng = random.Random(seed)
    pod0 = [h for h in TEST_HOSTS if gm.POD[h] == 0]
    pod1 = [h for h in TEST_HOSTS if gm.POD[h] == 1]
    for _ in range(100000):
        a, b = rng.sample(pod0, len(pod0)), rng.sample(pod1, len(pod1))
        fwd = list(zip(pod0, b))        # pod0 -> pod1
        rev = list(zip(pod1, a))        # pod1 -> pod0
        flows = fwd + rev
        rng.shuffle(flows)
        return flows[:n]


def pin_background():
    for sw, port in BG_HOPS:
        subprocess.run(
            ['ovs-ofctl', '-O', 'OpenFlow13', 'add-flow', sw,
             f'priority=100,ip,nw_src=10.0.0.1,nw_dst=10.0.0.5,actions=output:{port}'],
            check=True)


def bg_window_mbps(text, lo, hi):
    """Mean of the server's 1 s interval reports inside [lo, hi] seconds
    after the background flow started."""
    xs = []
    for m in INTERVAL.finditer(text):
        a, b = float(m.group(1)), float(m.group(2))
        if b - a > 1.5:          # the final 0..T summary line
            continue
        if a >= lo and b <= hi:
            xs.append(gm.to_mbps(m.group(3), m.group(4)))
    return round(sum(xs) / len(xs), 3) if xs else None


def run_one(arm, rep, seed, args):
    module, extra_env = gm.arm_spec(arm, args.poll)
    flows = pick_test_flows(seed, args.flows)
    gm.cleanup()
    os.makedirs(args.logdir, exist_ok=True)
    ctrl_log = os.path.join(args.logdir, f'bg_ctrl_{arm}_rep{rep}.log')
    ctrl = gm.start_controller(module, ctrl_log, extra_env)
    net = None
    try:
        gm.wait_controller(ctrl, ctrl_log)
        net, hosts = gm.setup_net(args.bw, args.settle)
        pin_background()

        n = len(flows)
        total = BG_LEAD + (n - 1) * args.stagger + args.duration
        hosts[BG_DST].cmd(f'iperf -u -s -p {BG_PORT} -i 1 > /tmp/bg_srv.txt 2>&1 &')
        ports = {}
        for k, (s, d) in enumerate(flows):
            ports[(s, d)] = gm.BASE_PORT + k
            hosts[d].cmd(f'iperf -u -s -p {ports[(s, d)]} -i 1 '
                         f'> /tmp/gate_srv_{ports[(s, d)]}.txt 2>&1 &')
        time.sleep(1)
        t_bg = time.time()
        hosts[BG_SRC].cmd(f'iperf -u -c {hosts[BG_DST].IP()} -p {BG_PORT} '
                          f'-t {int(total) + 2} -b {args.bg_rate}M > /tmp/bg_cli.txt 2>&1 &')
        time.sleep(BG_LEAD)
        t0 = time.time()
        for k, (s, d) in enumerate(flows):
            wait = t0 + k * args.stagger - time.time()
            if wait > 0:
                time.sleep(wait)
            hosts[s].cmd(f'iperf -u -c {hosts[d].IP()} -p {ports[(s, d)]} '
                         f'-t {args.duration} -b {args.rate}M '
                         f'> /tmp/gate_cli_{ports[(s, d)]}.txt 2>&1 &')
        t_flows_end = t0 + (n - 1) * args.stagger + args.duration
        time.sleep(max(0, t_flows_end + 3 - time.time()))

        flow_rows = []
        for (s, d) in flows:
            r = gm.parse_server(hosts[d].cmd(f'cat /tmp/gate_srv_{ports[(s, d)]}.txt'))
            failed = r is None
            flow_rows.append({'src': s, 'dst': d, 'port': ports[(s, d)],
                              'thr_mbps': 0.0 if failed else r['thr_mbps'],
                              'loss_pct': 100.0 if failed else r['loss_pct'],
                              'jitter_ms': '' if failed else r['jitter_ms'],
                              'failed': int(failed)})
        win_from = t0 + (n - 1) * args.stagger + gm.WARMUP_S
        win_to = t0 + args.duration
        bg_thr = bg_window_mbps(hosts[BG_DST].cmd('cat /tmp/bg_srv.txt'),
                                win_from - t_bg, win_to - t_bg)
        for h in hosts.values():
            h.cmd('pkill -9 -f iperf 2>/dev/null')
    finally:
        if net is not None:
            net.stop()
        ctrl.terminate()
        try:
            ctrl.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ctrl.kill()

    thr = [r['thr_mbps'] for r in flow_rows]
    stats_csv = gm.stats_path_from_log(ctrl_log)
    lm = link_load_metrics(stats_csv, win_from, win_to) if stats_csv else None
    offered = args.rate * n
    delivered = sum(thr)
    row = {
        'algorithm': arm, 'module': module, 'poll_s': args.poll, 'rep': rep,
        'seed': seed, 'n_flows': n, 'rate_mbps': args.rate,
        'duration': args.duration, 'stagger': args.stagger, 'bw': args.bw,
        'flows': ';'.join(f'{s}>{d}' for s, d in flows),
        'offered_mbps': offered, 'agg_delivered_mbps': round(delivered, 3),
        'delivered_ratio': round(delivered / offered, 4),
        'jain_flows': jain(thr),
        'mean_loss_pct': round(sum(r['loss_pct'] for r in flow_rows) / n, 3),
        'failed_flows': sum(r['failed'] for r in flow_rows),
        'stats_csv': stats_csv, 'ctrl_log': ctrl_log,
        't_start': round(t0, 3), 't_end': round(t_flows_end, 3),
        'bg_delivered_mbps': '' if bg_thr is None else bg_thr,
        'bg_rate_mbps': args.bg_rate,
    }
    for k in ('max_link_mean_util', 'max_link_peak_util', 'uplink_jain',
              'n_links_used', 'min_samples', 'bad_samples'):
        row[k] = '' if lm is None or lm[k] is None else lm[k]
    os.makedirs(gm.RESULTS, exist_ok=True)
    gm.append_row(args.runs_csv, RUN_FIELDS, row)
    for fr in flow_rows:
        fr.update(algorithm=arm, rep=rep)
        gm.append_row(args.flows_csv, gm.FLOW_FIELDS, fr)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--algorithms', nargs='+', default=[
        'shortest_path', 'global_rr', 'random_path', 'least_congested',
        'ql_frozen_busiest', 'ql_frozen_per_link'])
    ap.add_argument('--reps', type=int, default=15)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--flows', type=int, default=6)
    ap.add_argument('--rate', type=float, default=8.0)
    ap.add_argument('--bg-rate', dest='bg_rate', type=float, default=8.0)
    ap.add_argument('--duration', type=int, default=25)
    ap.add_argument('--stagger', type=float, default=2.5)
    ap.add_argument('--bw', type=float, default=10)
    ap.add_argument('--settle', type=float, default=4)
    ap.add_argument('--poll', type=float, default=0.5)
    ap.add_argument('--tag', default='bg')
    ap.add_argument('--logdir', default='results/gate_logs')
    ap.add_argument('--allow-anomaly', action='store_true')
    args = ap.parse_args()

    args.runs_csv = os.path.join(gm.RESULTS, f'gate_runs_{args.tag}.csv')
    args.flows_csv = os.path.join(gm.RESULTS, f'gate_flows_{args.tag}.csv')
    if os.getuid() != 0:
        sys.exit("Run with sudo, from the project root.")
    if not os.path.exists('controller.py'):
        sys.exit("Run from the project root (the folder containing controller.py).")
    if args.flows > 6:
        sys.exit("--flows must be <= 6 (the 6 non-background hosts).")
    window = args.duration - (args.flows - 1) * args.stagger - gm.WARMUP_S
    if window < 6:
        sys.exit(f"Steady window only {window:.1f}s; raise --duration.")
    if os.path.exists('models/anomaly_model.joblib') and not args.allow_anomaly:
        sys.exit("Anomaly detection is ON. Run: mv models/anomaly_model.joblib "
                 "models/anomaly_model.joblib.off")
    if not os.path.exists('algorithms/global_rr.py') and 'global_rr' in args.algorithms:
        sys.exit("algorithms/global_rr.py is missing; copy it first.")
    for arm in args.algorithms:
        if arm.startswith('ql_frozen_'):
            t = gm.ARM_TABLES[arm[len('ql_frozen_'):]]
            if not os.path.exists(t):
                sys.exit(f"{arm} needs {t}")

    total = args.reps * len(args.algorithms)
    done = 0
    for rep in range(1, args.reps + 1):
        order = list(args.algorithms)
        random.Random(args.seed * 1000 + rep).shuffle(order)
        for algo in order:
            done += 1
            print(f"[{done}/{total}] rep {rep} {algo} ...", flush=True)
            try:
                r = run_one(algo, rep, args.seed * 1000 + rep, args)
                print(f"    test flows {r['agg_delivered_mbps']}/{r['offered_mbps']} Mbps"
                      f" | background {r['bg_delivered_mbps']}/{args.bg_rate} Mbps",
                      flush=True)
            except Exception as e:
                print(f"    FAILED: {e}", flush=True)
    gm.cleanup()
    print(f"\nDone. Next: python3 experiments/summarize_gate.py --file "
          f"{args.runs_csv} --ref random_path")


if __name__ == '__main__':
    main()
