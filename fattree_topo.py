"""
Small fat-tree topology for Dynamic-Load-Balancing-in-SDN-using-RL.
Switches now have explicit, unique dpids (Mininet's auto-assignment
extracts digits from switch names, which collides badly here -- e.g.
a1, c1, and e1 would all auto-get dpid 1 without this fix).

Every link is shaped to `bw` Mbps (default 10) so the fabric has a real
capacity limit; without it TCLink applies no shaping at all and no
algorithm can ever be shown to relieve congestion. Use
FatTreeTopo(bw=...) to change it, or bw=None to disable shaping.
"""

from mininet.topo import Topo


class FatTreeTopo(Topo):
    def build(self, bw=10):
        lk = {} if bw is None else {'bw': bw}
        a1 = self.addSwitch('a1', dpid='0000000000000001')
        a2 = self.addSwitch('a2', dpid='0000000000000002')
        a3 = self.addSwitch('a3', dpid='0000000000000003')
        a4 = self.addSwitch('a4', dpid='0000000000000004')
        c1 = self.addSwitch('c1', dpid='0000000000000005')
        c2 = self.addSwitch('c2', dpid='0000000000000006')
        e1 = self.addSwitch('e1', dpid='0000000000000007')
        e2 = self.addSwitch('e2', dpid='0000000000000008')
        e3 = self.addSwitch('e3', dpid='0000000000000009')
        e4 = self.addSwitch('e4', dpid='000000000000000a')

        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        h3 = self.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
        h4 = self.addHost('h4', ip='10.0.0.4/24', mac='00:00:00:00:00:04')
        h5 = self.addHost('h5', ip='10.0.0.5/24', mac='00:00:00:00:00:05')
        h6 = self.addHost('h6', ip='10.0.0.6/24', mac='00:00:00:00:00:06')
        h7 = self.addHost('h7', ip='10.0.0.7/24', mac='00:00:00:00:00:07')
        h8 = self.addHost('h8', ip='10.0.0.8/24', mac='00:00:00:00:00:08')

        self.addLink(h1, e1, **lk)
        self.addLink(h2, e1, **lk)
        self.addLink(h3, e2, **lk)
        self.addLink(h4, e2, **lk)
        self.addLink(h5, e3, **lk)
        self.addLink(h6, e3, **lk)
        self.addLink(h7, e4, **lk)
        self.addLink(h8, e4, **lk)

        self.addLink(e1, a1, **lk)
        self.addLink(e1, a2, **lk)
        self.addLink(e2, a1, **lk)
        self.addLink(e2, a2, **lk)
        self.addLink(e3, a3, **lk)
        self.addLink(e3, a4, **lk)
        self.addLink(e4, a3, **lk)
        self.addLink(e4, a4, **lk)

        self.addLink(a1, c1, **lk)
        self.addLink(a1, c2, **lk)
        self.addLink(a2, c1, **lk)
        self.addLink(a2, c2, **lk)
        self.addLink(a3, c1, **lk)
        self.addLink(a3, c2, **lk)
        self.addLink(a4, c1, **lk)
        self.addLink(a4, c2, **lk)


topos = {'fattree': (lambda: FatTreeTopo())}
