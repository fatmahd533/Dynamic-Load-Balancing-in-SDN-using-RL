"""
Scripted congestion test: h1 saturates the h1<->h5 path, then h2 starts
a second flow on the exact same switch pair (h2<->h6). Only pings the
two relevant pairs for ARP resolution (not all 56 host pairs) to keep
the controller log readable -- each packet_in now also prints which
flow it's deciding for (dst IP:port), so iperf traffic (port 5001) is
unmistakable from ICMP ping traffic in the log.

Run with: sudo python3 experiments/congestion_test.py
"""

import sys
import time

sys.path.insert(0, '.')

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel

from fattree_topo import FatTreeTopo


def main():
    setLogLevel('info')

    topo = FatTreeTopo()
    net = Mininet(topo=topo, link=TCLink,
                   controller=lambda name: RemoteController(
                       name, ip='127.0.0.1', port=6633))
    net.start()

    print("\n=== Waiting for controller to install initial table-miss rules ===")
    time.sleep(2)

    h1, h2, h5, h6 = net.get('h1', 'h2', 'h5', 'h6')

    print("\n=== Resolving ARP for the two test pairs only ===")
    h1.cmd('ping -c1 10.0.0.5')
    h2.cmd('ping -c1 10.0.0.6')
    time.sleep(11)  # let those ping-triggered flow entries idle-timeout out

    print("\n=== Starting iperf servers on h5, h6 ===")
    h5.cmd('iperf -s &')
    h6.cmd('iperf -s &')
    time.sleep(1)

    print("\n=== Starting congesting flow: h1 -> h5, 20s, targeting 8Mbps ===")
    h1.cmd('iperf -c 10.0.0.5 -t 20 -b 8M > /tmp/h1_iperf.log &')

    print("=== Waiting 6 real seconds for load to register in stats polling ===")
    time.sleep(6)

    print("\n=== Starting second flow: h2 -> h6, 8s, targeting 8Mbps ===")
    result = h2.cmd('iperf -c 10.0.0.6 -t 8 -b 8M')
    print(result)

    print("\n=== Waiting for background transfers to finish ===")
    time.sleep(15)

    print("\n=== h1's transfer result ===")
    print(h1.cmd('cat /tmp/h1_iperf.log'))

    print("\n=== Cleaning up ===")
    h1.cmd('kill %iperf')
    h5.cmd('kill %iperf')
    h6.cmd('kill %iperf')
    net.stop()


if __name__ == '__main__':
    main()
