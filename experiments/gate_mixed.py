"""
Mixed TCP + UDP routing test: 3 UDP flows (8 Mbps each) and 3 TCP flows start
at the same moment between cross-pod host pairs. Same flows for every
algorithm in a repetition, shuffled order, fresh controller per run, static
ARP. TCP is capped at 5 Mbps per switch by the OpenFlow meter, so the
question here is how routing treats the UDP flows next to TCP, and whether
TCP is starved.

Run from ~/Documents/sdn-rl, as root, nothing else running, anomaly model off:
    sudo python3 experiments/gate_mixed.py --reps 15
    python3 experiments/gate_mixed.py summary
Results: results/gate_mixed.csv
"""
import argparse, csv, os, random, re, statistics, subprocess, sys, time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'experiments'))
import gate_matrix as gm  # noqa: E402

CSV = 'results/gate_mixed.csv'
FIELDS = ['algorithm', 'rep', 'seed', 'udp_mbps', 'tcp_mbps', 'total_mbps', 'udp_loss_pct', 'failed_flows']
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
       9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 19: 2.093}
TCP_LINE = re.compile(r'0\.0+-\s*([\d.]+)\s+sec\s+[\d.]+\s+\S+\s+([\d.]+)\s+(\w?)bits/sec')


def tcp_mbps(text):
    best = None
    for m in TCP_LINE.finditer(text):
        if best is None or float(m.group(1)) > best[0]:
            best = (float(m.group(1)), m)
    if best is None:
        return None
    m = best[1]
    return float(m.group(2)) * {'': 1e-6, 'K': 1e-3, 'M': 1.0, 'G': 1e3}.get(m.group(3), 1.0)


def run_one(arm, rep, seed, a):
    module, env = gm.arm_spec(arm, 0.5)
    flows = gm.pick_flows(seed, 6)
    kinds = ['udp'] * 3 + ['tcp'] * 3
    gm.cleanup()
    os.makedirs('results/gate_logs', exist_ok=True)
    log = f'results/gate_logs/ctrl_mixed_{arm}_rep{rep}.log'
    ctrl = gm.start_controller(module, log, env)
    net = None
    try:
        gm.wait_controller(ctrl, log)
        net, hosts = gm.setup_net(10, 4)
        for k, ((s, d), kind) in enumerate(zip(flows, kinds)):
            p = gm.BASE_PORT + 100 + k
            hosts[d].cmd(f'iperf {"-u " if kind == "udp" else ""}-s -p {p} -i 1 > /tmp/mix_srv_{k}.txt 2>&1 &')
        time.sleep(1)
        for k, ((s, d), kind) in enumerate(zip(flows, kinds)):
            p = gm.BASE_PORT + 100 + k
            if kind == 'udp':
                hosts[s].cmd(f'iperf -u -c {hosts[d].IP()} -p {p} -b 8M -t {a.duration} > /tmp/mix_cli_{k}.txt 2>&1 &')
            else:
                hosts[s].cmd(f'iperf -c {hosts[d].IP()} -p {p} -t {a.duration} > /tmp/mix_cli_{k}.txt 2>&1 &')
        time.sleep(a.duration + 4)
        udp, tcp, loss, failed = 0.0, 0.0, [], 0
        for k, ((s, d), kind) in enumerate(zip(flows, kinds)):
            if kind == 'udp':
                r = gm.parse_server(hosts[d].cmd(f'cat /tmp/mix_srv_{k}.txt'))
                if r is None:
                    failed += 1; loss.append(100.0)
                else:
                    udp += r['thr_mbps']; loss.append(r['loss_pct'])
            else:
                v = tcp_mbps(hosts[s].cmd(f'cat /tmp/mix_cli_{k}.txt'))
                if v is None:
                    failed += 1
                else:
                    tcp += v
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
    return {'algorithm': arm, 'rep': rep, 'seed': seed, 'udp_mbps': round(udp, 3),
            'tcp_mbps': round(tcp, 3), 'total_mbps': round(udp + tcp, 3),
            'udp_loss_pct': round(statistics.mean(loss), 3), 'failed_flows': failed}


def ci(v):
    n = len(v)
    if n < 2:
        return statistics.mean(v), 0.0
    t = T95.get(n - 1) or T95[min(T95, key=lambda k: abs(k - (n - 1)))]
    return statistics.mean(v), t * statistics.stdev(v) / n ** 0.5


def summary(ref):
    rows = list(csv.DictReader(open(CSV)))
    algos = sorted({r['algorithm'] for r in rows})
    by = {a: {int(r['rep']): r for r in rows if r['algorithm'] == a} for a in algos}
    print(f'\nMixed TCP+UDP, mean +/- 95% CI (n per algorithm: ' + ', '.join(f"{a}={len(by[a])}" for a in algos) + ')')
    print(f'{"algorithm":22s}{"UDP Mbps":>16s}{"TCP Mbps":>16s}{"total Mbps":>16s}{"UDP loss %":>14s}')
    for a in algos:
        cells = []
        for k in ('udp_mbps', 'tcp_mbps', 'total_mbps', 'udp_loss_pct'):
            m, h = ci([float(r[k]) for r in by[a].values()])
            cells.append(f'{m:.2f}+/-{h:.2f}')
        print(f'{a:22s}{cells[0]:>16s}{cells[1]:>16s}{cells[2]:>16s}{cells[3]:>14s}')
    if ref in by:
        print(f'\nPaired difference in total Mbps vs {ref}:')
        for a in algos:
            if a == ref:
                continue
            d = [float(by[a][r]['total_mbps']) - float(by[ref][r]['total_mbps']) for r in by[a] if r in by[ref]]
            if d:
                m, h = ci(d)
                print(f'  {a:22s}{m:+.2f} +/- {h:.2f}  better in {sum(x > 0 for x in d)}, worse in {sum(x < 0 for x in d)} of {len(d)}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', nargs='?', default='run', choices=['run', 'summary'])
    ap.add_argument('--reps', type=int, default=15)
    ap.add_argument('--seed', type=int, default=21)
    ap.add_argument('--duration', type=int, default=25)
    ap.add_argument('--ref', default='random_path')
    ap.add_argument('--algorithms', nargs='+',
                    default=['shortest_path', 'random_path', 'least_congested', 'least_congested_qos',
                             'lcp_pending', 'ql_frozen_per_link'])
    a = ap.parse_args()
    if a.mode == 'summary':
        return summary(a.ref)
    if os.getuid() != 0:
        sys.exit('Run with sudo.')
    if os.path.exists('models/anomaly_model.joblib'):
        sys.exit('Anomaly model is ON: mv models/anomaly_model.joblib models/anomaly_model.joblib.off')
    os.makedirs('results', exist_ok=True)
    jobs = [(r, al) for r in range(1, a.reps + 1) for al in a.algorithms]
    random.Random(5).shuffle(jobs)
    for i, (rep, algo) in enumerate(jobs, 1):
        print(f'[{i}/{len(jobs)}] rep {rep} {algo} ...', flush=True)
        row = run_one(algo, rep, a.seed * 1000 + rep, a)
        print('    ', row, flush=True)
        new = not os.path.exists(CSV)
        with open(CSV, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        gm.own(CSV)
    print('\nDone. Next: python3 experiments/gate_mixed.py summary')


if __name__ == '__main__':
    main()
