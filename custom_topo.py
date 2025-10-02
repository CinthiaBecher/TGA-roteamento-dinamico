#!/usr/bin/python3
# custom_topo.py — Topologia maior com múltiplos caminhos + seleção de algoritmo: RIP (DV) ou LB-DV
# Exemplos:
#   sudo python3 custom_topo.py --algo rip
#   sudo python3 custom_topo.py --algo lb --autotraffic --wload 8 --rounds 10 --disable-offload

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink

import argparse
from time import sleep

# -----------------------------
# Utilitários
# -----------------------------
def flush_and_set_ip(node, intf, cidr):
    node.cmd(f'ip -4 addr flush dev {intf}')
    node.cmd(f'ip link set {intf} up')
    node.setIP(cidr, intf=intf)

def ensure_host_default(host, gw):
    host.cmd('ip route flush default || true')
    host.cmd(f'ip route add default via {gw}')

def ip_forward_on(router):
    router.cmd('sysctl -w net.ipv4.ip_forward=1')

def disable_offload(node):
    for intf in node.intfs.values():
        name = str(intf)
        node.cmd(f'ethtool -K {name} gro off gso off tso off tx off rx off sg off || true')

def mk_ip_from_cidr(cidr, host_last_octet):
    ip, plen = cidr.split('/')
    a,b,c,_ = map(int, ip.split('.'))
    return f"{a}.{b}.{c}.{host_last_octet}/{plen}"

def mk_plain_ip_from_cidr(cidr, host_last_octet):
    ip, _ = cidr.split('/')
    a,b,c,_ = map(int, ip.split('.'))
    return f"{a}.{b}.{c}.{host_last_octet}"


# -----------------------------
# RIP (DV hop-count)
# -----------------------------
INFINITY = 16

class RIPNode:
    """RIP clássico (saltos) com split horizon + poison reverse."""
    def __init__(self, name):
        self.name = name
        self.table = {}            # net -> (cost_hops, nextHopRouter or None)
        self.direct_nets = set()
        self.msgs_sent = 0
        self.msgs_recv = 0

    def add_direct(self, net):
        self.table[net] = (0, None)
        self.direct_nets.add(net)

    def build_update_for_neighbor(self, neighbor_name):
        upd = {}
        for net, (cost, nh) in self.table.items():
            if nh == neighbor_name:
                upd[net] = INFINITY
            else:
                adv = cost + 1 if cost < INFINITY else INFINITY
                upd[net] = adv if adv <= INFINITY else INFINITY
        return upd

    def process_update(self, from_neighbor, update_dict):
        changed = False
        for net, recv_cost in update_dict.items():
            if recv_cost >= INFINITY:
                if net in self.table and self.table[net][1] == from_neighbor and self.table[net][0] != INFINITY:
                    self.table[net] = (INFINITY, self.table[net][1])
                    changed = True
                continue
            new_cost = min(recv_cost, INFINITY)
            if net not in self.table:
                self.table[net] = (new_cost, from_neighbor)
                changed = True
            else:
                cur_cost, cur_nh = self.table[net]
                if new_cost < cur_cost:
                    self.table[net] = (new_cost, from_neighbor)
                    changed = True
        return changed

def rip_converge(routers, neighbors, rounds=8):
    stats = {'updates_sent': 0, 'entries_sent': 0}
    for _ in range(rounds):
        outgoing = []
        for rname, node in routers.items():
            for n in neighbors[rname]:
                upd = node.build_update_for_neighbor(n)
                node.msgs_sent += 1
                stats['updates_sent'] += 1
                stats['entries_sent'] += len(upd)
                outgoing.append((rname, n, upd))
        any_change = False
        for frm, to, upd in outgoing:
            routers[to].msgs_recv += 1
            changed = routers[to].process_update(frm, upd)
            any_change = any_change or changed
        if not any_change:
            break
    return stats

# -----------------------------
# LB-DV (sensível à carga)
# -----------------------------
LB_INF = 10**6

