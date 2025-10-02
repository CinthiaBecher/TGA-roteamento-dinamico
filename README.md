# TGA – Roteamento Dinâmico em Mininet (RIP e LB-DV)

Este projeto implementa e demonstra **roteamento dinâmico** em uma topologia virtual construída no **Mininet**, com **dois algoritmos** à escolha na execução:

- **RIP (Distance Vector clássico)** com *split horizon + poison reverse*;
- **LB-DV (Distance Vector sensível à carga)**, que **equilibra tráfego** considerando a **utilização dos enlaces** durante a convergência, com custo `c(e) = 1 + w_load · U(e)`.

Tudo está em **um único arquivo** (`custom_topo.py`) para facilitar execução e demonstração.

---

## 1) Topologia

Topologia **base** (sempre presente):

```
h1—s1—r1—s2—r2—s4—r3—s5—h3
           |
           s3—h2
```

Sub-redes usadas:
- `h1<->r1`: `10.0.1.0/24` (r1-eth0, h1-eth0)
- `r1<->r2`: `10.0.12.0/24` (r1-eth1, r2-eth0)
- `h2<->r2`: `10.0.2.0/24` (r2-eth1, h2-eth0)
- `r2<->r3`: `10.0.23.0/24` (r2-eth2, r3-eth0)
- `h3<->r3`: `10.0.3.0/24` (r3-eth1, h3-eth0)

Opcional (**`--altlink`**): cria **caminho alternativo** entre `r1` e `r3` via `10.0.13.0/24`:
```
r1—s6—r3
```

**Observação sobre switches:** todos os switches OVS sobem em **`failMode=standalone`** (não precisam de controller OpenFlow).

---

## 2) O que o programa faz

Ao executar `custom_topo.py`, o script:

1. **Sobe a topologia** (Mininet + OVS em standalone);
2. **Configura IPs** em hosts e roteadores, ativa **IP forwarding** nos roteadores e define **rota default** nos hosts;
3. **Executa** o algoritmo selecionado (**RIP** ou **LB-DV**), troca mensagens por algumas **rodadas**, **converge** e **instala rotas** nos roteadores;
4. Abre o **CLI do Mininet** para você testar (`h1 ping h2`, `traceroute`, etc.).

---

## 3) Requisitos da VM (Ubuntu)

- **Ubuntu** (com interface gráfica opcional; funciona em terminal);
- **Python 3** e privilégios de **sudo**;
- **Mininet** e dependências (inclui **Open vSwitch**):
  ```bash
  sudo apt-get update
  sudo apt-get install -y mininet
  ```
- Ferramentas de teste e utilitários:
  ```bash
  sudo apt-get install -y iperf3 traceroute iputils-tracepath ethtool net-tools mtr-tiny
  ```
  - `iperf3` → gerar tráfego;
  - `traceroute`/`tracepath` → ver caminho;
  - `ethtool` → (opcional) desativar offloads que atrapalham limitação de banda em VMs;
  - `mtr-tiny` (opcional) → traceroute interativo.

> **Observação sobre VMs (UTM/Virtualização):** Se perceber taxas irreais em `iperf3`, use o parâmetro `--disable-offload` na execução para o script desativar GRO/GSO/TSO e similares nas interfaces virtuais. Isso ajuda a respeitar `bw=` dos links TC.  

---

## 4) Como rodar


```bash
sudo python3 custom_topo.py --algo rip
# ou
sudo python3 custom_topo.py --algo lb --rounds 10 --wload 8 --interval 0.25
```

**Parâmetros disponíveis:**
- `--algo {rip|lb}`: seleciona o algoritmo (padrão: `rip`);
- `--rounds N`: número de rondas de troca de mensagens entre roteadores (padrão: `8`);
- `--wload X`: peso da carga na métrica do link **LB-DV** (padrão: `4.0`);
- `--interval T`: janela de medição (segundos) de utilização no **LB-DV** (padrão: `0.25`);
- `--altlink`: adiciona caminho alternativo `r1<->r3 (10.0.13.0/24)`, necessário para o **LB-DV** exibir **desvio de rota**;
- `--autotraffic`: injeta tráfego `h1 -> h2` com `iperf3` **durante a convergência** do **LB-DV**;
- `--disable-offload`: desativa offloads de NIC (GRO/GSO/TSO, etc.) em todas as interfaces.

