# TGA – Roteamento Dinâmico em Mininet (RIP e LB-DV)

Este projeto implementa e demonstra **roteamento dinâmico** em uma topologia virtual construída no **Mininet**, com **dois algoritmos** à escolha na execução:

- **RIP (Distance Vector clássico)** com *split horizon + poison reverse*;
- **LB-DV (Distance Vector sensível à carga)**, que **equilibra tráfego** considerando a **utilização dos enlaces** durante a convergência, com custo `c(e) = 1 + w_load · U(e)`.

Tudo está em **um único arquivo** (`custom_topo.py`) para facilitar execução e demonstração.

---

## 1) 📡 Topologia da Rede

A topologia implementada neste projeto é composta por **5 roteadores interconectados** (`r1`–`r5`) e **3 hosts de borda** (`h1`, `h2`, `h3`).  

<img width="696" height="304" alt="Screenshot 2025-10-02 at 09 16 46" src="https://github.com/user-attachments/assets/944b09ce-a207-4ad6-93b4-4aab2033634c" />

- **Hosts**
  - `h1` (IP `10.0.1.10`) conectado ao roteador `r1` (rede `10.0.1.0/24`)  
  - `h2` (IP `10.0.2.10`) conectado ao roteador `r3` (rede `10.0.2.0/24`)  
  - `h3` (IP `10.0.3.10`) conectado ao roteador `r5` (rede `10.0.3.0/24`)  

- **Enlaces entre roteadores** (com largura de banda definida):  
  - `r1 — r2` → `10.0.12.0/24`, **5 Mbps**  
  - `r2 — r3` → `10.0.23.0/24`, **10 Mbps**  
  - `r1 — r4` → `10.0.14.0/24`, **10 Mbps**  
  - `r4 — r3` → `10.0.43.0/24`, **10 Mbps**  
  - `r2 — r4` → `10.0.24.0/24`, **8 Mbps**  
  - `r4 — r5` → `10.0.45.0/24`, **10 Mbps**  
  - `r3 — r5` → `10.0.35.0/24`, **8 Mbps**  

Essa configuração forma uma **rede multipath**, permitindo que existam diferentes rotas possíveis entre os hosts, por exemplo:  

- `h1 → h2`: via `r1–r2–r3` ou `r1–r4–r3`  
- `h1 → h3`: via `r1–r4–r5` ou `r1–r2–r3–r5`  

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
mininet> h1 tracepath -n 10.0.2.10
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

## Integrantes

Cínthia Becher e Gabrielle Bussolo