class LBDVNode:
    """DV com custo de link c(e) = 1 + w_load * U(e) + split horizon + poison reverse"""
    def __init__(self, name):
        self.name = name
        self.table = {}            # net -> (cost_float, nextHopRouter or None)
        self.direct_nets = set()
        self.msgs_sent = 0
        self.msgs_recv = 0

    def add_direct(self, net):
        self.table[net] = (0.0, None)
        self.direct_nets.add(net)

    def build_update_for_neighbor(self, neighbor_name):
        upd = {}
        for net, (cost, nh) in self.table.items():
            upd[net] = LB_INF if nh == neighbor_name else cost
        return upd

    def process_update(self, from_neighbor, update_dict, link_cost_to_neighbor):
        changed = False
        for net, recv_cost in update_dict.items():
            if recv_cost >= LB_INF:
                if net in self.table and self.table[net][1] == from_neighbor and self.table[net][0] < LB_INF:
                    self.table[net] = (LB_INF, self.table[net][1])
                    changed = True
                continue
            new_cost = recv_cost + link_cost_to_neighbor
            if net not in self.table:
                self.table[net] = (new_cost, from_neighbor)
                changed = True
            else:
                cur_cost, cur_nh = self.table[net]
                if new_cost + 1e-9 < cur_cost:
                    self.table[net] = (new_cost, from_neighbor)
                    changed = True
        return changed

def read_bytes(node, intf):
    try:
        rx = node.cmd(f'cat /sys/class/net/{intf}/statistics/rx_bytes').strip()
        tx = node.cmd(f'cat /sys/class/net/{intf}/statistics/tx_bytes').strip()
        return int(rx or 0), int(tx or 0)
    except Exception:
        return 0, 0

def measure_utilization(net, router_name, intf, bw_mbps, interval):
    node = net.get(router_name)
    rx1, tx1 = read_bytes(node, intf)
    sleep(interval)
    rx2, tx2 = read_bytes(node, intf)
    bps = ((rx2 - rx1) + (tx2 - tx1)) * 8.0 / max(interval, 1e-6)
    cap_bps = bw_mbps * 1e6
    U = max(0.0, min(bps / cap_bps, 1.0)) if cap_bps > 0 else 0.0
    return U

def lb_converge(net, routers, neighbors, neighbor_intf, link_bw_mbps, w_load=4.0, interval=0.25, rounds=8):
    stats = {'updates_sent': 0, 'entries_sent': 0}
    for _ in range(rounds):
        # custo local por link (direcionado)
        link_cost = {}
        for (rname, neigh), intf in neighbor_intf.items():
            bw = link_bw_mbps[(rname, neigh)]
            U = measure_utilization(net, rname, intf, bw, interval)
            link_cost[(rname, neigh)] = 1.0 + w_load * U

        outgoing = []
        for rname, node in routers.items():
            for n in neighbors[rname]:
                upd = node.build_update_for_neighbor(n)
                node.msgs_sent += 1
                stats['updates_sent'] += 1
                stats['entries_sent'] += len(upd)
                outgoing.append((rname, n, upd))

        any_change = False
        for frm, to, upd in outgoing:
            routers[to].msgs_recv += 1
            changed = routers[to].process_update(frm, upd, link_cost[(to, frm)])
            any_change = any_change or changed

        if not any_change:
            break
    return stats

# -----------------------------
# Topologia MAIOR com múltiplos caminhos
# -----------------------------
class MultiPathTopo(Topo):
    """
    Roteadores: r1..r5 ; Hosts: h1 (r1), h2 (r3), h3 (r5)
    Interligação:
      r1—r2  (10.0.12.0/24, bw=5)
      r2—r3  (10.0.23.0/24, bw=10)
      r1—r4  (10.0.14.0/24, bw=10)
      r4—r3  (10.0.43.0/24, bw=10)
      r2—r4  (10.0.24.0/24, bw=8)
      r4—r5  (10.0.45.0/24, bw=10)
      r3—r5  (10.0.35.0/24, bw=8)
    """
    def __init__(self):
        # 👇 defina a lista ANTES do __init__ da classe base
        self.link_specs = [
            ('r1','r2','10.0.12.0/24', 5),
            ('r2','r3','10.0.23.0/24',10),
            ('r1','r4','10.0.14.0/24',10),
            ('r4','r3','10.0.43.0/24',10),
            ('r2','r4','10.0.24.0/24', 8),
            ('r4','r5','10.0.45.0/24',10),
            ('r3','r5','10.0.35.0/24', 8),
        ]
        # só depois chame o __init__ do Topo (ele invoca build())
        super(MultiPathTopo, self).__init__()

    def build(self):
        # Roteadores/Hosts
        r1 = self.addHost('r1', ip=None)
        r2 = self.addHost('r2', ip=None)
        r3 = self.addHost('r3', ip=None)
        r4 = self.addHost('r4', ip=None)
        r5 = self.addHost('r5', ip=None)

        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)
        h3 = self.addHost('h3', ip=None)

        # Switches host<->router
        sH1 = self.addSwitch('sH1', failMode='standalone')
        sH2 = self.addSwitch('sH2', failMode='standalone')
        sH3 = self.addSwitch('sH3', failMode='standalone')
        self.addLink(h1, sH1, bw=100); self.addLink(r1, sH1, bw=100)
        self.addLink(h2, sH2, bw=100); self.addLink(r3, sH2, bw=100)
        self.addLink(h3, sH3, bw=100); self.addLink(r5, sH3, bw=100)

        # Inter-roteadores: um switch por link
        for i, (ra, rb, cidr, bw) in enumerate(self.link_specs, start=1):
            sL = self.addSwitch(f'sL{i}', failMode='standalone')
            self.addLink(ra, sL, bw=bw)
            self.addLink(rb, sL, bw=bw)

