"""
Checks the patched anomaly layer. From the project root, as root, nothing else
running, with models/anomaly_model.joblib PRESENT (trained by
train_anomaly_valid.py):
    sudo python3 experiments/anomaly_check.py
Phases: 20 s of normal traffic (4 UDP flows), then a 20 s flood (100 Mbps
declared on h1->h5). A ping h3->h7 runs throughout. It prints how many links
were excluded in each phase, the longest stretch of lost pings, and whether
any switch was cut off.
"""
import os
import re
import subprocess
import sys
import time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'experiments'))
import gate_matrix as gm  # noqa: E402

if os.getuid() != 0:
    sys.exit("Run with sudo from the project root.")
if not os.path.exists('models/anomaly_model.joblib'):
    sys.exit("models/anomaly_model.joblib missing (run train_anomaly_valid.py).")
if 'ANOMALY_CONFIRM' not in open('controller.py').read():
    sys.exit("controller.py not patched (run patch_anomaly.py).")

gm.cleanup()
log = 'results/anomaly_check_ctrl.log'
os.makedirs('results', exist_ok=True)
ctrl = gm.start_controller('least_congested', log, {'SDN_STATS_POLL': '0.5'})
net = None
try:
    gm.wait_controller(ctrl, log)
    net, h = gm.setup_net(10, 4)
    flows = [('h1', 'h5'), ('h2', 'h6'), ('h3', 'h7'), ('h4', 'h8')]
    for k, (s, d) in enumerate(flows):
        h[d].cmd(f'iperf -u -s -p {5200+k} > /dev/null 2>&1 &')
    h['h8'].cmd('true')
    time.sleep(1)
    t_ping = time.time()
    h['h7'].cmd('true')
    h['h3'].cmd('ping -i 0.2 -c 300 10.0.0.7 > /tmp/anom_ping.txt 2>&1 &')
    t_normal = time.time()
    for k, (s, d) in enumerate(flows):
        h[s].cmd(f'iperf -u -c {h[d].IP()} -p {5200+k} -t 20 -b 8M > /dev/null 2>&1 &')
    time.sleep(22)
    t_flood = time.time()
    h['h1'].cmd(f'iperf -u -c {h["h5"].IP()} -p 5200 -t 20 -b 100M > /dev/null 2>&1 &')
    h['h2'].cmd(f'iperf -u -c {h["h6"].IP()} -p 5201 -t 20 -b 100M > /dev/null 2>&1 &')
    time.sleep(24)
    t_end = time.time()
    time.sleep(max(0, t_ping + 62 - time.time()))
    ping = h['h3'].cmd('cat /tmp/anom_ping.txt')
    for x in h.values():
        x.cmd('pkill -9 -f iperf; pkill -9 ping')
finally:
    if net is not None:
        net.stop()
    ctrl.terminate()
    try:
        ctrl.wait(timeout=10)
    except subprocess.TimeoutExpired:
        ctrl.kill()
    gm.cleanup()

txt = open(log).read()
det = [float(m) for m in re.findall(r'\[t=([\d.]+)\] ANOMALY DETECTED', txt)]
clr = [float(m) for m in re.findall(r'\[t=([\d.]+)\] ANOMALY CLEARED', txt)]
nopath = len(re.findall(r'No path from switch', txt))
n_normal = sum(1 for t in det if t_normal <= t < t_flood)
n_flood = sum(1 for t in det if t_flood <= t < t_end)
seqs = [int(x) for x in re.findall(r'icmp_seq=(\d+)', ping)]
lost, longest, prev = 0, 0, 0
for q in seqs:
    gap = q - prev - 1
    lost += max(0, gap)
    longest = max(longest, gap)
    prev = q
m = re.search(r'(\d+) packets transmitted, (\d+) received', ping)
print("\n=== anomaly check ===")
print(f"links excluded during NORMAL traffic (false positives): {n_normal}")
print(f"links excluded during the FLOOD:                      {n_flood}")
print(f"'No path' routing failures in controller log:          {nopath}")
print(f"ping h3->h7: {m.group(2) if m else '?'}/{m.group(1) if m else '?'} received, "
      f"longest run of lost pings: {longest} (= {longest*0.2:.1f} s)")
print(f"detections {len(det)}, clearances {len(clr)}")
