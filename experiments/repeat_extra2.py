"""
Repeat the two weakest single-run measurements, many times each, with a
FRESH controller per run (reuses the helpers of gate_matrix.py).

  failure : h1 -> h5 ping every 5 ms while link e1<->a1 goes down
            (single failure), then e1<->a2 also goes down (e1 isolated),
            then both come back.  Measures per run:
              single_gap_s    longest gap between ping replies after the
                              first failure (how long the flow was cut)
              isolated_replies replies received while e1 is isolated (want 0)
              restore_s       time from restoring the links to the first reply
  qos     : 3 TCP bulk flows (h2>h6, h3>h7, h4>h8) plus one small UDP probe
            (h1>h5, 1 Mbps) plus ping h1>h5.  Measures UDP jitter, UDP loss
            and mean ping RTT with class-agnostic vs class-aware routing.

Run from ~/Documents/sdn-rl, as root, nothing else running:
    sudo python3 experiments/repeat_extra2.py failure --reps 10
    sudo python3 experiments/repeat_extra2.py qos --reps 10
    python3 experiments/repeat_extra2.py summary
The anomaly model must be OFF (models/anomaly_model.joblib.off).
"""
import argparse, csv, os, random, re, statistics, subprocess, sys, time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'experiments'))
import gate_matrix as gm  # noqa: E402

T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
       14: 2.145, 15: 2.131, 19: 2.093, 29: 2.045}
PING_RE = re.compile(r'^\[(\d+\.\d+)\]\s+\d+ bytes from.*time=([\d.]+) ms')


def ci(vals):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return None, None, 0
    m = statistics.mean(vals)
    if n < 2:
        return m, 0.0, n
    t = T95.get(n - 1) or T95[min(T95, key=lambda k: abs(k - (n - 1)))]
    return m, t * statistics.stdev(vals) / n ** 0.5, n


def read_pings(host, path):
    out = []
    for line in host.cmd(f'cat {path}').splitlines():
        m = PING_RE.match(line)
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
    return out


def with_controller(algo, tag, fn):
    gm.cleanup()
    os.makedirs('results/gate_logs', exist_ok=True)
    log = f'results/gate_logs/ctrl_{tag}.log'
    ctrl = gm.start_controller(algo, log, {'SDN_STATS_POLL': '0.5'})
    net = None
    try:
        gm.wait_controller(ctrl, log)
        net, hosts = gm.setup_net(10, 4)
        return fn(net, hosts)
    finally:
        if net is not None:
            net.stop()
        ctrl.terminate()
        try:
            ctrl.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ctrl.kill()


def run_failure(net, hosts):
    h1 = hosts['h1']
    h1.cmd('rm -f /tmp/rep_ping.log')
    h1.cmd('ping -D -i 0.005 10.0.0.5 > /tmp/rep_ping.log 2>&1 &')
    time.sleep(5)
    # find which uplink of e1 the ping really uses, and fail THAT one first
    used, other = 'a1', 'a2'
    e1 = net.get('e1')
    port_of = {}
    for nb in ('a1', 'a2'):
        for l in net.linksBetween(e1, net.get(nb)):
            port_of[e1.ports[l.intf1 if l.intf1.node is e1 else l.intf2]] = nb
    for line in e1.cmd('ovs-ofctl -O OpenFlow13 dump-flows e1').splitlines():
        if 'nw_dst=10.0.0.5' in line and 'icmp' in line:
            m = re.search(r'output:(\d+)', line)
            if m and int(m.group(1)) in port_of:
                used = port_of[int(m.group(1))]
                other = 'a2' if used == 'a1' else 'a1'
    t1 = time.time(); net.configLinkStatus('e1', used, 'down'); time.sleep(8)
    t2 = time.time(); net.configLinkStatus('e1', other, 'down'); time.sleep(6)
    t3 = time.time(); net.configLinkStatus('e1', used, 'up')
    net.configLinkStatus('e1', other, 'up'); time.sleep(10)
    h1.cmd('pkill -f "ping -D"'); time.sleep(0.5)
    ts = [t for t, _ in read_pings(h1, '/tmp/rep_ping.log')]
    w1 = [t1] + [t for t in ts if t1 <= t < t2] + [t2]
    gap = max(b - a for a, b in zip(w1, w1[1:]))
    iso = len([t for t in ts if t2 + 0.5 <= t < t3])
    after = [t for t in ts if t > t3]
    return {'single_gap_s': round(gap, 3), 'isolated_replies': iso,
            'restore_s': round(after[0] - t3, 3) if after else '',
            'used_uplink_a1': int(used == 'a1')}


