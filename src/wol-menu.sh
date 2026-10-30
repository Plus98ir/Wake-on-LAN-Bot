#!/usr/bin/env bash
# wol-menu - wake LAN machines through an OpenWrt router over SSH (etherwake).
# Config: /etc/wol-menu.conf (created on first run). Usage: wol-menu [wake <name>|status <name>|list]
set -u

CONF=${WOL_CONF:-/etc/wol-menu.conf}

if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<'EOF'
# Router reachable by SSH (direct, or a reverse tunnel port on this server)
ROUTER_HOST=127.0.0.1
ROUTER_PORT=22
ROUTER_USER=root
ROUTER_IFACE=br-lan
# One target per line: name MAC [IP for status check]
TARGETS="
PC 00:00:00:00:00:00 192.168.1.10
"
EOF
  echo "Created $CONF - edit it, then run again."
  exit 1
fi
# shellcheck source=/dev/null
. "$CONF"

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'

rsh() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 \
      -p "$ROUTER_PORT" "$ROUTER_USER@$ROUTER_HOST" "$@"
}

targets() { printf '%s\n' "$TARGETS" | awk 'NF>=2 && $1 !~ /^#/'; }

# Alive = answers ARP (works even when the OS firewall drops ping)
alive() {
  rsh "ip neigh flush to $1 dev '$ROUTER_IFACE' 2>/dev/null; ping -c 1 -W 1 $1 >/dev/null 2>&1; ip neigh show to $1 dev '$ROUTER_IFACE' | grep -q lladdr"
}

field() { targets | awk -v n="$1" -v f="$2" '$1==n {print $f; exit}'; }

check_router() {
  if rsh true 2>/dev/null; then
    echo "${G}Router reachable${N} ($ROUTER_USER@$ROUTER_HOST:$ROUTER_PORT)"
  else
    echo "${R}Router NOT reachable${N} ($ROUTER_USER@$ROUTER_HOST:$ROUTER_PORT) - tunnel down?"
    return 1
  fi
}

status() {
  local ip; ip=$(field "$1" 3)
  [ -n "$ip" ] || { echo "${Y}$1: no IP in config${N}"; return 0; }
  if alive "$ip"; then
    echo "$1 ($ip): ${G}ONLINE${N}"
  else
    echo "$1 ($ip): ${R}offline${N}"
  fi
}

wake() {
  local name=$1 mac; mac=$(field "$name" 2)
  [ -n "$mac" ] || { echo "${R}Unknown target: $name${N}"; return 1; }
  echo "Sending magic packet to $name ($mac) via $ROUTER_IFACE ..."
  if rsh "etherwake -i '$ROUTER_IFACE' '$mac'"; then
    echo "${G}Sent.${N}"
  else
    echo "${R}Failed (router unreachable or etherwake missing).${N}"; return 1
  fi
  local ip; ip=$(field "$name" 3)
  [ -n "$ip" ] || return 0
  printf 'Waiting for %s to come up' "$name"
  for _ in $(seq 1 24); do
    if alive "$ip"; then
      echo; echo "${G}$name is ONLINE.${N}"; return 0
    fi
    printf '.'; sleep 5
  done
  echo; echo "${Y}No answer after 2 min (check BIOS/NIC WOL settings).${N}"
}

menu() {
  while true; do
    echo
    echo "${B}=== Wake-on-LAN ===${N}"
    local i=0 names=()
    while read -r n m ip; do
      i=$((i+1)); names+=("$n")
      echo " $i) Wake $n  ($m${ip:+, $ip})"
    done < <(targets)
    echo " s) Status of all"
    echo " r) Check router connection"
    echo " 0) Back / Exit"
    read -rp "Choice: " c
    case $c in
      0|q|"") return 0 ;;
      s) while read -r n _; do status "$n"; done < <(targets) ;;
      r) check_router ;;
      *) if [[ $c =~ ^[0-9]+$ ]] && [ "$c" -ge 1 ] && [ "$c" -le "${#names[@]}" ]; then
           wake "${names[$((c-1))]}"
         else
           echo "${R}Invalid choice${N}"
         fi ;;
    esac
  done
}

case ${1:-} in
  wake)   wake "${2:?name}" ;;
  status) status "${2:?name}" ;;
  list)   targets ;;
  *)      menu ;;
esac
