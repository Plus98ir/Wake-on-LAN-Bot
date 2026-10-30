#!/usr/bin/env python3
"""wolbot - wake and control home PCs from a Bale (or Telegram) bot. Standard library only.

Config: /etc/wol-bot/config.json (WOLBOT_CONF overrides). Run `wolbot help` for commands.
"""
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request

CONF = os.environ.get("WOLBOT_CONF", "/etc/wol-bot/config.json")
HOME = os.path.dirname(CONF)
KEY = os.path.join(HOME, "id_ed25519")            # bot -> PC login key
KNOWN = os.path.join(HOME, "known_hosts")
TUNNEL_KEYS = "/var/lib/woltun/.ssh/authorized_keys"
APIS = {
    "bale": "https://tapi.bale.ai/bot{token}/{method}",
    "telegram": "https://api.telegram.org/bot{token}/{method}",
}
WAKE_WAIT = 240          # seconds to wait for a PC to come online after a wake
PAIR_TTL = 900           # pair codes live 15 min
FIRST_TUNNEL_PORT = 2230
PENDING = os.path.join(HOME, "pair-codes.json")   # pair code -> expiry time (shared with the CLI)
FAILS = {}               # client ip -> [fail times]
LOCK = threading.Lock()

# Default remote commands per OS. Windows apps are scheduled tasks created by setup-pc.ps1
# (interactive, so they open on the logged-on desktop).
OS_ACTIONS = {
    "windows": {
        "lock": 'schtasks /run /tn "WOL\\Lock"',
        "restart": "shutdown /r /t 5 /f",
        "shutdown": "shutdown /s /t 5 /f",
        "who": "query user",
    },
    "linux": {
        "lock": "loginctl lock-sessions",
        "restart": "systemctl reboot",
        "shutdown": "systemctl poweroff",
        "who": "who",
    },
}
DEFAULT_APPS = {
    "windows": {"v2rayN": 'schtasks /run /tn "WOL\\v2rayN"', "VS Code": 'schtasks /run /tn "WOL\\VSCode"'},
    "linux": {},
}


# ---------- config ----------

def load():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def save(cfg):
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


def find(cfg, name):
    for m in cfg["machines"]:
        if m["name"].lower() == name.lower():
            return m
    raise SystemExit(f"Unknown machine: {name}")


