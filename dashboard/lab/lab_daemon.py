"""
Privileged lab daemon: holds a persistent Mininet network + manages the
Ryu controller subprocess, and exposes a small JSON-line TCP protocol on
127.0.0.1:9191 for the (unprivileged) FastAPI backend to drive:
start/stop the topology, switch the active algorithm (restarts Ryu),
and start/stop arbitrary host-to-host traffic on demand.

Run with sudo (Mininet/OVS requires root), from the project root:
    sudo python3 dashboard/lab/lab_daemon.py   # SYSTEM python, NOT ryu-env
(mininet is only importable there; this daemon launches Ryu itself.)

Topology is built directly from topology.py's ADJACENCY and HOST_LOCATION
-- the single source of truth the rest of the project already uses --
with every switch-switch link's ports pinned explicitly via port1=/port2=
to match ADJACENCY exactly, and every switch's dpid pinned explicitly to
its numeric ID (1-10), rather than relying on Mininet's default add-order
assignment. This reconstructs the same topology controller.py already
expects.

IMPORTANT: this file's Mininet-dependent code (start_topology, the
traffic iperf commands) could not be tested against real Mininet/OVS in
the environment this was written in. Test it in isolation first:
    sudo python3 -c "
    import sys; sys.path.insert(0, '.')
    from dashboard.lab.lab_daemon import LabState
    s = LabState()
    print(s.start_topology())
    print(s.net.pingAll())   # expect 0% dropped
    s.stop_topology()
    "
before relying on the rest of the dashboard's control features. If
pingAll() shows drops, the most likely cause is a port1/port2 mismatch
versus your actual fattree_topo.py -- tell Claude what pingAll() printed
and the exact dpid each switch reports and it can be fixed precisely
rather than guessed at again.
"""
import os
import re
import sys
import json
import time
import signal
import socket
import socketserver
import subprocess
import threading

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, PROJECT_ROOT)

HOST = '127.0.0.1'
PORT = 9191

RYU_MANAGER = os.path.join(PROJECT_ROOT, 'ryu-env', 'bin', 'ryu-manager')
CONTROLLER_PY = 'controller.py'

VALID_ALGORITHMS = {'round_robin', 'least_congested', 'least_congested_qos', 'q_learning',
                    'shortest_path', 'random_path', 'global_rr', 'lcp_pending'}
COMPARE_ALGORITHMS = ['shortest_path', 'random_path', 'global_rr', 'least_congested',
                      'lcp_pending', 'q_learning']
ANOMALY_MODEL = os.path.join(PROJECT_ROOT, 'models', 'anomaly_model.joblib')
COMPARE_DURATION = 12      # seconds each flow sends
COMPARE_RATE = '8M'        # offered per flow (about 8.39 Mbps measured)
COMPARE_HOSTS = ['h%d' % i for i in range(1, 9)]
COMPARE_POD = {h: (0 if int(h[1:]) <= 4 else 1) for h in COMPARE_HOSTS}
SERVER_LINE = re.compile(
    r'\[\s*\d+\]\s+0\.0+-\s*([\d.]+)\s+sec\s+[\d.]+\s+\S+\s+([\d.]+)\s+(\w?)bits/sec'
    r'\s+([\d.]+)\s+ms\s+(\d+)/\s*(\d+)\s+\(([\d.]+)%\)')


# One iperf server interval line, e.g.
# [  3]  2.0- 3.0 sec  1.00 MBytes  8.39 Mbits/sec  0.020 ms  0/ 716 (0%)
INTERVAL_LINE = re.compile(
    r'\[\s*\d+\]\s+([\d.]+)-\s*([\d.]+)\s+sec\s+[\d.]+\s+\S+\s+([\d.]+)\s+(\w?)bits/sec'
    r'\s+[\d.]+\s+ms')


def parse_intervals(text):
    """Per-second delivered Mbps of one flow: {second_index: mbps}. Only
    1-second interval lines are used, never the whole-run summary line."""
    out = {}
    for m in INTERVAL_LINE.finditer(text):
        a, b = float(m.group(1)), float(m.group(2))
        if not (0.5 <= b - a <= 1.5):
            continue
        unit = {'': 1e-6, 'K': 1e-3, 'M': 1.0, 'G': 1e3}.get(m.group(4), 1.0)
        out[int(round(b))] = float(m.group(3)) * unit
    return out


