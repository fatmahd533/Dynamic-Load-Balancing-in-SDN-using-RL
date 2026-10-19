"""
Phase-1 "gate" experiment: can this testbed show load balancing at all?

For every (repetition, algorithm) it, fully automatically:
  1. cleans up Mininet, starts a FRESH controller with that algorithm
     (so no round-robin counters / Q-table carry over between runs),
  2. builds the fat-tree with 10 Mbps links and static ARP (so ARP/ICMP
     don't trigger routing decisions),
  3. launches N staggered UDP flows between cross-pod host pairs. The
     pairs are chosen from the seed, and are IDENTICAL for every
     algorithm within a repetition (paired design),
  4. records per-flow receiver throughput/loss (iperf server side) and
     link-load metrics from the controller's stats CSV for the steady
     window in which all flows are active,
  5. appends one row to results/gate_runs.csv and per-flow rows to
     results/gate_flows.csv.
The algorithm order is shuffled per repetition (counter-balanced).

Run from the project root, as root, with NOTHING else running (no
controller in another terminal, no dashboard lab daemon):

    sudo python3 experiments/gate_matrix.py --reps 1 --algorithms shortest_path   # smoke test
    sudo python3 experiments/gate_matrix.py --reps 5                              # the gate run
    python3 experiments/summarize_gate.py

Options: --stagger S (seconds between flow starts, 0 = simultaneous),
--poll P (controller stats polling seconds), --tag NAME (write
results/gate_runs_NAME.csv instead of gate_runs.csv).

Arms (--algorithms): shortest_path, round_robin, least_congested,
least_congested_qos, random_path, q_learning (untrained, original
settings), ql_frozen_busiest / ql_frozen_per_link (trained tables from
experiments/train_ql.py, no learning, epsilon 0).

Anomaly detection must be OFF (models/anomaly_model.joblib moved away),
otherwise the detector excludes links mid-run and confounds the result.
"""

import argparse
import csv
import os
import pwd
import random
import re
import socket
import subprocess
import sys
import time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'experiments'))

from analyze_link_load import link_load_metrics, jain  # noqa: E402

HOSTS = [f'h{i}' for i in range(1, 9)]
POD = {h: (0 if int(h[1:]) <= 4 else 1) for h in HOSTS}
RESULTS = 'results'
ARM_TABLES = {'busiest': os.path.join('models', 'ql_busiest.json'),
              'per_link': os.path.join('models', 'ql_per_link.json'),
              'pending': os.path.join('models', 'ql_pending.json')}
BASE_PORT = 5100
WARMUP_S = 2.5   # stats are 2 s averages; skip the sample straddling the start

SERVER_LINE = re.compile(
    r'\[\s*\d+\]\s+0\.0+-\s*([\d.]+)\s+sec\s+[\d.]+\s+\S+\s+([\d.]+)\s+(\w?)bits/sec'
    r'\s+([\d.]+)\s+ms\s+(\d+)/\s*(\d+)\s+\(([\d.]+)%\)')


def pick_flows(seed, n):
    """n (src, dst) pairs: distinct sources, distinct destinations, each
    pair crossing pods. Deterministic for a given seed."""
    rng = random.Random(seed)
    for _ in range(100000):
        srcs = rng.sample(HOSTS, n)
        dsts = rng.sample(HOSTS, n)
        if all(POD[s] != POD[d] for s, d in zip(srcs, dsts)):
            return list(zip(srcs, dsts))
    raise RuntimeError(f"could not find {n} cross-pod flow pairs")


def to_mbps(value, unit):
    value = float(value)
    return value * {'': 1e-6, 'K': 1e-3, 'M': 1.0, 'G': 1e3}.get(unit, 1.0)


def parse_server(text):
    """Receiver-side summary of one UDP flow, or None if the flow never
    produced a server report (treated as failed = 0 Mbps, 100% loss)."""
    best = None
    for m in SERVER_LINE.finditer(text):
        dur = float(m.group(1))
        if best is None or dur > best[0]:
            best = (dur, m)
    if best is None:
        return None
    m = best[1]
    return {'thr_mbps': to_mbps(m.group(2), m.group(3)),
            'jitter_ms': float(m.group(4)),
            'lost': int(m.group(5)), 'total': int(m.group(6)),
            'loss_pct': float(m.group(7))}