def write_tunnel_keys(cfg):
    """One restricted key per PC: remote forward to its own port only."""
    lines = []
    for m in cfg["machines"]:
        pc = m.get("pc") or {}
        if pc.get("tunnel_key"):
            lines.append(f'restrict,port-forwarding,permitlisten="127.0.0.1:{pc["port"]}" '
                         f'{pc["tunnel_key"]} wol-{m["name"]}')
    os.makedirs(os.path.dirname(TUNNEL_KEYS), exist_ok=True)
    with open(TUNNEL_KEYS, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(TUNNEL_KEYS, 0o600)
    subprocess.run(["chown", "-R", "woltun:woltun", os.path.dirname(TUNNEL_KEYS)], check=False)


# ---------- machine actions ----------

def norm_mac(mac):
    h = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(h) != 12:
        raise ValueError(f"bad MAC: {mac}")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


def magic_packet(mac):
    return b"\xff" * 6 + bytes.fromhex(norm_mac(mac).replace(":", "")) * 16


def run_ssh(host, port, user, cmd, key=None, timeout=20):
    args = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=5", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        args += ["-i", key, "-o", f"UserKnownHostsFile={KNOWN}", "-o", "IdentitiesOnly=yes"]
    args += [f"{user}@{host}", cmd]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def wake(m):
    """Try every wake method of the machine; True if at least one sent the packet."""
    mac = norm_mac(m["mac"])
    report, ok = [], False
    for w in m.get("wake", []):
        try:
            if w["type"] == "ssh":
                iface = w.get("iface", "br-lan")
                cmd = (f"if command -v etherwake >/dev/null; then etherwake -i {iface} {mac}; "
                       f"else ether-wake -i {iface} {mac}; fi")
                rc, out = run_ssh(w["host"], w.get("port", 22), w.get("user", "root"), cmd, w.get("key"))
                good = rc == 0
                report.append(f"ssh {w['host']}:{w.get('port', 22)}: {'sent' if good else 'failed ' + out[-120:]}")
            elif w["type"] == "udp":
                addr = (socket.gethostbyname(w["host"]), int(w.get("port", 9)))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    for _ in range(3):
                        s.sendto(magic_packet(mac), addr)
                        time.sleep(0.2)
                good = True
                report.append(f"udp {w['host']}:{addr[1]}: sent")
            else:
                good = False
                report.append(f"unknown wake type {w['type']}")
        except Exception as e:  # noqa: BLE001 - report any failure to the user
            good = False
            report.append(f"{w.get('type')}: {e}")
        ok = ok or good
    if not m.get("wake"):
        report.append("no wake method configured")
    return ok, report


def online(m):
    """PC is online when its reverse tunnel answers with an SSH banner."""
    pc = m.get("pc")
    if not pc:
        return None
    try:
        with socket.create_connection(("127.0.0.1", int(pc["port"])), timeout=4) as s:
            s.settimeout(4)
            return s.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def pc_run(m, cmd, timeout=20):
    pc = m["pc"]
    return run_ssh("127.0.0.1", pc["port"], pc.get("user", "Administrator"), cmd, KEY, timeout)


def actions(m):
    return {**OS_ACTIONS[m.get("os", "windows")], **m.get("actions", {})}


def apps(m):
    return m.get("apps", DEFAULT_APPS[m.get("os", "windows")])


# ---------- bot ----------

class Api:
    def __init__(self, cfg):
        self.url = APIS[cfg.get("api", "bale")]
        self.token = cfg["token"]
        proxy = cfg.get("proxy")  # optional http(s) proxy, e.g. for Telegram from Iran
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method, wait=20, **params):
        req = urllib.request.Request(self.url.format(token=self.token, method=method),
                                     json.dumps(params).encode(), {"Content-Type": "application/json"})
        with self.opener.open(req, timeout=wait) as r:
            j = json.load(r)
        if not j.get("ok"):
            raise RuntimeError(f"{method}: {j}")
        return j["result"]

    def send(self, chat, text, kb=None):
        p = {"chat_id": chat, "text": text}
        if kb:
            p["reply_markup"] = {"inline_keyboard": kb}
        return self.call("sendMessage", **p)

    def edit(self, chat, mid, text, kb=None):
        p = {"chat_id": chat, "message_id": mid, "text": text}
        if kb:
            p["reply_markup"] = {"inline_keyboard": kb}
        try:
            return self.call("editMessageText", **p)
        except Exception:  # noqa: BLE001 - "message is not modified" etc.
            return None


def btn(text, data):
    return {"text": text, "callback_data": data}


def status_line(m):
    st = online(m)
    if st is None:
        return "⚪ no agent (wake only)"
    return "🟢 online" if st else "🔴 offline"


def machine_view(cfg, i, note=""):
    m = cfg["machines"][i]
    text = f"🖥 {m['name']}  ({m['mac']})\nStatus: {status_line(m)}"
    if note:
        text += f"\n\n{note}"
    kb = [[btn("⚡ Wake", f"w:{i}"), btn("🔄 Refresh", f"s:{i}")]]
    if m.get("pc"):
        names = list(apps(m))
        for j in range(0, len(names), 2):
            kb.append([btn(f"▶️ {n}", f"a:{i}:{j + k}") for k, n in enumerate(names[j:j + 2])])
        kb.append([btn("🔒 Lock", f"l:{i}"), btn("👤 Who", f"u:{i}")])
        kb.append([btn("🔁 Restart", f"r:{i}"), btn("⏻ Shut down", f"h:{i}")])
        kb.append([btn("💻 VS Code Remote-SSH", f"v:{i}")])
    kb.append([btn("🗑 Remove", f"d:{i}"), btn("⬅️ Machines", "m")])
    return text, kb


def list_view(cfg):
    lines = [f"🖥 {m['name']}: {status_line(m)}" for m in cfg["machines"]] or ["No machines yet."]
    kb = [[btn(f"🖥 {m['name']}", f"o:{i}")] for i, m in enumerate(cfg["machines"])]
    kb.append([btn("➕ Add PC", "p")])
    return "Machines\n\n" + "\n".join(lines), kb


def new_pair_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    code = f"{code[:4]}-{code[4:]}"
    with LOCK:
        codes = load_codes()
        codes[code] = time.time() + PAIR_TTL
        save_codes(codes)
    return code


def load_codes():
    try:
        with open(PENDING) as f:
            codes = json.load(f)
    except (OSError, ValueError):
        codes = {}
    now = time.time()
    return {c: t for c, t in codes.items() if t > now}


def save_codes(codes):
    with open(PENDING, "w") as f:
        json.dump(codes, f)
    os.chmod(PENDING, 0o600)