def jain(values):
    v = [max(0.0, x) for x in values]
    if not v or sum(x * x for x in v) == 0:
        return 0.0
    return (sum(v) ** 2) / (len(v) * sum(x * x for x in v))


def pick_flows(seed, n):
    """n (src, dst) pairs, distinct sources and destinations, every pair
    crossing pods. Same function and meaning as in experiments/gate_matrix.py."""
    import random
    rng = random.Random(seed)
    for _ in range(100000):
        srcs = rng.sample(COMPARE_HOSTS, n)
        dsts = rng.sample(COMPARE_HOSTS, n)
        if all(COMPARE_POD[a] != COMPARE_POD[b] for a, b in zip(srcs, dsts)):
            return list(zip(srcs, dsts))
    raise RuntimeError('no flow set found')


def parse_server(text):
    best = None
    for m in SERVER_LINE.finditer(text):
        dur = float(m.group(1))
        if best is None or dur > best[0]:
            best = (dur, m)
    if best is None:
        return None
    m = best[1]
    unit = {'': 1e-6, 'K': 1e-3, 'M': 1.0, 'G': 1e3}.get(m.group(3), 1.0)
    return {'mbps': float(m.group(2)) * unit, 'loss_pct': float(m.group(7))}

# q_learning in the dashboard starts from the table trained offline
# (experiments/train_ql.py) and stays frozen, so it behaves like the
# evaluated agent instead of starting cold. Falls back to cold if missing.
QL_TABLE = os.path.join(PROJECT_ROOT, 'models', 'ql_per_link.json')
VALID_PROTOCOLS = {'tcp', 'udp'}
BW_RE = re.compile(r'^\d+(\.\d+)?[KMG]?$')


