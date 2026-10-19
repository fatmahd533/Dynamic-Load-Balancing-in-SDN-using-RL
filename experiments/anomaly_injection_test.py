"""
Fires a sustained, extreme UDP flood (well beyond anything in this
project's prior tests -- 100Mbps declared target on a 10Mbps link,
forcing full line-rate saturation) on a specific host pair, to confirm
the trained Isolation Forest actually flags and excludes the link in
real time. Watch Terminal 1 (the controller) for "ANOMALY DETECTED"
and "Evicted flows" log lines while this runs.

Requires: experiments/train_anomaly_model.py already run, and the
controller restarted afterward so it picks up models/anomaly_model.joblib
(look for "Anomaly detection ENABLED" at controller startup).

Run with: sudo python3 experiments/anomaly_injection_test.py
"""

import sys
import time

sys.path.insert(0, '.')

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel

from fattree_topo import FatTreeTopo

FLOOD_SECONDS = 20
FLOOD_TARGET_MBPS = 100  # far beyond the 10Mbps link cap -- forces sustained saturation


def main():
    setLogLevel('info')

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

    print("\n=== Baseline: normal-looking traffic for 6s (should NOT trigger anything) ===")
    h5.cmd('iperf -u -s &')
    time.sleep(1)
    h1.cmd('iperf -u -c 10.0.0.5 -t 6 -b 2M > /dev/null 2>&1')

    print(f"\n=== FLOOD: {FLOOD_TARGET_MBPS}Mbps declared target for {FLOOD_SECONDS}s "
          f"(far beyond the 10Mbps link cap) ===")
    print("  Watch Terminal 1 now for 'ANOMALY DETECTED' / 'Evicted flows' lines...")
    h1.cmd(f'iperf -u -c 10.0.0.5 -t {FLOOD_SECONDS} -b {FLOOD_TARGET_MBPS}M > /dev/null 2>&1')

    print("\n=== Flood finished. Waiting 5s, then sending one more normal flow ===")
    time.sleep(5)
    result = h1.cmd('iperf -u -c 10.0.0.5 -t 5 -b 2M')
    print(result)
    print("Check Terminal 1: did this flow's path avoid the flagged link, "
          "or did 'ANOMALY CLEARED' appear (link stats returned to normal)?")

    print("\n=== Cleaning up ===")
    h1.cmd('pkill -9 -f iperf 2>/dev/null')
    h5.cmd('pkill -9 -f iperf 2>/dev/null')
    net.stop()


if __name__ == '__main__':
    main()