# ---------------------------------------------------------------- plumbing
def own(path):
    """Give files created as root back to the invoking user."""
    uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
    if uid and os.path.exists(path):
        try:
            os.chown(path, int(uid), int(gid))
        except OSError:
            pass


def cleanup():
    subprocess.run(['mn', '-c'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def arm_spec(arm, poll):
    """arm label -> (algorithm module, extra controller environment)."""
    env = {'SDN_STATS_POLL': str(poll)}
    module = arm
    if arm.startswith('ql_frozen_'):
        mode = arm[len('ql_frozen_'):]
        if mode not in ARM_TABLES:
            raise ValueError(f"unknown arm {arm}")
        module = 'q_learning'
        env.update(SDN_QL_LOAD=os.path.join(ROOT, ARM_TABLES[mode]),
                   SDN_QL_FREEZE='1',
                   SDN_QL_STATE='per_link' if mode == 'pending' else mode,
                   SDN_QL_TIEBREAK='random')
        if mode == 'pending':
            env['SDN_QL_PENDING'] = '1'
    return module, env


def start_controller(algo, logpath, extra_env=None):
    uid = int(os.environ.get('SUDO_UID', os.getuid()))
    gid = int(os.environ.get('SUDO_GID', os.getgid()))
    env = dict(os.environ)
    env.update(SDN_ALGORITHM=algo, PYTHONUNBUFFERED='1',
               HOME=pwd.getpwuid(uid).pw_dir)
    env.update(extra_env or {})
    exe = os.path.join(ROOT, 'ryu-env', 'bin', 'ryu-manager')
    kw = dict(user=uid, group=gid) if (os.getuid() == 0 and uid != 0) else {}
    log = open(logpath, 'w')
    proc = subprocess.Popen([exe, 'controller.py'], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT, **kw)
    own(logpath)
    return proc


def wait_controller(proc, logpath, timeout=40):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if proc.poll() is not None:
            tail = open(logpath).read()[-1500:]
            raise RuntimeError("controller exited early:\n" + tail)
        try:
            socket.create_connection(('127.0.0.1', 6633), timeout=1).close()
            time.sleep(1.0)
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("controller never started listening on :6633")


def stats_path_from_log(logpath):
    m = re.search(r'Logging port stats to:\s*(\S+)', open(logpath).read())
    return os.path.join(ROOT, m.group(1)) if m else None


def append_row(path, fields, row):
    new = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)
    own(path)


RUN_FIELDS = ['algorithm', 'module', 'poll_s', 'rep', 'seed', 'n_flows', 'rate_mbps', 'duration',
              'stagger', 'bw', 'flows', 'offered_mbps', 'agg_delivered_mbps',
              'delivered_ratio', 'jain_flows', 'mean_loss_pct', 'failed_flows',
              'max_link_mean_util', 'max_link_peak_util', 'uplink_jain',
              'n_links_used', 'min_samples', 'bad_samples',
              'stats_csv', 'ctrl_log', 't_start', 't_end']
FLOW_FIELDS = ['algorithm', 'rep', 'src', 'dst', 'port', 'thr_mbps',
               'loss_pct', 'jitter_ms', 'failed']


# ---------------------------------------------------------------- helpers
def setup_net(bw, settle):
    """Fat-tree with shaped links and static ARP. Returns (net, hosts)."""
    from mininet.net import Mininet
    from mininet.node import RemoteController
    from mininet.link import TCLink
    from fattree_topo import FatTreeTopo

    net = Mininet(topo=FatTreeTopo(bw=bw), link=TCLink,
                  controller=lambda name: RemoteController(
                      name, ip='127.0.0.1', port=6633))
    net.start()
    time.sleep(settle)
    hosts = {h: net.get(h) for h in HOSTS}
    for a in hosts.values():
        for b in hosts.values():
            if a is not b:
                a.setARP(b.IP(), b.MAC())
    return net, hosts


def launch_and_collect(hosts, flows, rate, duration, stagger):
    """Start one UDP server per flow, launch the flows (staggered), wait
    for them to finish and parse the receiver-side reports.
    Returns (t0, t_flows_end, flow_rows)."""
    ports = {}
    for k, (s, d) in enumerate(flows):
        ports[(s, d)] = BASE_PORT + k
        hosts[d].cmd(f'iperf -u -s -p {ports[(s, d)]} -i 1 '
                     f'> /tmp/gate_srv_{ports[(s, d)]}.txt 2>&1 &')
    time.sleep(1)

    t0 = time.time()
    for k, (s, d) in enumerate(flows):
        wait = t0 + k * stagger - time.time()
        if wait > 0:
            time.sleep(wait)
        hosts[s].cmd(f'iperf -u -c {hosts[d].IP()} -p {ports[(s, d)]} '
                     f'-t {duration} -b {rate}M '
                     f'> /tmp/gate_cli_{ports[(s, d)]}.txt 2>&1 &')
    t_flows_end = t0 + (len(flows) - 1) * stagger + duration
    time.sleep(max(0, t_flows_end + 3 - time.time()))

    rows = []
    for (s, d) in flows:
        r = parse_server(hosts[d].cmd(f'cat /tmp/gate_srv_{ports[(s, d)]}.txt'))
        failed = r is None
        rows.append({'src': s, 'dst': d, 'port': ports[(s, d)],
                     'thr_mbps': 0.0 if failed else r['thr_mbps'],
                     'loss_pct': 100.0 if failed else r['loss_pct'],
                     'jitter_ms': '' if failed else r['jitter_ms'],
                     'failed': int(failed)})
    for h in hosts.values():
        h.cmd('pkill -9 -f iperf 2>/dev/null')
    return t0, t_flows_end, rows


# ---------------------------------------------------------------- one run
def run_one(arm, rep, flow_seed, args):
    module, extra_env = arm_spec(arm, args.poll)
    flows = pick_flows(flow_seed, args.flows)
    cleanup()
    os.makedirs(args.logdir, exist_ok=True)
    ctrl_log = os.path.join(args.logdir, f'ctrl_{arm}_rep{rep}.log')
    ctrl = start_controller(module, ctrl_log, extra_env)
    net = None
    try:
        wait_controller(ctrl, ctrl_log)
        net, hosts = setup_net(args.bw, args.settle)
        t0, t_flows_end, flow_rows = launch_and_collect(
            hosts, flows, args.rate, args.duration, args.stagger)
    finally:
        if net is not None:
            net.stop()
        ctrl.terminate()
        try:
            ctrl.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ctrl.kill()

    thr = [r['thr_mbps'] for r in flow_rows]
    stats_csv = stats_path_from_log(ctrl_log)
    win_from = t0 + (len(flows) - 1) * args.stagger + WARMUP_S
    win_to = t0 + args.duration
    lm = link_load_metrics(stats_csv, win_from, win_to) if stats_csv else None
    offered = args.rate * len(flows)
    delivered = sum(thr)
    row = {
        'algorithm': arm, 'module': module, 'poll_s': args.poll,
        'rep': rep, 'seed': flow_seed,
        'n_flows': len(flows), 'rate_mbps': args.rate,
        'duration': args.duration, 'stagger': args.stagger, 'bw': args.bw,
        'flows': ';'.join(f'{s}>{d}' for s, d in flows),
        'offered_mbps': offered,
        'agg_delivered_mbps': round(delivered, 3),
        'delivered_ratio': round(delivered / offered, 4),
        'jain_flows': jain(thr),   # failed flows are 0, not dropped
        'mean_loss_pct': round(sum(r['loss_pct'] for r in flow_rows) / len(flow_rows), 3),
        'failed_flows': sum(r['failed'] for r in flow_rows),
        'stats_csv': stats_csv, 'ctrl_log': ctrl_log,
        't_start': round(t0, 3), 't_end': round(t_flows_end, 3),
    }
    for k in ('max_link_mean_util', 'max_link_peak_util', 'uplink_jain',
              'n_links_used', 'min_samples', 'bad_samples'):
        row[k] = '' if lm is None or lm[k] is None else lm[k]
    os.makedirs(RESULTS, exist_ok=True)
    append_row(args.runs_csv, RUN_FIELDS, row)
    for fr in flow_rows:
        fr.update(algorithm=arm, rep=rep)
        append_row(args.flows_csv, FLOW_FIELDS, fr)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--algorithms', nargs='+',
                    default=['shortest_path', 'round_robin', 'least_congested'])
    ap.add_argument('--reps', type=int, default=5)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--flows', type=int, default=4)
    ap.add_argument('--rate', type=float, default=8.0, help='Mbps offered per flow')
    ap.add_argument('--duration', type=int, default=25, help='seconds per flow')
    ap.add_argument('--stagger', type=float, default=2.5,
                    help='seconds between flow starts (0 = all at once)')
    ap.add_argument('--bw', type=float, default=10)
    ap.add_argument('--settle', type=float, default=4)
    ap.add_argument('--poll', type=float, default=0.5,
                    help='controller port-stats polling interval (s)')
    ap.add_argument('--tag', default='', help='suffix for the results files')
    ap.add_argument('--logdir', default='results/gate_logs')
    ap.add_argument('--allow-anomaly', action='store_true')
    args = ap.parse_args()

    suffix = f'_{args.tag}' if args.tag else ''
    args.runs_csv = os.path.join(RESULTS, f'gate_runs{suffix}.csv')
    args.flows_csv = os.path.join(RESULTS, f'gate_flows{suffix}.csv')
    if os.getuid() != 0:
        sys.exit("Run with sudo, from the project root.")
    if not os.path.exists(os.path.join(ROOT, 'controller.py')):
        sys.exit("Run from the project root (the folder containing controller.py).")
    window = args.duration - (args.flows - 1) * args.stagger - WARMUP_S
    if window < 6:
        sys.exit(f"Steady window is only {window:.1f}s; raise --duration or lower "
                 f"--stagger/--flows (need >= 6s).")
    if os.path.exists('models/anomaly_model.joblib') and not args.allow_anomaly:
        sys.exit("Anomaly detection is ON (models/anomaly_model.joblib exists).\n"
                 "Run: mv models/anomaly_model.joblib models/anomaly_model.joblib.off\n"
                 "(or pass --allow-anomaly to override).")
    for arm in args.algorithms:
        if arm.startswith('ql_frozen_'):
            arm_spec(arm, args.poll)  # validates the name
            table = ARM_TABLES[arm[len('ql_frozen_'):]]
            if not os.path.exists(table):
                sys.exit(f"{arm} needs {table}; train it first: "
                         f"sudo python3 experiments/train_ql.py --state "
                         f"{arm[len('ql_frozen_'):]}")
    if 'q_learning' in args.algorithms:
        print("NOTE: q_learning here starts cold every run (mostly random, "
              "epsilon~1); treat it as exploratory, not a trained agent.")

    total = args.reps * len(args.algorithms)
    done = 0
    for rep in range(1, args.reps + 1):
        order = list(args.algorithms)
        random.Random(args.seed * 1000 + rep).shuffle(order)
        for algo in order:
            done += 1
            print(f"[{done}/{total}] rep {rep} algorithm {algo} ...", flush=True)
            try:
                r = run_one(algo, rep, args.seed * 1000 + rep, args)
                print(f"    delivered {r['agg_delivered_mbps']}/{r['offered_mbps']} Mbps"
                      f" | loss {r['mean_loss_pct']}% | max link util "
                      f"{r['max_link_mean_util']} | uplink Jain {r['uplink_jain']}",
                      flush=True)
            except Exception as e:  # keep the matrix going
                print(f"    FAILED: {e}", flush=True)
    cleanup()
    print(f"\nDone. Rows in {args.runs_csv}. Next: python3 experiments/"
          f"summarize_gate.py --file {args.runs_csv}")


if __name__ == '__main__':
    main()