def pair_view(cfg):
    code = new_pair_code()
    full = f"{cfg.get('public_host', '<server>')}:{cfg.get('pair_port', 8790)}/{code}"
    text = ("➕ Add a PC\n\n"
            "1. On the PC run WOL Agent.exe\n"
            "2. Paste this code and press Install:\n\n"
            f"{full}\n\n"
            f"The code works once and expires in {PAIR_TTL // 60} min.\n"
            "Before that: BIOS -> Power On By PCI-E = Enabled, ErP = Disabled.")
    return text, [[btn("⬅️ Machines", "m")]]


def vscode_help(cfg, m):
    host = cfg.get("public_host") or "<this-server>"
    user = cfg.get("jump_user", "root")
    pc = m["pc"]
    return (f"VS Code Remote-SSH for {m['name']}\n\n"
            "Add to ~/.ssh/config on your laptop:\n\n"
            f"Host {m['name'].lower()}\n"
            f"  HostName 127.0.0.1\n"
            f"  Port {pc['port']}\n"
            f"  User {pc.get('user', 'Administrator')}\n"
            f"  ProxyJump {user}@{host}\n\n"
            "Your laptop key must be in the PC's administrators_authorized_keys "
            "(setup-pc.ps1 -ExtraKey) and allowed on the jump server.\n"
            f"Then in VS Code: Remote-SSH: Connect to Host -> {m['name'].lower()}")


def wait_online(api, cfg, chat, mid, i):
    m = cfg["machines"][i]
    start = time.time()
    while time.time() - start < WAKE_WAIT:
        if online(m):
            t, kb = machine_view(cfg, i, f"✅ {m['name']} is online after {int(time.time() - start)} s.")
            api.edit(chat, mid, t, kb)
            return
        time.sleep(5)
    t, kb = machine_view(cfg, i, f"⚠️ No answer after {WAKE_WAIT // 60} min. "
                                 "Check BIOS/NIC WOL, auto-logon and the WOL\\Tunnel task.")
    api.edit(chat, mid, t, kb)


