#!/usr/bin/python3
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import heapq

# ---------- Algoritmo de roteamento dinâmico (Dijkstra) ----------
class RouterAlgo:
    def __init__(self, name):
        self.name = name
        self.neighbors = {}
        self.routing_table = {}

    def add_neighbor(self, neighbor, cost=1):
        self.neighbors[neighbor] = cost

    def update_routing_table(self, network):
        dist = {r: float('inf') for r in network}
        prev = {r: None for r in network}
        dist[self.name] = 0
        pq = [(0, self.name)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, cost in network[u].neighbors.items():
                nd = d + cost
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        self.routing_table = {}
        for r in network:
            if r != self.name:
                hop = self._get_next_hop(prev, r)
                self.routing_table[r] = (dist[r], hop)

    def _get_next_hop(self, prev, target):
        hop = target
        while prev[hop] and prev[hop] != self.name:
            hop = prev[hop]
        return hop

# ---------- Topologia ----------
class ThreeRouterTopo(Topo):
    def build(self):
        # Roteadores (sem IP default do Mininet)
        r1 = self.addHost('r1', ip=None)
        r2 = self.addHost('r2', ip=None)
        r3 = self.addHost('r3', ip=None)

        # Switches OVS em modo standalone (sem controller)
        s1 = self.addSwitch('s1', failMode='standalone')  # h1 <-> r1
        s2 = self.addSwitch('s2', failMode='standalone')  # r1 <-> r2
        s3 = self.addSwitch('s3', failMode='standalone')  # h2 <-> r2
        s4 = self.addSwitch('s4', failMode='standalone')  # r2 <-> r3
        s5 = self.addSwitch('s5', failMode='standalone')  # h3 <-> r3

        # Hosts (sem IP default do Mininet)
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)
        h3 = self.addHost('h3', ip=None)

        # Ligações
        self.addLink(h1, s1); self.addLink(r1, s1)
        self.addLink(r1, s2); self.addLink(r2, s2)
        self.addLink(h2, s3); self.addLink(r2, s3)
        self.addLink(r2, s4); self.addLink(r3, s4)
        self.addLink(h3, s5); self.addLink(r3, s5)

def flush_and_set_ip(node, intf, cidr):
    # limpa qualquer IP anterior e sobe a interface com o CIDR desejado
    node.cmd(f'ip -4 addr flush dev {intf}')
    node.cmd(f'ip link set {intf} up')
    node.setIP(cidr, intf=intf)  # atualiza kernel + estado interno do Mininet

def run():
    info("🚀 Topologia com switches em standalone + roteamento dinâmico\n")
    topo = ThreeRouterTopo()
    net = Mininet(topo=topo, controller=None, autoSetMacs=True)
    net.start()

    # Nós
    h1, h2, h3 = net.get('h1', 'h2', 'h3')
    r1, r2, r3 = net.get('r1', 'r2', 'r3')

    # ---- IPs (flush + setIP + link up) ----
    flush_and_set_ip(h1, 'h1-eth0', '10.0.1.10/24')
    flush_and_set_ip(h2, 'h2-eth0', '10.0.2.10/24')
    flush_and_set_ip(h3, 'h3-eth0', '10.0.3.10/24')

    flush_and_set_ip(r1, 'r1-eth0', '10.0.1.1/24')     # h1
    flush_and_set_ip(r1, 'r1-eth1', '10.0.12.1/24')    # r2

    flush_and_set_ip(r2, 'r2-eth0', '10.0.12.2/24')    # r1
    flush_and_set_ip(r2, 'r2-eth1', '10.0.2.1/24')     # h2
    flush_and_set_ip(r2, 'r2-eth2', '10.0.23.1/24')    # r3

    flush_and_set_ip(r3, 'r3-eth0', '10.0.23.2/24')    # r2
    flush_and_set_ip(r3, 'r3-eth1', '10.0.3.1/24')     # h3

    # ---- IP forwarding nos roteadores ----
    for r in (r1, r2, r3):
        r.cmd('sysctl -w net.ipv4.ip_forward=1')

    # ---- Rotas default nos hosts ----
    for host, gw in [(h1,'10.0.1.1'), (h2,'10.0.2.1'), (h3,'10.0.3.1')]:
        host.cmd('ip route flush default || true')
        host.cmd(f'ip route add default via {gw}')

    # ---------- Roteamento dinâmico ----------
    routers = { "r1": RouterAlgo("r1"), "r2": RouterAlgo("r2"), "r3": RouterAlgo("r3") }
    routers["r1"].add_neighbor("r2", 1)
    routers["r2"].add_neighbor("r1", 1)
    routers["r2"].add_neighbor("r3", 1)
    routers["r3"].add_neighbor("r2", 1)

    for r in routers.values():
        r.update_routing_table(routers)

    # next-hop -> gateway IP (interfaces inter-roteadores)
    gw = {
        ("r1","r2"): "10.0.12.2",
        ("r2","r1"): "10.0.12.1",
        ("r2","r3"): "10.0.23.2",
        ("r3","r2"): "10.0.23.1",
    }
    host_nets = {"r1":"10.0.1.0/24", "r2":"10.0.2.0/24", "r3":"10.0.3.0/24"}

    # Limpa rotas estáticas antigas e aplica rotas dinâmicas para redes de hosts
    for rname in routers:
        rnode = net.get(rname)
        rnode.cmd('ip route flush proto static || true')
        for dest, (_cost, hop) in routers[rname].routing_table.items():
            if hop and (rname, hop) in gw:
                rnode.cmd(f'ip route replace {host_nets[dest]} via {gw[(rname, hop)]}')

    info("\n✅ Pronto! Teste no CLI:\n")
    info("   h1 ping h2   |   h1 ping h3   |   h2 ping h3\n\n")

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
