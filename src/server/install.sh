#!/usr/bin/env bash
# Install / update wolbot on a Linux server (Debian/Ubuntu). Run as root from the folder holding wolbot.py.
#   bash install.sh [--token TOKEN] [--admin ID] [--api bale|telegram] [--public-host HOST]
#   bash install.sh --uninstall
set -euo pipefail

DIR=/opt/wol-bot
ETC=/etc/wol-bot
UNIT=/etc/systemd/system/wol-bot.service
SSHD_DROP=/etc/ssh/sshd_config.d/wol-bot.conf
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now wol-bot 2>/dev/null || true
  rm -f "$UNIT" /usr/local/bin/wolbot "$SSHD_DROP"
  rm -rf "$DIR"
  systemctl daemon-reload
  sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  echo "Removed. Config kept in $ETC, tunnel user 'woltun' kept (userdel -r woltun to remove)."
  exit 0
fi

TOKEN="" ADMIN="" API="" PUBHOST=""
while [ $# -gt 0 ]; do
  case $1 in
    --token) TOKEN=$2; shift 2 ;;
    --admin) ADMIN=$2; shift 2 ;;
    --api) API=$2; shift 2 ;;
    --public-host) PUBHOST=$2; shift 2 ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
# fingerprint confirmation (wolbot auth) checks passkey signatures with it
python3 -c "import cryptography" 2>/dev/null || apt-get install -y -q python3-cryptography >/dev/null 2>&1 \
  || echo "  python3-cryptography missing - only needed for fingerprint confirmation (wolbot auth)"

echo "[1/5] Files"
install -d -m 755 "$DIR"
install -m 755 "$SRC/wolbot.py" "$DIR/wolbot.py"
install -m 644 "$SRC/relay.py" "$DIR/relay.py"
install -d -m 755 "$DIR/app"
install -m 644 "$SRC"/app/* "$DIR/app/"
ln -sf "$DIR/wolbot.py" /usr/local/bin/wolbot
install -d -m 700 "$ETC"
[ -f "$ETC/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C "wolbot@$(hostname)" -f "$ETC/id_ed25519"
if [ ! -f "$ETC/config.json" ]; then
  cat > "$ETC/config.json" <<EOF
{
  "api": "bale",
  "token": "",
  "admins": [],
  "public_host": "$(hostname -I | awk '{print $1}')",
  "jump_user": "root",
  "pair_port": 8790,
  "machines": []
}
EOF
  chmod 600 "$ETC/config.json"
fi

echo "[2/5] Tunnel user 'woltun' (remote forwards only, no shell)"
id woltun >/dev/null 2>&1 || useradd -r -m -d /var/lib/woltun -s /usr/sbin/nologin woltun
install -d -m 700 -o woltun -g woltun /var/lib/woltun/.ssh
touch /var/lib/woltun/.ssh/authorized_keys
chown woltun:woltun /var/lib/woltun/.ssh/authorized_keys
chmod 600 /var/lib/woltun/.ssh/authorized_keys

echo "[3/5] sshd rules for woltun"
if ! grep -qE '^\s*Include\s+/etc/ssh/sshd_config.d/' /etc/ssh/sshd_config; then
  echo "  /etc/ssh/sshd_config has no 'Include /etc/ssh/sshd_config.d/*.conf' - add it, then rerun"; exit 1
fi
cat > "$SSHD_DROP" <<'EOF'
# wolbot: PCs keep a reverse tunnel here; nothing else is allowed for this user
Match User woltun
    AllowTcpForwarding remote
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PasswordAuthentication no
    ForceCommand /bin/false
    # a dead tunnel (home IP changed, modem rebooted) must free its port fast, or the new one is refused
    ClientAliveInterval 15
    ClientAliveCountMax 3
EOF
if sshd -t; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
else
  rm -f "$SSHD_DROP"; echo "sshd config test failed - rule removed"; exit 1
fi

echo "[4/5] Settings"
[ -n "$TOKEN" ] && wolbot set-token "$TOKEN" ${API:-} >/dev/null
[ -n "$ADMIN" ] && wolbot admin "$ADMIN" >/dev/null
if [ -n "$PUBHOST" ]; then
  python3 - "$ETC/config.json" "$PUBHOST" <<'EOF'
import json, sys
p, h = sys.argv[1], sys.argv[2]
c = json.load(open(p)); c["public_host"] = h
json.dump(c, open(p, "w"), indent=2)
EOF
fi

echo "[5/5] Service"
cat > "$UNIT" <<EOF
[Unit]
Description=wolbot - Wake-on-LAN bot
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $DIR/wolbot.py run
Restart=always
RestartSec=5
MemoryMax=80M

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
if python3 -c "import json,sys; sys.exit(0 if json.load(open('$ETC/config.json')).get('token') else 1)"; then
  systemctl enable --now wol-bot >/dev/null 2>&1
  systemctl restart wol-bot
  echo "Bot running: systemctl status wol-bot"
else
  systemctl enable wol-bot >/dev/null 2>&1
  echo "No token yet: wolbot set-token <token> && systemctl restart wol-bot"
fi

echo
echo "Bot key for the PC (setup-pc.ps1 -BotKey):"
cat "$ETC/id_ed25519.pub"