def handle_callback(api, cfg, cb):
    chat = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    try:
        api.call("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:  # noqa: BLE001
        pass
    parts = data.split(":")
    op = parts[0]
    if op == "m":
        t, kb = list_view(cfg)
        return api.edit(chat, mid, t, kb)
    if op == "p":
        t, kb = pair_view(cfg)
        return api.edit(chat, mid, t, kb)
    i = int(parts[1])
    if i >= len(cfg["machines"]):
        return None
    m = cfg["machines"][i]
    note = ""
    if op == "o" or op == "s":
        pass
    elif op == "w":
        ok, report = wake(m)
        note = ("⚡ Magic packet sent." if ok else "❌ Wake failed.") + "\n" + "\n".join(report)
        if ok and m.get("pc") and not online(m):
            note += f"\nWaiting up to {WAKE_WAIT // 60} min for it to come online..."
            threading.Thread(target=wait_online, args=(api, cfg, chat, mid, i), daemon=True).start()
    elif op == "a":
        name = list(apps(m))[int(parts[2])]
        rc, out = pc_run(m, apps(m)[name])
        note = f"▶️ {name}: " + ("started" if rc == 0 else f"failed ({out[-200:]})")
    elif op in ("l", "u", "R", "H"):
        key = {"l": "lock", "u": "who", "R": "restart", "H": "shutdown"}[op]
        rc, out = pc_run(m, actions(m)[key])
        if key == "who":
            note = out[-800:] or "(nobody logged on)"
        else:
            note = f"{key}: " + ("done" if rc == 0 else f"failed ({out[-200:]})")
    elif op in ("r", "h"):
        word = "Restart" if op == "r" else "Shut down"
        kb = [[btn(f"✅ Yes, {word.lower()}", f"{op.upper()}:{i}"), btn("✖️ Cancel", f"s:{i}")]]
        return api.edit(chat, mid, f"{word} {m['name']}?", kb)
    elif op == "v":
        return api.edit(chat, mid, vscode_help(cfg, m), [[btn("⬅️ Back", f"s:{i}")]])
    elif op == "d":
        kb = [[btn("🗑 Yes, remove", f"D:{i}"), btn("✖️ Cancel", f"s:{i}")]]
        return api.edit(chat, mid, f"Remove {m['name']} from the bot? (run WOL Agent -> Uninstall on the PC too)", kb)
    elif op == "D":
        cfg["machines"].pop(i)
        save(cfg)
        write_tunnel_keys(cfg)
        t, kb = list_view(cfg)
        return api.edit(chat, mid, f"Removed {m['name']}.\n\n" + t, kb)
    t, kb = machine_view(cfg, i, note)
    return api.edit(chat, mid, t, kb)


def serve():
    cfg = load()
    if not cfg.get("token"):
        raise SystemExit("No bot token in config. Run: wolbot set-token <token>")
    api = Api(cfg)
    admins = {str(a).lstrip("b") for a in cfg.get("admins", [])}
    offset = 0
    pair_server(api)
    print(f"wolbot started ({cfg.get('api', 'bale')}, {len(cfg['machines'])} machines)", flush=True)
    while True:
        try:
            ups = api.call("getUpdates", wait=45, offset=offset, timeout=30)
        except Exception as e:  # noqa: BLE001 - network hiccup: retry
            print(f"getUpdates: {e}", flush=True)
            time.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            try:
                cfg = load()  # pick up edits without a restart
                if "callback_query" in u:
                    cb = u["callback_query"]
                    if str(cb["from"]["id"]) not in admins:
                        continue
                    handle_callback(api, cfg, cb)
                elif "message" in u:
                    msg = u["message"]
                    uid = str(msg["from"]["id"])
                    if uid not in admins:
                        api.send(msg["chat"]["id"], f"Not allowed. Your id: {uid}")
                        continue
                    t, kb = list_view(cfg)
                    api.send(msg["chat"]["id"], t, kb)
            except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                print(f"update {u.get('update_id')}: {e}", flush=True)


# ---------- pairing (WOL Agent.exe -> POST /pair) ----------

def free_port(cfg):
    used = {m["pc"]["port"] for m in cfg["machines"] if m.get("pc")}
    port = FIRST_TUNNEL_PORT
    while port in used:
        port += 1
    return port


def pair(cfg, req, client_ip):
    """Register (or re-register) a PC from a valid one-time code. Returns the agent settings."""
    code = str(req.get("code", "")).upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    with LOCK:
        fails = [t for t in FAILS.get(client_ip, []) if t > time.time() - 600]
        if len(fails) >= 5:
            raise PermissionError("too many wrong codes, try again in 10 min")
        codes = load_codes()
        if code not in codes:
            FAILS[client_ip] = fails + [time.time()]
            raise PermissionError("wrong or expired code - press Add PC in the bot for a new one")
        del codes[code]
        save_codes(codes)
    mac = norm_mac(req["mac"])
    key = " ".join(str(req.get("tunnel_key", "")).split()[:2])
    if not re.match(r"^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+$", key):
        raise ValueError("bad tunnel key")
    name = re.sub(r"[^\w.-]", "", str(req.get("name") or "PC"))[:32] or "PC"
    m = next((x for x in cfg["machines"] if x["mac"] == mac), None)
    if m is None:
        names = {x["name"].lower() for x in cfg["machines"]}
        base, n = name, 2
        while name.lower() in names:
            name, n = f"{base}-{n}", n + 1
        m = {"name": name, "mac": mac, "wake": []}
        cfg["machines"].append(m)
    port = (m.get("pc") or {}).get("port") or free_port(cfg)
    m["os"] = req.get("os", "windows")
    m["pc"] = {"port": port, "user": str(req.get("user") or "Administrator"), "tunnel_key": key}
    if not m.get("wake"):
        # any modem: UDP magic packet to its public address (needs a port forward of UDP 9)
        m["wake"] = [{"type": "udp", "host": req.get("wake_host") or client_ip, "port": 9}]
    save(cfg)
    write_tunnel_keys(cfg)
    return m, {
        "ok": True, "name": m["name"], "server": cfg.get("public_host"), "ssh_port": cfg.get("ssh_port", 22),
        "tunnel_user": "woltun", "tunnel_port": port, "bot_key": open(KEY + ".pub").read().strip(),
    }


def pair_server(api):
    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - http.server API
            if self.path != "/pair":
                return self.reply(404, {"error": "not found"})
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 8192)
                req = json.loads(self.rfile.read(n) or b"{}")
                cfg = load()
                m, out = pair(cfg, req, self.client_address[0])
            except PermissionError as e:
                return self.reply(403, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self.reply(400, {"error": f"bad request: {e}"})
            self.reply(200, out)
            for a in cfg.get("admins", []):
                try:
                    t, kb = machine_view(cfg, cfg["machines"].index(m), "✅ New PC paired. It shows online once its tunnel is up.")
                    api.send(str(a).lstrip("b"), t, kb)
                except Exception as e:  # noqa: BLE001
                    print(f"notify {a}: {e}", flush=True)

        def log_message(self, fmt, *args):
            print(f"pair {self.client_address[0]}: {fmt % args}", flush=True)

    cfg = load()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", int(cfg.get("pair_port", 8790))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------- CLI ----------

HELP = """wolbot - Wake-on-LAN bot

  wolbot run                                   run the bot (systemd uses this)
  wolbot set-token <token> [bale|telegram]     set the bot token
  wolbot admin <id>                            allow a Bale/Telegram user id
  wolbot add-pc <name> <mac> <port> "<tunnel pubkey>" [user] [windows|linux]
                                               add a PC with the agent (setup-pc.ps1 prints this line)
  wolbot add-wake <name> ssh <host> <port> <user> [iface]
                                               wake through a router / Linux box in the LAN (etherwake)
  wolbot add-wake <name> udp <host> [port]     wake by UDP magic packet to a modem port forward
  wolbot del <name>                            remove a machine
  wolbot list | wake <name> | status <name>
  wolbot pair-code                             one-time code for WOL Agent.exe (same as Add PC in the bot)
  wolbot pubkey                                key to put on the PC (setup-pc.ps1 -BotKey)
"""


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "run":
        return serve()
    if cmd == "pubkey":
        return print(open(KEY + ".pub").read().strip())
    if cmd == "pair-code":
        cfg = load()
        return print(f"{cfg.get('public_host', '<server>')}:{cfg.get('pair_port', 8790)}/{new_pair_code()}"
                     f"  (one use, {PAIR_TTL // 60} min) - paste it into WOL Agent.exe")
    if cmd == "help" or cmd not in ("set-token", "admin", "add-pc", "add-wake", "del", "list", "wake", "status"):
        return print(HELP)
    cfg = load()
    if cmd == "set-token":
        cfg["token"] = argv[1]
        cfg["api"] = argv[2] if len(argv) > 2 else cfg.get("api", "bale")
    elif cmd == "admin":
        cfg.setdefault("admins", [])
        if argv[1] not in map(str, cfg["admins"]):
            cfg["admins"].append(argv[1])
    elif cmd == "add-pc":
        name, mac, port, pub = argv[1:5]
        if not re.match(r"^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+$", " ".join(pub.split()[:2])):
            raise SystemExit("tunnel pubkey must look like: ssh-ed25519 AAAA...")
        m = next((x for x in cfg["machines"] if x["name"].lower() == name.lower()), None)
        if m is None:
            m = {"name": name, "wake": []}
            cfg["machines"].append(m)
        m["mac"] = norm_mac(mac)
        m["os"] = argv[6] if len(argv) > 6 else m.get("os", "windows")
        m["pc"] = {"port": int(port), "user": argv[5] if len(argv) > 5 else "Administrator",
                   "tunnel_key": " ".join(pub.split()[:2])}
    elif cmd == "add-wake":
        m = find(cfg, argv[1])
        if argv[2] == "ssh":
            w = {"type": "ssh", "host": argv[3], "port": int(argv[4]), "user": argv[5],
                 "iface": argv[6] if len(argv) > 6 else "br-lan"}
        elif argv[2] == "udp":
            w = {"type": "udp", "host": argv[3], "port": int(argv[4]) if len(argv) > 4 else 9}
        else:
            raise SystemExit("type must be ssh or udp")
        m["wake"] = [x for x in m.get("wake", []) if not (x["type"] == w["type"] and x["host"] == w["host"])]
        m["wake"].append(w)
    elif cmd == "del":
        cfg["machines"] = [x for x in cfg["machines"] if x["name"].lower() != argv[1].lower()]
    elif cmd == "list":
        for m in cfg["machines"]:
            print(f"{m['name']}  {m['mac']}  {status_line(m)}  wake={[w['type'] + ':' + w['host'] for w in m.get('wake', [])]}"
                  + (f"  tunnel=127.0.0.1:{m['pc']['port']}" if m.get("pc") else ""))
        return None
    elif cmd == "wake":
        ok, report = wake(find(cfg, argv[1]))
        print("\n".join(report))
        return 0 if ok else 1
    elif cmd == "status":
        return print(status_line(find(cfg, argv[1])))
    save(cfg)
    write_tunnel_keys(cfg)
    print("Saved. Restart the bot to apply token/admin changes: systemctl restart wol-bot")
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
