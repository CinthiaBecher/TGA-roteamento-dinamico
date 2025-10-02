#!/usr/bin/python3
# custom_topo.py - Mininet + seleção de algoritmo: RIP (DV) ou DV sensível a carga (LB)
# Uso:
#   sudo python3 custom_topo.py --algo rip
#   sudo python3 custom_topo.py --algo lb --wload 4.0 --rounds 8 --interval 0.25

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink

import argparse
from time import sleep

# =========================
#   TOPOLOGIA
# =========================

class ThreeRouterTopo(Topo):
    """
    Topologia:
      h1—s1—r1—s2—r2—s4—r3—s5—h3
                 |
                 s3—h2

    Sub-redes:
      h1<->r1 : 10.0.1.0/24   (r1-eth0, h1-eth0)
      r1<->r2 : 10.0.12.0/24  (r1-eth1, r2-eth0)
      h2<->r2 : 10.0.2.0/24   (r2-eth1, h2-eth0)
      r2<->r3 : 10.0.23.0/24  (r2-eth2, r3-eth0)
      h3<->r3 : 10.0.3.0/24   (r3-eth1, h3-eth0)
    """
    def build(self):
        # Roteadores/Hosts sem IP default do Mininet
        r1 = self.addHost('r1', ip=None)
        r2 = self.addHost('r2', ip=None)
        r3 = self.addHost('r3', ip=None)

        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)
        h3 = self.addHost('h3', ip=None)

        # Switches OVS em modo standalone (sem controller)
        s1 = self.addSwitch('s1', failMode='standalone')  # h1<->r1
        s2 = self.addSwitch('s2', failMode='standalone')  # r1<->r2
        s3 = self.addSwitch('s3', failMode='standalone')  # h2<->r2
        s4 = self.addSwitch('s4', failMode='standalone')  # r2<->r3
        s5 = self.addSwitch('s5', failMode='standalone')  # h3<->r3

        # Links com TC (para ter noção de capacidade); host<->roteador mais largo
        self.addLink(h1, s1, bw=100); self.addLink(r1, s1, bw=100)
        self.addLink(r1, s2, bw=10);  self.addLink(r2, s2, bw=10)
        self.addLink(h2, s3, bw=100); self.addLink(r2, s3, bw=100)
        self.addLink(r2, s4, bw=10);  self.addLink(r3, s4, bw=10)
        self.addLink(h3, s5, bw=100); self.addLink(r3, s5, bw=100)

# =========================
#   UTILITÁRIOS DE REDE
# =========================

def flush_and_set_ip(node, intf, cidr):
    node.cmd(f'ip -4 addr flush dev {intf}')
    node.cmd(f'ip link set {intf} up')
    node.setIP(cidr, intf=intf)

def ensure_host_default(host, gw):
    host.cmd('ip route flush default || true')
    host.cmd(f'ip route add default via {gw}')

def ip_forward_on(router):
    router.cmd('sysctl -w net.ipv4.ip_forward=1')

# =========================
#   RIP (DV hop-count) Didático
# =========================

INFINITY = 16  # padrão RIP

class RIPNode:
    """RIP clássico (distância em saltos) com split horizon + poison reverse."""
    def __init__(self, name):
        self.name = name
        self.table = {}            # net -> (cost, nextHopRouter or None)
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

def rip_converge(routers, neighbors, rounds=5):
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

# =========================
#   DV sensível à carga (LB)
# =========================

LB_INF = 10**6  # "infinito" grande

