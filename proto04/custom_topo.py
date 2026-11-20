from mininet.topo import Topo

class MyTopo(Topo):
    def build(self):
        s1 = self.addSwitch("s1")

        h1 = self.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
        h2 = self.addHost("h2", ip="10.0.0.5", mac="00:00:00:00:00:05")
        h3 = self.addHost("h3", ip="10.0.0.10", mac="00:00:00:00:00:0A")
        self.addLink(h1, s1, port2=1)
        self.addLink(h2, s1, port2=2)
        self.addLink(h3, s1, port2=3)

topos = {
    "mytopo": MyTopo
}