def run_qos(net, hosts):
    for src, dst, p in (('h2', 'h6', 5301), ('h3', 'h7', 5302), ('h4', 'h8', 5303)):
        hosts[dst].cmd(f'iperf -s -p {p} > /dev/null 2>&1 &')
    hosts['h5'].cmd('iperf -u -s -p 5310 -i 1 > /tmp/rep_udp_srv.txt 2>&1 &')
    time.sleep(1)
    for src, dst, p in (('h2', 'h6', 5301), ('h3', 'h7', 5302), ('h4', 'h8', 5303)):
        hosts[src].cmd(f'iperf -c {hosts[dst].IP()} -p {p} -t 22 > /dev/null 2>&1 &')
    time.sleep(5)
    h1 = hosts['h1']
    h1.cmd('rm -f /tmp/rep_qping.log')
    h1.cmd('ping -D -i 0.1 -c 120 10.0.0.5 > /tmp/rep_qping.log 2>&1 &')
    h1.cmd('iperf -u -c 10.0.0.5 -p 5310 -b 1M -t 12 > /dev/null 2>&1 &')
    time.sleep(16)
    pr = gm.parse_server(hosts['h5'].cmd('cat /tmp/rep_udp_srv.txt'))
    rtts = [r for _, r in read_pings(h1, '/tmp/rep_qping.log')]
    for h in hosts.values():
        h.cmd('pkill -9 -f iperf; pkill -9 ping')
    return {'udp_jitter_ms': pr['jitter_ms'] if pr else '',
            'udp_loss_pct': pr['loss_pct'] if pr else 100.0,
            'ping_rtt_ms': round(statistics.mean(rtts), 3) if rtts else '',
            'ping_replies': len(rtts)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['failure', 'qos', 'summary'])
    ap.add_argument('--reps', type=int, default=10)
    ap.add_argument('--algorithms', nargs='+')
    a = ap.parse_args()

    if a.mode == 'summary':
        for name in ('failure', 'qos'):
            path = f'results/repeat_{name}.csv'
            if not os.path.exists(path):
                continue
            rows = list(csv.DictReader(open(path)))
            print(f'\n=== {name} ({path}) mean +/- 95% CI (n) ===')
            metrics = [k for k in rows[0] if k not in ('algorithm', 'rep')]
            for algo in sorted({r['algorithm'] for r in rows}):
                sub = [r for r in rows if r['algorithm'] == algo]
                parts = []
                for k in metrics:
                    vals = [float(r[k]) for r in sub if r[k] != '']
                    m, h, n = ci(vals)
                    parts.append(f'{k}={m:.3f}+/-{h:.3f} (n={n})' if m is not None else f'{k}=n/a')
                print(f'  {algo:22s} ' + '  '.join(parts))
        return

    if os.getuid() != 0:
        sys.exit('Run with sudo.')
    if os.path.exists('models/anomaly_model.joblib'):
        sys.exit('Anomaly model is ON. Run: mv models/anomaly_model.joblib models/anomaly_model.joblib.off')
    algos = a.algorithms or (['least_congested', 'shortest_path'] if a.mode == 'failure'
                             else ['shortest_path', 'least_congested', 'least_congested_qos'])
    fn = run_failure if a.mode == 'failure' else run_qos
    path = f'results/repeat_{a.mode}.csv'
    os.makedirs('results', exist_ok=True)
    jobs = [(r, al) for r in range(1, a.reps + 1) for al in algos]
    random.Random(3).shuffle(jobs)
    for k, (rep, algo) in enumerate(jobs, 1):
        print(f'[{k}/{len(jobs)}] rep {rep} {algo} ...', flush=True)
        row = with_controller(algo, f'{a.mode}_{algo}_{rep}', fn)
        row.update(algorithm=algo, rep=rep)
        print('    ', row, flush=True)
        new = not os.path.exists(path)
        with open(path, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['algorithm', 'rep'] + [k2 for k2 in row if k2 not in ('algorithm', 'rep')])
            if new:
                w.writeheader()
            w.writerow(row)
        gm.own(path)
    print(f'\nDone. Next: python3 experiments/repeat_extra2.py summary')


if __name__ == '__main__':
    main()