class LBDVNode:
    """
    DV com métrica de link: c(e) = 1 + w_load * U(e)
    - U(e): utilização [0..1] (lida via bytes/s / capacidade)
    - split horizon + poison reverse
    Tabela guarda custo acumulado e próximo salto.
    """
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
            if nh == neighbor_name:
                upd[net] = LB_INF
            else:
                upd[net] = cost  # neste DV, quem recebe somará o custo do link local
        return upd

    def process_update(self, from_neighbor, update_dict, link_cost_to_neighbor):
        """
        link_cost_to_neighbor: custo do link local para 'from_neighbor'
        """
        changed = False
        for net, recv_cost in update_dict.items():
            if recv_cost >= LB_INF:
                # poison reverse recebido: invalida se dependia desse vizinho
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
    """Retorna U in [0..1] medindo bytes da interface por 'interval' segundos."""
    node = net.get(router_name)
    rx1, tx1 = read_bytes(node, intf)
    sleep(interval)
    rx2, tx2 = read_bytes(node, intf)
    bps = ((rx2 - rx1) + (tx2 - tx1)) * 8.0 / max(interval, 1e-6)
    cap_bps = bw_mbps * 1e6
    U = max(0.0, min(bps / cap_bps, 1.0)) if cap_bps > 0 else 0.0
    return U

def lb_converge(net, routers, neighbors, neighbor_intf, link_bw_mbps, w_load=4.0, interval=0.25, rounds=6):
    """
    Convergência do DV sensível à carga.
    Em cada rodada:
      - mede U(link) por interface local
      - define custo do link: 1 + w_load * U
      - troca updates e recalcula tabelas
    """
    stats = {'updates_sent': 0, 'entries_sent': 0}
    for _ in range(rounds):
        # medir custo por link local (direcionado)
        link_cost = {}
        for (rname, neigh) in neighbor_intf.keys():
            intf = neighbor_intf[(rname, neigh)]
            bw = link_bw_mbps[(rname, neigh)]
            U = measure_utilization(net, rname, intf, bw, interval)
            link_cost[(rname, neigh)] = 1.0 + w_load * U

        # enviar atualizações
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
            cost_ln = link_cost[(to, frm)]
            changed = routers[to].process_update(frm, upd, cost_ln)
            any_change = any_change or changed

        if not any_change:
            break
    return stats

# =========================
#   MAIN / EXECUÇÃO
# =========================

