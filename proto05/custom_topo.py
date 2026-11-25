"""Tutorial 5: one P4 (BMv2) switch, three Linux L2 bridges, 12 hosts.

The P4 switch s1 selects an egress port using IPv4 LPM.
The Linux bridges s2-s4 deliver Ethernet frames using MAC learning.

Run with the yutakayasuda/p4mn Mininet image using --custom and --topo.
No P4 source code changes or extra Open vSwitch processes are required.
"""

from mininet.node import Switch
from mininet.topo import Topo


class LearningBridge(Switch):
    """Kernel L2 learning bridge using `ip link` (not Open vSwitch)."""

    def start(self, controllers):
        # The P4 switch is started separately by the p4mn default switch class.
        # Linux automatically learns source MAC addresses on a bridge.
        self.cmd('ip link add name %s type bridge' % self.name)
        for intf in self.intfList():
            self.cmd('ip link set dev %s master %s' % (intf.name, self.name))
            self.cmd('ip link set dev %s up' % intf.name)
        self.cmd('ip link set dev %s up' % self.name)

    def stop(self, deleteIntfs=True):
        self.cmd('ip link del dev %s' % self.name)
        super().stop(deleteIntfs=deleteIntfs)


class LpmL2Topo(Topo):
    """s1: P4/BMv2; s2-s4: learning bridges; h1-h12: hosts."""

    def build(self):
        # The p4mn image provides BMv2 as its default switch class.
        s1 = self.addSwitch('s1')

        # Override the default for the three *non-P4* L2 switches.
        s2 = self.addSwitch('s2', cls=LearningBridge)
        s3 = self.addSwitch('s3', cls=LearningBridge)
        s4 = self.addSwitch('s4', cls=LearningBridge)

        # Pin s1's port numbers explicitly (P4Runtime actions use these).
        # The upstream interface on each L2 bridge is its port1.
        self.addLink(s1, s2, port1=1, port2=1)  # s1-eth1 -> h1-h3
        self.addLink(s1, s3, port1=2, port2=1)  # s1-eth2 -> h4-h7
        self.addLink(s1, s4, port1=3, port2=1)  # s1-eth3 -> h8-h11

        # Add each host explicitly, keeping one host per switch port.
        h1 = self.addHost('h1', ip='10.0.0.1')
        self.addLink(h1, s2, port1=0, port2=2)
        h2 = self.addHost('h2', ip='10.0.0.2')
        self.addLink(h2, s2, port1=0, port2=3)
        h3 = self.addHost('h3', ip='10.0.0.3')
        self.addLink(h3, s2, port1=0, port2=4)

        h4 = self.addHost('h4', ip='10.0.0.4')
        self.addLink(h4, s3, port1=0, port2=2)
        h5 = self.addHost('h5', ip='10.0.0.5')
        self.addLink(h5, s3, port1=0, port2=3)
        h6 = self.addHost('h6', ip='10.0.0.6')
        self.addLink(h6, s3, port1=0, port2=4)
        h7 = self.addHost('h7', ip='10.0.0.7')
        self.addLink(h7, s3, port1=0, port2=5)

        h8 = self.addHost('h8', ip='10.0.0.8')
        self.addLink(h8, s4, port1=0, port2=2)
        h9 = self.addHost('h9', ip='10.0.0.9')
        self.addLink(h9, s4, port1=0, port2=3)
        h10 = self.addHost('h10', ip='10.0.0.10')
        self.addLink(h10, s4, port1=0, port2=4)
        h11 = self.addHost('h11', ip='10.0.0.11')
        self.addLink(h11, s4, port1=0, port2=5)

        # The final host connects directly to s1's fourth port.
        h12 = self.addHost('h12', ip='10.0.0.12')
        self.addLink(h12, s1, port1=0, port2=4)


# Mininet `--custom custom_topo.py --topo lpm_l2` registration.
topos = {'lpm_l2': lambda: LpmL2Topo()}