# -----------------------------
# MAIN
# -----------------------------
def main():
    parser = argparse.ArgumentParser(description="Topologia maior com múltiplos caminhos + RIP ou LB-DV")
    parser.add_argument('--algo', choices=['rip','lb'], default='rip', help='Algoritmo de roteamento')
    parser.add_argument('--rounds', type=int, default=10, help='Rodadas de troca de mensagens')
    parser.add_argument('--wload', type=float, default=6.0, help='Peso da carga (LB-DV)')
    parser.add_argument('--interval', type=float, default=0.25, help='Janela de medição (LB-DV, seg)')
    parser.add_argument('--autotraffic', action='store_true', help='Gera tráfego h1->h2 durante convergência')
    parser.add_argument('--disable-offload', action='store_true', help='Desativa GRO/TSO/GSO nas NICs')
    args = parser.parse_args()

    info(f"🚀 Subindo topologia grande (algo={args.algo})\n")
    topo = MultiPathTopo()
    net = Mininet(topo=topo, controller=None, autoSetMacs=True, link=TCLink)
    net.start()

    # Nós
    h1,h2,h3 = net.get('h1','h2','h3')
    r1,r2,r3,r4,r5 = net.get('r1','r2','r3','r4','r5')

    if args.disable_offload:
        for n in (h1,h2,h3,r1,r2,r3,r4,r5):
            disable_offload(n)

    # ----- IPs host<->roteador -----
    # Descobre interface dos roteadores conectadas aos switches de host:
    def intf_to_switch(node, swname):
        sw = net.get(swname)
        con = node.connectionsTo(sw)
        return str(con[0][0])  # nome da intf no node

    # h1<->r1 : 10.0.1.0/24
    r1_h_intf = intf_to_switch(r1,'sH1')
    flush_and_set_ip(r1, r1_h_intf, '10.0.1.1/24')
    flush_and_set_ip(h1,'h1-eth0','10.0.1.10/24')
    ensure_host_default(h1,'10.0.1.1')

    # h2<->r3 : 10.0.2.0/24
    r3_h_intf = intf_to_switch(r3,'sH2')
    flush_and_set_ip(r3, r3_h_intf, '10.0.2.1/24')
    flush_and_set_ip(h2,'h2-eth0','10.0.2.10/24')
    ensure_host_default(h2,'10.0.2.1')

    # h3<->r5 : 10.0.3.0/24
    r5_h_intf = intf_to_switch(r5,'sH3')
    flush_and_set_ip(r5, r5_h_intf, '10.0.3.1/24')
    flush_and_set_ip(h3,'h3-eth0','10.0.3.10/24')
    ensure_host_default(h3,'10.0.3.1')

    # IP forwarding nos roteadores
    for r in (r1,r2,r3,r4,r5):
        ip_forward_on(r)

    # ----- Inter-roteadores: configurar IPs, vizinhança e metadados -----
    # link_specs deve ser igual ao usado dentro do topo
    link_specs = topo.link_specs  # [(ra,rb,cidr,bw), ...]
    neighbors = { 'r1':[], 'r2':[], 'r3':[], 'r4':[], 'r5':[] }
    neighbor_gw_ip = {}
    neighbor_intf = {}
    link_bw_mbps = {}

    for i,(ra,rb,cidr,bw) in enumerate(link_specs, start=1):
        sw = net.get(f'sL{i}')
        # interfaces reais nos roteadores ra e rb ligadas a sL{i}
        rai = str(net.get(ra).connectionsTo(sw)[0][0])
        rbi = str(net.get(rb).connectionsTo(sw)[0][0])

        # atribui .1 para 'ra' e .2 para 'rb' nesse /24
        ipA = mk_ip_from_cidr(cidr, 1)
        ipB = mk_ip_from_cidr(cidr, 2)
        flush_and_set_ip(net.get(ra), rai, ipA)
        flush_and_set_ip(net.get(rb), rbi, ipB)

        # vizinhança (não-direcionado)
        neighbors[ra].append(rb)
        neighbors[rb].append(ra)

        # mapeamentos direcionados: gw, intf local e bw
        neighbor_gw_ip[(ra,rb)] = mk_plain_ip_from_cidr(cidr, 2)
        neighbor_gw_ip[(rb,ra)] = mk_plain_ip_from_cidr(cidr, 1)
        neighbor_intf[(ra,rb)] = rai
        neighbor_intf[(rb,ra)] = rbi
        link_bw_mbps[(ra,rb)] = bw
        link_bw_mbps[(rb,ra)] = bw

    # Redes de hosts (destinos de interesse)
    host_nets = {
        'r1': '10.0.1.0/24',   # h1
        'r3': '10.0.2.0/24',   # h2
        'r5': '10.0.3.0/24',   # h3
    }

    # ----- Seleção e execução do algoritmo -----
    if args.algo == 'rip':
        rip = { rn: RIPNode(rn) for rn in ['r1','r2','r3','r4','r5'] }
        # diretas (somente redes com hosts)
        rip['r1'].add_direct(host_nets['r1'])
        rip['r3'].add_direct(host_nets['r3'])
        rip['r5'].add_direct(host_nets['r5'])

        stats = rip_converge(rip, neighbors, rounds=args.rounds)

        info("\n📋 Tabelas RIP (net -> (saltos, nextHop))\n")
        for rn in ['r1','r2','r3','r4','r5']:
            info(f"  {rn}:\n")
            for net_dst, (cost, nh) in sorted(rip[rn].table.items()):
                info(f"    {net_dst:>14} -> (cost={cost}, nextHop={nh})\n")
        info(f"\n📈 RIP stats: updates={stats['updates_sent']}, entradas={stats['entries_sent']}\n")

        # instalar rotas (apenas redes de hosts não diretas)
        for rn in ['r1','r2','r3','r4','r5']:
            rnode = net.get(rn)
            rnode.cmd('ip route flush proto static || true')
            for net_dst, (cost, nh) in rip[rn].table.items():
                if cost == 0 or cost >= INFINITY or nh is None:
                    continue
                gw_ip = neighbor_gw_ip.get((rn, nh))
                if gw_ip:
                    rnode.cmd(f'ip route replace {net_dst} via {gw_ip}')

    else:  # LB-DV
        lb = { rn: LBDVNode(rn) for rn in ['r1','r2','r3','r4','r5'] }
        lb['r1'].add_direct(host_nets['r1'])
        lb['r3'].add_direct(host_nets['r3'])
        lb['r5'].add_direct(host_nets['r5'])

        # tráfego durante a convergência
        if args.autotraffic:
            h2.cmd('pkill -9 iperf3 || true')
            h2.cmd('iperf3 -s -D')
            h1.cmd('iperf3 -c 10.0.2.10 -t 14 -i 1 >/tmp/iperf_h1_h2.txt 2>&1 &')

        # AQUECER o enlace r1<->r2 para que a medição pegue carga real
        r2.cmd('pkill -9 iperf3 || true'); r2.cmd('iperf3 -s -D')
        r1.cmd('iperf3 -c 10.0.12.2 -t 12 -i 1 >/tmp/iperf_r1_r2.txt 2>&1 &')
        sleep(1)  # dá 1s para o fluxo estabilizar


        stats = lb_converge(
            net, lb, neighbors,
            neighbor_intf=neighbor_intf,
            link_bw_mbps=link_bw_mbps,
            w_load=args.wload,
            interval=args.interval,
            rounds=args.rounds
        )

        info("\n📋 Tabelas LB-DV (net -> (custo, nextHop))\n")
        for rn in ['r1','r2','r3','r4','r5']:
            info(f"  {rn}:\n")
            for net_dst, (cost, nh) in sorted(lb[rn].table.items()):
                cdisp = f"{cost:.3f}" if cost < LB_INF else "inf"
                info(f"    {net_dst:>14} -> (cost={cdisp}, nextHop={nh})\n")
        info(f"\n📈 LB stats: updates={stats['updates_sent']}, entradas={stats['entries_sent']}, w_load={args.wload}\n")

        # instalar rotas
        for rn in ['r1','r2','r3','r4','r5']:
            rnode = net.get(rn)
            rnode.cmd('ip route flush proto static || true')
            for net_dst, (cost, nh) in lb[rn].table.items():
                if cost == 0 or cost >= LB_INF or nh is None:
                    continue
                gw_ip = neighbor_gw_ip.get((rn, nh))
                if gw_ip:
                    rnode.cmd(f'ip route replace {net_dst} via {gw_ip}')

    info("\n✅ Rotas aplicadas. Teste no CLI:\n")
    info("   h1 ping h2   |   h1 traceroute -n 10.0.2.10   |   r1 ip route | grep 10.0.2.0\n\n")

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    main()