class LabState:
    def __init__(self):
        self.lock = threading.RLock()
        self.net = None
        self.hosts = {}
        self.switches = {}
        self.ryu_proc = None
        self.ryu_log = None
        self.current_algorithm = None
        self.compare = {'state': 'idle'}
        self.traffic = {}  # traffic_id -> dict(src, dst, protocol, duration, client_pid, server_pid, started)
        self._next_traffic_id = 1

    # ---------------- topology ----------------

    def start_topology(self):
        with self.lock:
            if self.net is not None:
                return {'already_running': True, 'hosts': sorted(self.hosts.keys())}

            from mininet.net import Mininet
            from mininet.node import RemoteController, OVSSwitch
            from mininet.link import TCLink
            from topology import ADJACENCY, HOST_LOCATION

            net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
            c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

            switches = {}
            for sw_id in ADJACENCY:
                switches[sw_id] = net.addSwitch(
                    's%d' % sw_id, dpid='%016x' % sw_id, protocols='OpenFlow13')

            added = set()
            for node, ports in ADJACENCY.items():
                for port, neighbor in ports.items():
                    key = frozenset((node, neighbor))
                    if key in added:
                        continue
                    added.add(key)
                    neighbor_port = next(p for p, n in ADJACENCY[neighbor].items() if n == node)
                    net.addLink(switches[node], switches[neighbor],
                                port1=port, port2=neighbor_port, bw=10)

            hosts = {}
            for ip, (sw_id, port) in HOST_LOCATION.items():
                hname = 'h' + ip.split('.')[-1]
                h = net.addHost(hname, ip='%s/24' % ip)
                hosts[hname] = h
                net.addLink(h, switches[sw_id], port2=port, bw=10)

            net.build()
            c0.start()
            for sw in switches.values():
                sw.start([c0])

            self.net, self.hosts, self.switches = net, hosts, switches
            return {'started': True, 'hosts': sorted(hosts.keys()),
                    'switches': sorted(switches.keys())}

    def stop_topology(self):
        with self.lock:
            self._stop_ryu()
            for tid in list(self.traffic.keys()):
                self._stop_traffic_unlocked(tid)
            if self.net is not None:
                self.net.stop()
                self.net = None
                self.hosts = {}
                self.switches = {}
            return {'stopped': True}

    # ---------------- ryu / algorithm ----------------

    def _start_ryu(self, algorithm):
        env = os.environ.copy()
        env['SDN_ALGORITHM'] = algorithm
        env['SDN_STATS_POLL'] = '0.5'
        if algorithm == 'q_learning' and os.path.exists(QL_TABLE):
            env['SDN_QL_LOAD'] = QL_TABLE
            env['SDN_QL_STATE'] = 'per_link'
            env['SDN_QL_FREEZE'] = '1'
            env['SDN_QL_TIEBREAK'] = 'random'
        log_path = os.path.join(PROJECT_ROOT, 'dashboard_lab_ryu.log')
        self.ryu_log = open(log_path, 'a')
        self.ryu_proc = subprocess.Popen(
            [RYU_MANAGER, CONTROLLER_PY],
            cwd=PROJECT_ROOT, env=env,
            stdout=self.ryu_log, stderr=subprocess.STDOUT)
        self.current_algorithm = algorithm
        # Give switches a few seconds to (re)connect. Not a hard
        # guarantee -- /api/lab/status after this reports whatever the
        # actual current_algorithm is, so the frontend can poll rather
        # than trust this blindly.
        time.sleep(4)

    def _stop_ryu(self):
        if self.ryu_proc is not None:
            self.ryu_proc.terminate()
            try:
                self.ryu_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ryu_proc.kill()
            self.ryu_proc = None
        if self.ryu_log is not None:
            self.ryu_log.close()
            self.ryu_log = None

    def set_algorithm(self, algorithm):
        if algorithm not in VALID_ALGORITHMS:
            return {'error': 'unknown algorithm: %s (must be one of %s)'
                              % (algorithm, sorted(VALID_ALGORITHMS))}
        with self.lock:
            if self.compare.get('state') == 'running':
                return {'error': 'a comparison is running -- wait for it to finish'}
            if self.net is None:
                return {'error': 'topology not running -- call start_topology first'}
            # Restarting Ryu mid-test would drop the routing of every
            # flow still running, so refuse a *change* of algorithm
            # while any test is active.
            self._reap_finished()
            if self.traffic and algorithm != self.current_algorithm:
                return {'error': 'cannot switch to %s while %d test(s) are running on %s'
                                  % (algorithm, len(self.traffic), self.current_algorithm)}
            self._stop_ryu()
            self._start_ryu(algorithm)
            return {'algorithm': algorithm, 'restarted': True}

    # ---------------- traffic ----------------

    def run_traffic(self, src, dst, protocol, duration, bandwidth):
        if protocol not in VALID_PROTOCOLS:
            return {'error': 'protocol must be "tcp" or "udp"'}
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return {'error': 'duration must be an integer (seconds)'}
        if not (1 <= duration <= 120):
            return {'error': 'duration must be between 1 and 120 seconds'}
        bw = bandwidth or '2M'
        if protocol == 'udp' and not BW_RE.match(bw):
            return {'error': "bandwidth must look like e.g. '5M', '500K', '1G'"}

        with self.lock:
            if self.compare.get('state') == 'running':
                return {'error': 'a comparison is running -- wait for it to finish'}
            if self.net is None:
                return {'error': 'topology not running'}
            src_h = self.hosts.get(src)
            dst_h = self.hosts.get(dst)
            if src_h is None or dst_h is None:
                return {'error': 'unknown host (must be one of %s)'
                                  % sorted(self.hosts.keys())}
            if src == dst:
                return {'error': 'src and dst must differ'}

            self._reap_finished()  # don't let an already-finished entry block a legit retry
            for m in self.traffic.values():
                if {m['src'], m['dst']} == {src, dst}:
                    return {'error': 'a test between %s and %s is already running' % (src, dst)}

            n = self._next_traffic_id
            tid = 't%d' % n
            self._next_traffic_id += 1
            # Unique iperf port per test, so two concurrent tests that
            # share a destination host (e.g. h1->h5 and h3->h5) don't
            # fight over the default port 5001 on that host.
            port = 5000 + (n % 1000)
            dst_ip = dst_h.IP()

            if protocol == 'udp':
                server_pid = dst_h.cmd(
                    'iperf -s -u -p %d > /tmp/iperf_%s_srv.log 2>&1 & echo $!' % (port, tid)).strip()
                time.sleep(0.3)
                client_pid = src_h.cmd(
                    'iperf -c %s -u -p %d -b %s -t %d > /tmp/iperf_%s_cli.log 2>&1 & echo $!'
                    % (dst_ip, port, bw, duration, tid)).strip()
            else:
                server_pid = dst_h.cmd(
                    'iperf -s -p %d > /tmp/iperf_%s_srv.log 2>&1 & echo $!' % (port, tid)).strip()
                time.sleep(0.3)
                client_pid = src_h.cmd(
                    'iperf -c %s -p %d -t %d > /tmp/iperf_%s_cli.log 2>&1 & echo $!'
                    % (dst_ip, port, duration, tid)).strip()

            self.traffic[tid] = {
                'src': src, 'dst': dst, 'protocol': protocol,
                'duration': duration, 'bandwidth': bw, 'port': port,
                'server_pid': server_pid, 'client_pid': client_pid,
                'started': time.time(),
            }
            return {'traffic_id': tid, 'src': src, 'dst': dst,
                    'protocol': protocol, 'duration': duration, 'bandwidth': bw}

    def stop_traffic(self, tid):
        with self.lock:
            return self._stop_traffic_unlocked(tid)

    def _stop_traffic_unlocked(self, tid):
        if tid not in self.traffic:
            return {'error': 'unknown traffic_id'}
        meta = self.traffic.pop(tid)
        src_h = self.hosts.get(meta['src'])
        dst_h = self.hosts.get(meta['dst'])
        if src_h and meta.get('client_pid'):
            src_h.cmd('kill %s 2>/dev/null' % meta['client_pid'])
        if dst_h and meta.get('server_pid'):
            dst_h.cmd('kill %s 2>/dev/null' % meta['server_pid'])
        return {'stopped': tid}

    def _reap_finished(self):
        """Auto-removes traffic entries whose iperf process has already
        finished on its own (duration elapsed + a small buffer). iperf
        is started with -t <duration>, so it self-terminates; without
        this, finished tests would sit in self.traffic forever, showing
        absurd elapsed times and cluttering the active-traffic list."""
        now = time.time()
        for tid in [t for t, m in self.traffic.items() if now - m['started'] > m['duration'] + 2]:
            # Goes through the normal stop path so the iperf *server*
            # (which never self-terminates) is killed too. With one port
            # per test, leaving it running would leak a process per run.
            self._stop_traffic_unlocked(tid)

    def list_traffic(self):
        with self.lock:
            self._reap_finished()
            now = time.time()
            return [
                {'traffic_id': tid, 'src': m['src'], 'dst': m['dst'],
                 'protocol': m['protocol'], 'duration': m['duration'],
                 'elapsed': round(now - m['started'], 1)}
                for tid, m in self.traffic.items()
            ]

    # ---------------- comparison (same flows, several algorithms) ----------------

    def compare_start(self, algorithms, n_flows=6):
        if not isinstance(algorithms, list) or not (2 <= len(algorithms) <= 6):
            return {'error': 'choose between 2 and 6 algorithms'}
        for a in algorithms:
            if a not in COMPARE_ALGORITHMS:
                return {'error': 'unknown algorithm: %s' % a}
        if len(set(algorithms)) != len(algorithms):
            return {'error': 'each algorithm only once'}
        try:
            n_flows = int(n_flows)
        except (TypeError, ValueError):
            return {'error': 'flows must be a number'}
        if not (2 <= n_flows <= 8):
            return {'error': 'flows must be between 2 and 8'}
        if os.path.exists(ANOMALY_MODEL):
            return {'error': 'the anomaly model is on and would cut links during the test; '
                             'move models/anomaly_model.joblib to models/anomaly_model.joblib.off '
                             'and restart the daemon'}
        with self.lock:
            if self.compare.get('state') == 'running':
                return {'error': 'a comparison is already running'}
            self._reap_finished()
            if self.traffic:
                return {'error': 'wait for the running test to finish first'}
            seed = int(time.time()) % 100000
            flows = pick_flows(seed, n_flows)
            self.compare = {
                'state': 'running', 'algorithms': list(algorithms), 'current': None,
                'phase': 'starting', 'seed': seed, 'duration': COMPARE_DURATION,
                'flows': [{'src': a, 'dst': b} for a, b in flows],
                'offered_mbps': round(8.39 * n_flows, 1), 'results': {}, 'started': time.time(),
            }
        t = threading.Thread(target=self._compare_job, args=(list(algorithms), flows), daemon=True)
        t.start()
        return {'started': True, 'seed': seed}

    def compare_status(self):
        got = self.lock.acquire(timeout=0.3)
        try:
            return json.loads(json.dumps(self.compare))
        finally:
            if got:
                self.lock.release()

    def _set_compare(self, **kw):
        with self.lock:
            self.compare.update(kw)

    def _compare_job(self, algorithms, flows):
        try:
            self._set_compare(phase='starting the network')
            self.start_topology()
            for a in self.hosts.values():
                for b in self.hosts.values():
                    if a is not b:
                        a.setARP(b.IP(), b.MAC())
            for algo in algorithms:
                self._set_compare(current=algo, phase='switching controller')
                with self.lock:
                    self._stop_ryu()
                    self._clear_flows()
                    self._start_ryu(algo)
                self._wait_switches_connected(25)
                time.sleep(2)
                self._set_compare(phase='sending %d flows' % len(flows))
                result = self._compare_run(flows)
                with self.lock:
                    self.compare['results'][algo] = result
            with self.lock:
                self.compare.update(state='done', current=None, phase='finished',
                                    finished=time.time())
        except Exception as e:
            with self.lock:
                self.compare.update(state='error', current=None,
                                    error='%s: %s' % (type(e).__name__, e))

    def _clear_flows(self):
        """Delete every flow rule on every switch while no controller is
        attached, so rules left by the previous algorithm cannot route the
        next one's flows. The new controller reinstalls the table-miss."""
        for sw_id in self.switches:
            subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'del-flows', 's%d' % sw_id],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wait_switches_connected(self, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            out = subprocess.run(['ovs-vsctl', '--columns=is_connected', 'list', 'controller'],
                                 capture_output=True, text=True).stdout
            if out.count('true') >= len(self.switches):
                return True
            time.sleep(0.5)
        return False

    def _tx_bytes(self):
        """{(switch, port): tx bytes} for every switch port."""
        out = {}
        for sw_id in self.switches:
            text = subprocess.run(
                ['ovs-ofctl', '--no-names', '-O', 'OpenFlow13', 'dump-ports', 's%d' % sw_id],
                capture_output=True, text=True).stdout
            for m in re.finditer(r'port\s+(\S+):\s+rx pkts=\d+, bytes=\d+,.*?\n\s+tx pkts=\d+, bytes=(\d+)',
                                 text):
                tok = re.search(r'(\d+)$', m.group(1))
                if tok and m.group(1) != 'LOCAL':
                    out[(sw_id, int(tok.group(1)))] = int(m.group(2))
        return out

    def _compare_run(self, flows):
        from topology import ADJACENCY
        hosts = self.hosts
        base_port = 5200
        for k, (s_name, d_name) in enumerate(flows):
            hosts[d_name].cmd('iperf -u -s -p %d -i 1 > /tmp/cmp_srv_%d.txt 2>&1 &' % (base_port + k, k))
        time.sleep(1)
        before = self._tx_bytes()
        t0 = time.time()
        for k, (s_name, d_name) in enumerate(flows):
            hosts[s_name].cmd('iperf -u -c %s -p %d -b %s -t %d > /tmp/cmp_cli_%d.txt 2>&1 &'
                              % (hosts[d_name].IP(), base_port + k, COMPARE_RATE, COMPARE_DURATION, k))
        # measure link counters over the steady middle of the run
        time.sleep(3)
        mid_a, t_a = self._tx_bytes(), time.time()
        time.sleep(max(1, COMPARE_DURATION - 7))
        mid_b, t_b = self._tx_bytes(), time.time()
        time.sleep(max(0, t0 + COMPARE_DURATION + 3 - time.time()))
        per_flow = []
        total = 0.0
        timeline = {}
        for k, (s_name, d_name) in enumerate(flows):
            srv_text = hosts[d_name].cmd('cat /tmp/cmp_srv_%d.txt' % k)
            for sec, v in parse_intervals(srv_text).items():
                timeline[sec] = timeline.get(sec, 0.0) + v
            r = parse_server(srv_text)
            mbps = r['mbps'] if r else 0.0
            total += mbps
            per_flow.append({'src': s_name, 'dst': d_name, 'mbps': round(mbps, 2),
                             'loss_pct': r['loss_pct'] if r else 100.0})
        for h in hosts.values():
            h.cmd('pkill -9 -f iperf 2>/dev/null')
        link = {}
        span = max(0.5, t_b - t_a)
        for sw_id, ports in ADJACENCY.items():
            for port, nb in ports.items():
                d = mid_b.get((sw_id, port), 0) - mid_a.get((sw_id, port), 0)
                mbps = max(0.0, d * 8 / span / 1e6)
                key = '%d-%d' % (min(sw_id, nb), max(sw_id, nb))
                link[key] = round(max(link.get(key, 0.0), mbps), 2)
        used = [v for v in link.values() if v > 0.5]
        # the 8 edge<->aggregation links: where load balancing is decided
        uplinks = sorted(((k, v) for k, v in link.items()
                          if max(map(int, k.split('-'))) >= 7), key=lambda kv: kv[0])
        n_sec = int(COMPARE_DURATION)
        return {'delivered_mbps': round(total, 2), 'per_flow': per_flow, 'link_mbps': link,
                'max_link_mbps': round(max(link.values()) if link else 0.0, 2),
                'links_used': len(used),
                'uplink_mbps': [{'link': k, 'mbps': v} for k, v in uplinks],
                'jain_uplinks': round(jain([v for _, v in uplinks]), 3),
                'timeline_mbps': [round(timeline.get(t, 0.0), 2) for t in range(1, n_sec + 1)]}

    def pingall(self):
        """Runs Mininet's pingAll() across the live topology -- only
        meaningful once both the topology AND a controller are up (an
        OVS switch with no connected controller drops everything by
        design, so a 100% drop result with no controller running is
        expected, not a sign of a topology bug)."""
        with self.lock:
            if self.net is None:
                return {'error': 'topology not running'}
            drop_pct = self.net.pingAll()
            return {'drop_percent': drop_pct, 'controller_connected': self.ryu_proc is not None}

    def status(self):
        # Never wait behind a long operation (start_topology, set_algorithm,
        # pingall hold the lock for seconds): if the lock is busy, return the
        # last known status marked busy so the dashboard keeps seeing a live
        # daemon instead of timing out.
        got = self.lock.acquire(timeout=0.3)
        if not got:
            last = dict(getattr(self, '_last_status', {}) or {'topology_up': False,
                        'algorithm': None, 'hosts': [], 'active_traffic': []})
            last['busy'] = True
            return last
        try:
            st = {
                'topology_up': self.net is not None,
                'algorithm': self.current_algorithm,
                'hosts': sorted(self.hosts.keys()) if self.net else [],
                'active_traffic': self.list_traffic(),
                'busy': False,
            }
            self._last_status = st
            return st
        finally:
            self.lock.release()


STATE = LabState()


def dispatch(req):
    cmd = req.get('cmd')
    try:
        if cmd == 'status':
            return STATE.status()
        if cmd == 'start_topology':
            return STATE.start_topology()
        if cmd == 'stop_topology':
            return STATE.stop_topology()
        if cmd == 'set_algorithm':
            return STATE.set_algorithm(req.get('algorithm'))
        if cmd == 'run_traffic':
            return STATE.run_traffic(req.get('src'), req.get('dst'), req.get('protocol'),
                                      req.get('duration'), req.get('bandwidth'))
        if cmd == 'stop_traffic':
            return STATE.stop_traffic(req.get('traffic_id'))
        if cmd == 'list_traffic':
            return {'active_traffic': STATE.list_traffic()}
        if cmd == 'compare_start':
            return STATE.compare_start(req.get('algorithms'), req.get('flows', 6))
        if cmd == 'compare_status':
            return STATE.compare_status()
        if cmd == 'pingall':
            return STATE.pingall()
        return {'error': 'unknown cmd: %s' % cmd}
    except Exception as e:
        return {'error': '%s: %s' % (type(e).__name__, e)}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            line = line.decode('utf-8').strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = dispatch(req)
            except json.JSONDecodeError as e:
                resp = {'error': 'bad JSON: %s' % e}
            except Exception as e:
                resp = {'error': '%s: %s' % (type(e).__name__, e)}
            try:
                self.wfile.write((json.dumps(resp) + '\n').encode('utf-8'))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True      # must be a class attribute: it is read at bind time
    daemon_threads = True           # a stuck request must never keep the process alive


def main():
    if os.geteuid() != 0:
        print('lab_daemon.py must be run with sudo (Mininet/OVS requires root).')
        sys.exit(1)

    try:
        server = Server((HOST, PORT), Handler)
    except OSError as e:
        print('Cannot listen on %s:%d (%s).' % (HOST, PORT, e))
        print('Another lab daemon is probably still running. Stop it with:')
        print('    sudo pkill -f lab_daemon.py ; sudo mn -c')
        sys.exit(1)
    print('Lab daemon listening on %s:%d' % (HOST, PORT))

    stop = threading.Event()

    def on_signal(*_):
        stop.set()                  # only set a flag: no work inside a signal handler

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    while not stop.wait(0.5):
        pass

    print('\nShutting down -- stopping topology and Ryu...')
    server.shutdown()
    server.server_close()
    cleaner = threading.Thread(target=STATE.stop_topology, daemon=True)
    cleaner.start()
    cleaner.join(20)                # bounded: never hang on cleanup
    os._exit(0)


if __name__ == '__main__':
    main()