Exemplos úteis:

```bash
# 1) RIP (baseline)
sudo python3 custom_topo.py --algo rip

# 2) LB-DV com caminho alternativo + tráfego durante convergência
sudo python3 custom_topo.py --algo lb --altlink --autotraffic --wload 8 --rounds 10 --disable-offload
```

---

## 5) Como usar o CLI do Mininet (comandos importantes)

> No `mininet>`, **prefixe o comando** com o nome do nó (ex.: `h1`, `r1`).

Testes básicos:
```bash
mininet> h1 ping h2
mininet> h1 ping h3
mininet> h2 ping h3
```

Caminho percorrido (instale `traceroute`/`tracepath` antes):
```bash
mininet> h1 traceroute -n 10.0.2.10
# ou
mininet> h1 tracepath 10.0.2.10
```

Ver as **rotas instaladas** pelos algoritmos:
```bash
mininet> r1 ip route
mininet> r2 ip route
mininet> r3 ip route
```

Gerar tráfego manualmente:
```bash
mininet> h2 iperf3 -s -D               # servidor (background)
mininet> h1 iperf3 -c 10.0.2.10 -t 20  # cliente (20s)
```

Inspecionar IPs/links:
```bash
mininet> dump
mininet> r1 ip -4 addr show
mininet> r2 ip -4 addr show
mininet> r3 ip -4 addr show
```

Sair do CLI:
```bash
mininet> exit
```

---

## 6) Como testar os algoritmos

### 6.1) RIP (baseline em saltos)
1. Execute:
   ```bash
   sudo python3 custom_topo.py --algo rip
   ```
2. No `mininet>`:
   ```bash
   h1 ping h2
   h1 ping h3
   r1 ip route
   r2 ip route
   r3 ip route
   ```
   Você verá rotas com **próximo salto** apontando para os vizinhos esperados em menor número de saltos.

### 6.2) LB-DV (sensível à carga)
1. Execute com **caminho alternativo** + **tráfego durante convergência**:
   ```bash
   sudo python3 custom_topo.py --algo lb --altlink --autotraffic --wload 8 --rounds 10 --disable-offload
   ```
2. No `mininet>`:
   ```bash
   # Ver rotas resultantes
   r1 ip route | grep 10.0.2.0
   r3 ip route | grep 10.0.2.0

   # Ver caminho do h1 até h2
   h1 traceroute -n 10.0.2.10
   # ou
   h1 tracepath 10.0.2.10
   ```
   - Se o link `r1<->r2` estiver **carregado**, o **LB-DV** tende a **desviar via r3** quando `--altlink` está ativo (ex.: `10.0.2.0/24 via 10.0.13.2`).  
   - Ajuste `--wload` (maior ⇒ mais sensível à carga) e `--rounds` para observar mudanças.

> Dica: Você pode iniciar `iperf3` manualmente (como acima) se quiser testar cenários específicos de carga.

---

## 7) Como funciona o algoritmo LB-DV


### LB-DV (Distance Vector sensível à carga)
- Métrica de link: `c(e) = 1 + w_load · U(e)`;
  - `U(e)`: **utilização** medida via contadores `/sys/class/net/*/statistics/*` durante uma janela `--interval`;
  - `w_load`: peso de sensibilidade à carga (default `4.0`, aumente para impactar mais);
- O custo total do caminho é a **soma** dos custos dos links;
- Também usa **split horizon + poison reverse**;
- Durante a convergência, **mede utilização** e **recalcula rotas**; ao final instala com `ip route`.


---

## 9) Roteiro rápido para apresentação

1. **RIP baseline**
   ```bash
   sudo python3 custom_topo.py --algo rip
   # no mininet>
   h1 ping h2
   r1 ip route
   r2 ip route
   r3 ip route
   ```

2. **LB-DV desviando por carga**
   ```bash
   sudo python3 custom_topo.py --algo lb --altlink --autotraffic --wload 8 --rounds 10 --disable-offload
   # no mininet>
   h1 traceroute -n 10.0.2.10
   r1 ip route | grep 10.0.2.0
   ```
   Mostre que a rota para `10.0.2.0/24` pode mudar de `via 10.0.12.2` (direto) para `via 10.0.13.2` (via r3) quando o link direto fica carregado.