def main():
    parser = argparse.ArgumentParser(description="Mininet + Roteamento (RIP ou DV sensível à carga)")
    parser.add_argument('--algo', choices=['rip', 'lb'], default='rip', help='Algoritmo de roteamento')
    parser.add_argument('--rounds', type=int, default=8, help='Rodadas de troca de mensagens')
    parser.add_argument('--wload', type=float, default=4.0, help='Peso da carga no custo do link (LB)')
    parser.add_argument('--interval', type=float, default=0.25, help='Janela de medição de utilização (seg)')
    args = parser.parse_args()

    info(f"🚀 Subindo topologia (algo={args.algo})\n")
    topo = ThreeRouterTopo()
    net = Mininet(topo=topo, controller=None, autoSetMacs=True, link=TCLink)
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
        ip_forward_on(r)

    # ---- Rotas default nos hosts ----
    ensure_host_default(h1, '10.0.1.1')
    ensure_host_default(h2, '10.0.2.1')
    ensure_host_default(h3, '10.0.3.1')

    # ======= Definições comuns aos dois algoritmos =======

    host_nets = {            # redes dos hosts
        'r1': '10.0.1.0/24',
        'r2': '10.0.2.0/24',
        'r3': '10.0.3.0/24',
    }
    neighbors = {            # vizinhanças entre ROTEADORES
        'r1': ['r2'],
        'r2': ['r1', 'r3'],
        'r3': ['r2'],
    }
    # gateway IP para alcançar o vizinho (IP do vizinho na rede inter-roteadores)
    neighbor_gw_ip = {
        ('r1', 'r2'): '10.0.12.2',
        ('r2', 'r1'): '10.0.12.1',
        ('r2', 'r3'): '10.0.23.2',
        ('r3', 'r2'): '10.0.23.1',
    }
    # interface local para alcançar o vizinho (para medição LB)
    neighbor_intf = {
        ('r1', 'r2'): 'r1-eth1',
        ('r2', 'r1'): 'r2-eth0',
        ('r2', 'r3'): 'r2-eth2',
        ('r3', 'r2'): 'r3-eth0',
    }
    # largura de banda (Mbps) dos links inter-roteadores (bates com addLink bw=10)
    link_bw_mbps = {
        ('r1', 'r2'): 10,
        ('r2', 'r1'): 10,
        ('r2', 'r3'): 10,
        ('r3', 'r2'): 10,
    }

    # ======= Seleção e execução do algoritmo =======

    if args.algo == 'rip':
        rip = {'r1': RIPNode('r1'), 'r2': RIPNode('r2'), 'r3': RIPNode('r3')}
        rip['r1'].add_direct(host_nets['r1'])
        rip['r2'].add_direct(host_nets['r2'])
        rip['r3'].add_direct(host_nets['r3'])

        stats = rip_converge(rip, neighbors, rounds=args.rounds)

        info("\n📋 Tabelas RIP (net -> (custo_em_saltos, nextHop))\n")
        for rn in ['r1', 'r2', 'r3']:
            info(f"  {rn}:\n")
            for net_dst, (cost, nh) in sorted(rip[rn].table.items()):
                info(f"    {net_dst:>14} -> (cost={cost}, nextHop={nh})\n")
        info(f"\n📈 RIP stats: updates={stats['updates_sent']}, entradas_anunciadas={stats['entries_sent']}\n")

        # Instalar rotas
        for rn in ['r1', 'r2', 'r3']:
            rnode = net.get(rn)
            rnode.cmd('ip route flush proto static || true')
            for net_dst, (cost, nh) in rip[rn].table.items():
                if cost == 0 or cost >= INFINITY or nh is None:
                    continue
                gw_ip = neighbor_gw_ip.get((rn, nh))
                if gw_ip:
                    rnode.cmd(f'ip route replace {net_dst} via {gw_ip}')

    else:  # args.algo == 'lb'
        lb = {'r1': LBDVNode('r1'), 'r2': LBDVNode('r2'), 'r3': LBDVNode('r3')}
        lb['r1'].add_direct(host_nets['r1'])
        lb['r2'].add_direct(host_nets['r2'])
        lb['r3'].add_direct(host_nets['r3'])

        stats = lb_converge(
            net, lb, neighbors,
            neighbor_intf=neighbor_intf,
            link_bw_mbps=link_bw_mbps,
            w_load=args.wload,
            interval=args.interval,
            rounds=args.rounds
        )

        info("\n📋 Tabelas LB-DV (net -> (custo_composto, nextHop))\n")
        for rn in ['r1', 'r2', 'r3']:
            info(f"  {rn}:\n")
            for net_dst, (cost, nh) in sorted(lb[rn].table.items()):
                cdisp = f"{cost:.3f}" if cost < LB_INF else "inf"
                info(f"    {net_dst:>14} -> (cost={cdisp}, nextHop={nh})\n")
        info(f"\n📈 LB stats: updates={stats['updates_sent']}, entradas_anunciadas={stats['entries_sent']}, w_load={args.wload}\n")

        # Instalar rotas
        for rn in ['r1', 'r2', 'r3']:
            rnode = net.get(rn)
            rnode.cmd('ip route flush proto static || true')
            for net_dst, (cost, nh) in lb[rn].table.items():
                if cost == 0 or cost >= LB_INF or nh is None:
                    continue
                gw_ip = neighbor_gw_ip.get((rn, nh))
                if gw_ip:
                    rnode.cmd(f'ip route replace {net_dst} via {gw_ip}')

    info("\n✅ Rotas aplicadas. Teste no CLI:\n")
    info("   h1 ping h2   |   h1 ping h3   |   h2 ping h3\n")
    info("   traceroute/tracepath também funcionam.\n\n")

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    main()
