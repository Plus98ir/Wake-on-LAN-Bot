#!/usr/bin/env python3
"""wolbot - wake and control home PCs from a Bale (or Telegram) bot. Standard library only.

Config: /etc/wol-bot/config.json (WOLBOT_CONF overrides). Run `wolbot help` for commands.
"""
import base64
import hashlib
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
import urllib.parse
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
RELAY_STATE = os.path.join(HOME, "relay-state.json")  # {"queue": {relay id: [mac]}, "seen": {relay id: time}}
RELAY_SRC = os.path.join(os.path.dirname(os.path.realpath(__file__)), "relay.py")
IP_CHECK = 300           # seconds between public-IP checks for auto UDP wake targets
FAILS = {}               # client ip -> [fail times]
LOCK = threading.Lock()

# Fingerprint confirmation (passkeys). Off until `wolbot auth <https url>`.
AUTH_TTL = 120           # a confirmation link lives 2 min and works once
ENROLL = os.path.join(HOME, "passkey-codes.json")  # passkey sign-up code -> {exp, owner, name} (shared with the CLI)
CONFIRMS = {}            # confirmation token -> the button press waiting for a fingerprint
ENROLL_CH = {}           # sign-up code -> challenge of the open sign-up page
AUTH_FAILS = []          # times of failed checks (kill switch)
NOTIFIED = {}            # stranger id -> last alert time
PROTECT = "walRHckDpy"   # buttons that need a fingerprint (config auth.protect overrides)
OP_NAMES = {"w": "Wake", "a": "Start", "l": "Lock", "u": "Who", "R": "Restart", "H": "Shut down",
            "c": "Claude Remote", "k": "Stop Claude", "D": "Remove", "p": "Add PC (pair code)",
            "y": "Relay code"}

CLAUDE_PS = r"powershell -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\wol-agent\wol-claude.ps1"

# Kill switch on Windows: writes wol-kill.ps1 / wol-unkill.ps1 and starts task WOL\Kill as SYSTEM,
# so it keeps running after it cuts this very SSH session.
KILL_PS = r'''$a = "$env:ProgramData\wol-agent"
New-Item -ItemType Directory -Force $a | Out-Null
@'
# wolbot kill switch: cut every remote way in. Undo on the PC: wol-unkill.ps1 (as administrator)
Add-Content "$env:ProgramData\wol-agent\kill.log" "$(Get-Date -Format s) kill switch"
schtasks /run /tn "WOL\Lock" | Out-Null
schtasks /change /tn "WOL\Tunnel" /disable | Out-Null
schtasks /end /tn "WOL\Tunnel" | Out-Null
Get-Process ssh -ErrorAction SilentlyContinue | Stop-Process -Force
Set-Service sshd -StartupType Disabled
Stop-Service sshd -Force
Get-NetAdapter | Where-Object Status -ne Disabled | Disable-NetAdapter -Confirm:$false
'@ | Set-Content "$a\wol-kill.ps1" -Encoding utf8
@'
# Undo the wolbot kill switch (run as administrator), then on the server: wolbot unlock
Get-NetAdapter | Enable-NetAdapter -Confirm:$false
Set-Service sshd -StartupType Automatic
Start-Service sshd
schtasks /change /tn "WOL\Tunnel" /enable | Out-Null
schtasks /run /tn "WOL\Tunnel" | Out-Null
'@ | Set-Content "$a\wol-unkill.ps1" -Encoding utf8
foreach ($f in "wol-kill.ps1", "wol-unkill.ps1") { icacls "$a\$f" /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null }
$act = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$a\wol-kill.ps1`""
$sys = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskPath "\WOL\" -TaskName Kill -Action $act -Principal $sys -Force | Out-Null
Start-ScheduledTask -TaskPath "\WOL\" -TaskName Kill
'''
KILL_CMD = ("powershell -NoProfile -NonInteractive -EncodedCommand "
            + base64.b64encode(KILL_PS.encode("utf-16-le")).decode())

# Default remote commands per OS. Windows apps are scheduled tasks created by setup-pc.ps1
# (interactive, so they open on the logged-on desktop).
OS_ACTIONS = {
    "windows": {
        "lock": 'schtasks /run /tn "WOL\\Lock"',
        "restart": "shutdown /r /t 5 /f",
        "shutdown": "shutdown /s /t 5 /f",
        "who": "query user",
        "claude": 'schtasks /run /tn "WOL\\Claude"',
        "claude_status": f"{CLAUDE_PS} -Status",
        "claude_stop": f"{CLAUDE_PS} -Stop",
        "kill": KILL_CMD,
    },
    "linux": {
        "lock": "loginctl lock-sessions",
        "restart": "systemctl reboot",
        "shutdown": "systemctl poweroff",
        "who": "who",
        "kill": "loginctl lock-sessions; (sleep 1; systemctl disable --now ssh sshd) >/dev/null 2>&1 &",
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
            elif w["type"] == "relay":
                with LOCK:
                    st = load_relay_state()
                    q = st["queue"].setdefault(w["id"], [])
                    if mac not in q:
                        q.append(mac)
                    save_relay_state(st)
                good = relay_online(w["id"])
                report.append(f"relay {relay_name(w['id'])}: " + ("sent" if good else "queued, relay is offline"))
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


def load_relay_state():
    try:
        with open(RELAY_STATE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    return {"queue": st.get("queue", {}), "seen": st.get("seen", {})}


def save_relay_state(st):
    with open(RELAY_STATE, "w") as f:
        json.dump(st, f)


def relay_online(rid):
    return time.time() - load_relay_state()["seen"].get(rid, 0) < 90


def relay_name(rid):
    try:
        return next((r["name"] for r in load().get("relays", []) if r["id"] == rid), rid)
    except (OSError, ValueError):
        return rid


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


APP_TASK = re.compile(r"^App\d{1,2}$")
APPS_FILE = r"C:\ProgramData\wol-agent\apps.json"


def apps_from(lst):
    """[{name, task}] chosen in WOL Agent -> {button name: command}; None if unusable."""
    if isinstance(lst, dict):  # PowerShell writes a one-item list as an object
        lst = [lst]
    if not isinstance(lst, list):
        return None
    out = {}
    for a in lst:
        if isinstance(a, dict) and APP_TASK.match(str(a.get("task", ""))):
            name = re.sub(r"\s+", " ", str(a.get("name") or a["task"])).strip()[:30]
            out[name] = f'schtasks /run /tn "WOL\\{a["task"]}"'
    return out


def sync_apps(cfg, m):
    """Pick up the app buttons saved on the PC (WOL Agent -> Save)."""
    if m.get("os", "windows") != "windows":
        return
    rc, out = pc_run(m, f"type {APPS_FILE}")
    try:
        new = apps_from(json.loads(out.lstrip("\ufeff"))) if rc == 0 else None
    except ValueError:
        new = None
    if new is not None and new != m.get("apps"):
        m["apps"] = new
        save(cfg)


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
    for w in m.get("wake", []):
        if w["type"] == "relay":
            text += f"\nRelay {relay_name(w['id'])}: {'🟢 online' if relay_online(w['id']) else '🔴 offline'}"
    if note:
        text += f"\n\n{note}"
    kb = [[btn("⚡ Wake", f"w:{i}"), btn("🔄 Refresh", f"s:{i}")]]
    if m.get("pc"):
        names = list(apps(m))
        for j in range(0, len(names), 2):
            kb.append([btn(f"▶️ {n}", f"a:{i}:{j + k}") for k, n in enumerate(names[j:j + 2])])
        if "claude" in actions(m):
            kb.append([btn("🤖 Claude Remote", f"c:{i}"), btn("⏹ Stop Claude", f"k:{i}")])
        kb.append([btn("🔒 Lock", f"l:{i}"), btn("👤 Who", f"u:{i}")])
        kb.append([btn("🔁 Restart", f"r:{i}"), btn("⏻ Shut down", f"h:{i}")])
        kb.append([btn("💻 VS Code Remote-SSH", f"v:{i}")])
    kb.append([btn("📡 Relay (no public IP)", f"y:{i}")])
    kb.append([btn("🗑 Remove", f"d:{i}"), btn("⬅️ Machines", "m")])
    return text, kb


def list_view(cfg):
    lines = [f"🖥 {m['name']}: {status_line(m)}" for m in cfg["machines"]] or ["No machines yet."]
    kb = [[btn(f"🖥 {m['name']}", f"o:{i}")] for i, m in enumerate(cfg["machines"])]
    kb.append([btn("➕ Add PC", "p")])
    if auth_cfg(cfg):
        kb.append([btn("🛑 Kill switch", "x")])
    text = "Machines\n\n" + "\n".join(lines)
    if (auth_cfg(cfg) or {}).get("locked"):
        text = "🛑 Kill switch is ON - every action is blocked.\n\n" + text
    return text, kb


def one_time_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{code[:4]}-{code[4:]}"


def new_pair_code(relay_for=None):
    """One-time code: plain expiry time for a PC, {"exp", "relay"} for a relay of machine relay_for."""
    code = one_time_code()
    exp = time.time() + PAIR_TTL
    with LOCK:
        codes = load_codes()
        codes[code] = {"exp": exp, "relay": relay_for} if relay_for else exp
        save_codes(codes)
    return code


def load_codes():
    try:
        with open(PENDING) as f:
            codes = json.load(f)
    except (OSError, ValueError):
        codes = {}
    now = time.time()
    return {c: t for c, t in codes.items() if (t["exp"] if isinstance(t, dict) else t) > now}


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


def relay_view(cfg, i):
    m = cfg["machines"][i]
    srv = f"{cfg.get('public_host', '<server>')}:{cfg.get('pair_port', 8790)}"
    code = new_pair_code(m["name"])
    text = (f"📡 Relay for {m['name']}\n\n"
            "Needed when the modem has no public IP (TD-LTE, 4G, many fibre lines) or the IP keeps "
            "changing. A relay is any always-on device in the same home network: an old Android phone, "
            "a Raspberry Pi or another PC. It asks this server for wake jobs and sends the packet "
            "inside the home, so the modem needs no setup.\n\n"
            "Android: install Termux (F-Droid), then type:\n\n"
            "pkg install python\n"
            f"curl -O http://{srv}/relay.py\n"
            f"python relay.py {srv}/{code}\n\n"
            "Then pull down the Termux notification and tap \"Acquire wakelock\". "
            "Keep the phone on the charger and on the home Wi-Fi.\n"
            "Linux / Windows with Python 3: the last two lines.\n"
            "Later starts: python relay.py\n\n"
            f"The code works once and expires in {PAIR_TTL // 60} min.")
    return text, [[btn("⬅️ Back", f"s:{i}")]]


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def claude_wait(api, cfg, chat, mid, i):
    """Poll the PC until Claude Remote Control prints its link (or an error)."""
    m = cfg["machines"][i]
    out = ""
    for _ in range(12):
        time.sleep(4)
        rc, out = pc_run(m, actions(m)["claude_status"])
        out = ANSI.sub("", out)
        if re.search(r"https://claude\.ai/\S+|error|not found|not trusted|\(y/n\)", out, re.I):
            break
        if out.startswith("running") and "vscode:" in out:  # VS Code mode prints no link
            break
    url = re.search(r"https://claude\.ai/\S+", out)
    if "(y/n)" in out or "not trusted" in out.lower():
        pc_run(m, actions(m)["claude_stop"])  # it would wait for an answer forever
        note = ("🤖 Claude needs a one-time answer on the PC (trust the folder / enable Remote Control).\n"
                "On the PC open WOL Agent and press \"Claude one-time setup\", answer Enter and y, "
                "close the window. Then press 🤖 Claude Remote again.")
    elif url:
        note = (f"🤖 Claude Remote Control is on.\n\nOpen: {url.group(0)}\n"
                f"or the Claude app -> Code -> session \"{m['name']}\".")
    elif out.startswith("running") and "vscode:" in out:
        note = ("🤖 Claude is open in VS Code on the PC (window WOL-Claude, Claude panel).\n"
                "Open the Claude app or claude.ai/code -> Code: the newest session (computer icon) is that chat.")
    elif out.startswith("running"):
        note = (f"🤖 Claude Remote Control is on.\nOpen the Claude app or claude.ai/code -> Code -> "
                f"session \"{m['name']}\".\n\n" + out[-500:])
    else:
        note = "🤖 Claude did not start:\n" + out[-600:]
    t, kb = machine_view(cfg, i, note)
    api.edit(chat, mid, t, kb)


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


def handle_callback(api, cfg, cb, confirmed=False):
    """A button press. confirmed=True when it comes back from the fingerprint page."""
    chat = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    if cb.get("id"):
        try:
            api.call("answerCallbackQuery", callback_query_id=cb["id"])
        except Exception:  # noqa: BLE001
            pass
    parts = data.split(":")
    op = parts[0]
    if not confirmed and needs_auth(cfg, op):
        return ask_auth(api, cfg, str(cb["from"]["id"]), chat, mid, data)
    if op == "m":
        t, kb = list_view(cfg)
        return api.edit(chat, mid, t, kb)
    if op == "x":
        kb = [[btn("🛑 Yes, cut everything off", "X"), btn("✖️ Cancel", "m")]]
        return api.edit(chat, mid, "🛑 Kill switch\n\nEvery online PC locks its screen, stops SSH and the tunnel "
                                   "and turns its network cards off. The bot blocks every action until you undo it "
                                   "on the PC (wol-unkill.ps1) and run `wolbot unlock` on the server.", kb)
    if op == "X":
        api.edit(chat, mid, "🛑 Kill switch started...")
        threading.Thread(target=kill, args=(api, f"pressed in the bot by {cb['from']['id']}"), daemon=True).start()
        return None
    if op == "p":
        t, kb = pair_view(cfg)
        return api.edit(chat, mid, t, kb)
    i = int(parts[1])
    if i >= len(cfg["machines"]):
        return None
    m = cfg["machines"][i]
    note = ""
    if op == "o":
        pass
    elif op == "s":
        if m.get("pc") and online(m):
            sync_apps(cfg, m)
    elif op == "w":
        ok, report = wake(m)
        note = ("⚡ Magic packet sent." if ok else "❌ Wake failed.") + "\n" + "\n".join(report)
        if ok and m.get("pc") and not online(m):
            note += f"\nWaiting up to {WAKE_WAIT // 60} min for it to come online..."
            threading.Thread(target=wait_online, args=(api, cfg, chat, mid, i), daemon=True).start()
    elif op == "a":
        names = list(apps(m))
        j = int(parts[2])
        if j >= len(names):
            note = "The app list changed - try again."
        else:
            rc, out = pc_run(m, apps(m)[names[j]])
            note = f"▶️ {names[j]}: " + ("started" if rc == 0 else f"failed ({out[-200:]})")
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
    elif op == "c":
        rc, out = pc_run(m, actions(m)["claude"])
        if rc != 0:
            note = f"🤖 Claude: failed ({out[-200:]}). Run the new WOL Agent on the PC to add it."
        else:
            note = "🤖 Starting Claude Remote Control..."
            threading.Thread(target=claude_wait, args=(api, cfg, chat, mid, i), daemon=True).start()
    elif op == "k":
        rc, out = pc_run(m, actions(m)["claude_stop"])
        note = "⏹ Claude: " + (out[-200:] if rc == 0 else f"failed ({out[-200:]})")
    elif op == "y":
        t, kb = relay_view(cfg, i)
        return api.edit(chat, mid, t, kb)
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
    auth_server(api)
    threading.Thread(target=ip_watch, daemon=True).start()
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
                        stranger(api, cfg, cb["from"], f"pressed {cb.get('data', '')!r}")
                        continue
                    handle_callback(api, cfg, cb)
                elif "message" in u:
                    msg = u["message"]
                    uid = str(msg["from"]["id"])
                    if uid not in admins:
                        stranger(api, cfg, msg["from"], f"wrote {str(msg.get('text', ''))[:60]!r}")
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


def take_code(req, client_ip, relay):
    """Use up a one-time code of the right kind; returns its value. 5 wrong codes per IP per 10 min."""
    code = str(req.get("code", "")).upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    with LOCK:
        fails = [t for t in FAILS.get(client_ip, []) if t > time.time() - 600]
        if len(fails) >= 5:
            raise PermissionError("too many wrong codes, try again in 10 min")
        codes = load_codes()
        if code not in codes or isinstance(codes[code], dict) != relay:
            FAILS[client_ip] = fails + [time.time()]
            where = "PC -> Relay" if relay else "Add PC"
            raise PermissionError(f"wrong or expired code - press {where} in the bot for a new one")
        val = codes.pop(code)
        save_codes(codes)
    return val


def pair(cfg, req, client_ip):
    """Register (or re-register) a PC from a valid one-time code. Returns the agent settings."""
    take_code(req, client_ip, relay=False)
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
    if "apps" in req:  # app buttons chosen in WOL Agent
        new = apps_from(req["apps"])
        if new is not None:
            m["apps"] = new
    if not m.get("wake"):
        # any modem: UDP magic packet to its public address (needs a port forward of UDP 9)
        # "auto": ip_watch follows the home IP while the PC is online
        w = {"type": "udp", "host": req.get("wake_host") or client_ip, "port": 9}
        if not req.get("wake_host"):
            w["auto"] = True
        m["wake"] = [w]
    save(cfg)
    write_tunnel_keys(cfg)
    return m, {
        "ok": True, "name": m["name"], "server": cfg.get("public_host"), "ssh_port": cfg.get("ssh_port", 22),
        "tunnel_user": "woltun", "tunnel_port": port, "bot_key": open(KEY + ".pub").read().strip(),
    }


def relay_pair(cfg, req, client_ip):
    """Register a relay from a one-time relay code and add it as a wake method of its machine."""
    val = take_code(req, client_ip, relay=True)
    m = next((x for x in cfg["machines"] if x["name"] == val["relay"]), None)
    if m is None:
        raise ValueError("machine of this code was removed")
    rid = secrets.token_hex(4)
    r = {"id": rid, "key": secrets.token_urlsafe(24), "name": f"{m['name']}-relay"}
    cfg.setdefault("relays", []).append(r)
    m["wake"] = [w for w in m.get("wake", []) if w["type"] != "relay"] + [{"type": "relay", "id": rid}]
    used = {w["id"] for x in cfg["machines"] for w in x.get("wake", []) if w["type"] == "relay"}
    cfg["relays"] = [x for x in cfg["relays"] if x["id"] in used]  # drop the replaced relay
    save(cfg)
    return m, {"id": rid, "key": r["key"], "name": r["name"], "machine": m["name"]}


def relay_poll(cfg, qs):
    """Long poll from a relay: returns the MACs to wake (waits up to 25 s for a job)."""
    rid, key = qs.get("id", [""])[0], qs.get("key", [""])[0]
    r = next((x for x in cfg.get("relays", []) if x["id"] == rid), None)
    if r is None or not secrets.compare_digest(r["key"], key):
        raise PermissionError("unknown relay")
    end = time.time() + 25
    while True:
        with LOCK:
            st = load_relay_state()
            st["seen"][rid] = time.time()
            macs = st["queue"].pop(rid, [])
            save_relay_state(st)
        if macs or time.time() > end:
            return {"wake": macs}
        time.sleep(1)


def ip_watch():
    """Keep "auto" UDP wake targets on the home's current public IP (asked from the PC itself)."""
    while True:
        time.sleep(IP_CHECK)
        try:
            cfg = load()
            url = f"http://{cfg.get('public_host')}:{cfg.get('pair_port', 8790)}/ip"
            for m in cfg["machines"]:
                auto = [w for w in m.get("wake", []) if w["type"] == "udp" and w.get("auto")]
                if not auto or not online(m):
                    continue
                rc, out = pc_run(m, f"curl -s -m 8 {url}")
                ip = out.strip()
                if rc != 0 or not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                    continue
                cfg = load()  # re-read: the bot may have saved meanwhile
                mm = next((x for x in cfg["machines"] if x["name"] == m["name"]), {})
                changed = False
                for w in mm.get("wake", []):
                    if w["type"] == "udp" and w.get("auto") and w["host"] != ip:
                        print(f"{mm['name']}: home IP {w['host']} -> {ip}", flush=True)
                        w["host"], changed = ip, True
                if changed:
                    save(cfg)
        except Exception as e:  # noqa: BLE001 - never let the watcher die
            print(f"ip_watch: {e}", flush=True)


def pair_server(api):
    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server API
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/ip":  # the PC asks for its own public IP (ip_watch)
                body = self.client_address[0].encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/relay.py":  # so a phone in Iran can fetch the relay without GitHub
                body = open(RELAY_SRC, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/x-python")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/relay/poll":
                try:
                    return self.reply(200, relay_poll(load(), urllib.parse.parse_qs(url.query)))
                except PermissionError as e:
                    return self.reply(403, {"error": str(e)})
            return self.reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802 - http.server API
            if self.path not in ("/pair", "/relay/pair"):
                return self.reply(404, {"error": "not found"})
            relay = self.path == "/relay/pair"
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 8192)
                req = json.loads(self.rfile.read(n) or b"{}")
                cfg = load()
                m, out = (relay_pair if relay else pair)(cfg, req, self.client_address[0])
            except PermissionError as e:
                return self.reply(403, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self.reply(400, {"error": f"bad request: {e}"})
            self.reply(200, out)
            note = ("✅ Relay linked. Wake now goes through it." if relay
                    else "✅ New PC paired. It shows online once its tunnel is up.")
            for a in cfg.get("admins", []):
                try:
                    t, kb = machine_view(cfg, cfg["machines"].index(m), note)
                    api.send(str(a).lstrip("b"), t, kb)
                except Exception as e:  # noqa: BLE001
                    print(f"notify {a}: {e}", flush=True)

        def log_message(self, fmt, *args):
            if self.path.startswith("/relay/poll") or self.path == "/ip":  # frequent, and the URL holds the key
                return
            print(f"pair {self.client_address[0]}: {fmt % args}", flush=True)

    cfg = load()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", int(cfg.get("pair_port", 8790))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------- fingerprint (passkeys / WebAuthn) + kill switch ----------

def auth_cfg(cfg):
    """Fingerprint settings, or None when the bot runs without them (old behaviour)."""
    a = cfg.get("auth") or {}
    return a if a.get("url") else None


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rp(a):
    """(rp id, origin) of the confirmation site."""
    u = urllib.parse.urlsplit(a["url"])
    return u.hostname, f"{u.scheme}://{u.netloc}"


def needs_auth(cfg, op):
    a = auth_cfg(cfg)
    return bool(a) and op in a.get("protect", PROTECT)


def user_keys(cfg, uid):
    return [k for k in (auth_cfg(cfg) or {}).get("keys", []) if str(k["owner"]) == str(uid)]


def action_label(cfg, data):
    """Human name of a button press, e.g. "Shut down on PLUSPC" (also detects a changed machine list)."""
    parts = data.split(":")
    name = OP_NAMES.get(parts[0], parts[0])
    if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) < len(cfg["machines"]):
        m = cfg["machines"][int(parts[1])]
        if parts[0] == "a" and len(parts) > 2 and parts[2].isdigit():
            names = list(apps(m))
            j = int(parts[2])
            name += f" {names[j]}" if j < len(names) else " ?"
        name += f" on {m['name']}"
    return name


def notify(api, cfg, text):
    print(text.replace("\n", " | "), flush=True)
    for a in cfg.get("admins", []):
        try:
            api.send(str(a).lstrip("b"), text)
        except Exception as e:  # noqa: BLE001
            print(f"notify {a}: {e}", flush=True)


def stranger(api, cfg, user, what):
    """Someone who is not an admin talks to the bot: tell the admins (once an hour per person)."""
    uid = str(user.get("id"))
    if time.time() - NOTIFIED.get(uid, 0) < 3600:
        return
    NOTIFIED[uid] = time.time()
    who = " ".join(str(user.get(k, "")) for k in ("first_name", "last_name")).strip()
    uname = f" @{user['username']}" if user.get("username") else ""
    notify(api, cfg, f"👤 Stranger {uid} ({who}{uname}) {what} - ignored.")


def ask_auth(api, cfg, uid, chat, mid, data):
    """Hold a protected button press until the fingerprint page confirms it."""
    a = auth_cfg(cfg)
    parts = data.split(":")
    back = f"s:{parts[1]}" if len(parts) > 1 else "m"
    if a.get("locked"):
        return api.edit(chat, mid, "🛑 Kill switch is ON - every action is blocked.\n"
                                   "Undo on the PC: C:\\ProgramData\\wol-agent\\wol-unkill.ps1 (as admin), "
                                   "then on the server: wolbot unlock", [[btn("⬅️ Machines", "m")]])
    if not user_keys(cfg, uid):
        return api.edit(chat, mid, "🔐 This button needs your fingerprint, but you have no passkey yet.\n"
                                   f"On the server: wolbot passkey-add <phone name> {uid}",
                        [[btn("⬅️ Back", back)]])
    token = secrets.token_urlsafe(24)
    label = action_label(cfg, data)
    now = time.time()
    with LOCK:
        for t in [t for t, c in CONFIRMS.items() if c["exp"] < now]:
            CONFIRMS.pop(t)
        CONFIRMS[token] = {"uid": uid, "chat": chat, "mid": mid, "data": data, "label": label,
                           "exp": now + AUTH_TTL, "challenge": b64u(secrets.token_bytes(32))}
    kb = [[{"text": "🔐 Confirm with fingerprint", "url": f"{a['url'].rstrip('/')}/a/{token}"}],
          [btn("✖️ Cancel", back)]]
    return api.edit(chat, mid, f"🔐 {label}\n\nConfirm with your fingerprint within {AUTH_TTL // 60} min. "
                               "The link works once.", kb)


def run_confirmed(api, c):
    cfg = load()
    if (auth_cfg(cfg) or {}).get("locked"):
        return api.edit(c["chat"], c["mid"], "🛑 Kill switch is ON - nothing was done.", [[btn("⬅️ Machines", "m")]])
    if action_label(cfg, c["data"]) != c["label"]:
        return api.edit(c["chat"], c["mid"], "The machine list changed meanwhile - nothing was done, press the button again.",
                        [[btn("⬅️ Machines", "m")]])
    print(f"confirmed by fingerprint: {c['label']} (user {c['uid']})", flush=True)
    cb = {"id": None, "from": {"id": c["uid"]}, "data": c["data"],
          "message": {"chat": {"id": c["chat"]}, "message_id": c["mid"]}}
    return handle_callback(api, cfg, cb, confirmed=True)


def auth_fail(api, why):
    """A failed check. kill_after of them within 10 min start the kill switch."""
    cfg = load()
    limit = int((auth_cfg(cfg) or {}).get("kill_after", 3))
    now = time.time()
    with LOCK:
        AUTH_FAILS[:] = [t for t in AUTH_FAILS if t > now - 600] + [now]
        n = len(AUTH_FAILS)
    notify(api, cfg, f"⚠️ Failed check: {why} ({n}/{limit} in 10 min)")
    if limit and n >= limit:
        threading.Thread(target=kill, args=(api, f"{n} failed checks in 10 min, last: {why}"), daemon=True).start()


def kill(api, reason):
    """Kill switch: block the bot and cut every online PC off (screen lock, SSH, tunnel, network)."""
    with LOCK:
        cfg = load()
        a = cfg.setdefault("auth", {})
        a["locked"] = True
        save(cfg)
        CONFIRMS.clear()
    report = []
    for m in cfg["machines"]:
        if m.get("pc") and online(m) and "kill" in actions(m):
            rc, out = pc_run(m, actions(m)["kill"], timeout=40)
            # the PC cuts this very session, so a dropped connection (255) means it worked
            report.append(f"{m['name']}: " + ("cut off" if rc in (0, 255) else f"failed ({out[-120:]})"))
    notify(api, cfg, "🛑 KILL SWITCH\n" + reason + "\n\n" + "\n".join(report or ["no PC was online"])
           + "\n\nEvery action is blocked. Undo: on the PC run C:\\ProgramData\\wol-agent\\wol-unkill.ps1 "
             "as administrator, then on the server: wolbot unlock")


def check_client_data(raw, kind, challenge, origin):
    cd = json.loads(raw)
    if cd.get("type") != kind:
        raise ValueError("wrong type")
    if not secrets.compare_digest(str(cd.get("challenge", "")), challenge):
        raise ValueError("wrong challenge")
    if cd.get("origin") != origin:
        raise ValueError(f"wrong origin {cd.get('origin')}")


def check_auth_data(ad, rp_id):
    """-> (flags, sign count) after checking the site hash and that the user proved it is them."""
    if len(ad) < 37 or ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise ValueError("wrong site")
    flags = ad[32]
    if not flags & 0x01 or not flags & 0x04:
        raise ValueError("no user verification")
    return flags, int.from_bytes(ad[33:37], "big")


def verify_assertion(a, key, challenge, body):
    """Check a fingerprint signature (WebAuthn assertion); returns the new sign count."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    rp_id, origin = rp(a)
    cd_raw = b64d(body["clientDataJSON"])
    check_client_data(cd_raw, "webauthn.get", challenge, origin)
    ad = b64d(body["authenticatorData"])
    _, count = check_auth_data(ad, rp_id)
    if count and count <= int(key.get("count", 0)):
        raise ValueError("sign counter went back (cloned key?)")
    msg = ad + hashlib.sha256(cd_raw).digest()
    sig = b64d(body["signature"])
    pub = serialization.load_der_public_key(b64d(key["pub"]))
    alg = int(key["alg"])
    if alg == -7:
        pub.verify(sig, msg, ec.ECDSA(hashes.SHA256()))
    elif alg == -257:
        pub.verify(sig, msg, padding.PKCS1v15(), hashes.SHA256())
    elif alg == -8:
        pub.verify(sig, msg)
    else:
        raise ValueError(f"unsupported algorithm {alg}")
    return count


def verify_registration(a, challenge, body):
    """Check a new passkey (WebAuthn create) and return what to store."""
    from cryptography.hazmat.primitives import serialization

    rp_id, origin = rp(a)
    check_client_data(b64d(body["clientDataJSON"]), "webauthn.create", challenge, origin)
    flags, count = check_auth_data(b64d(body["authenticatorData"]), rp_id)
    pub = b64d(body["publicKey"])
    serialization.load_der_public_key(pub)
    alg = int(body["alg"])
    if alg not in (-7, -8, -257):
        raise ValueError(f"unsupported algorithm {alg}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,1400}", str(body.get("id", ""))):
        raise ValueError("bad credential id")
    return {"id": body["id"], "pub": b64u(pub), "alg": alg, "count": count,
            "synced": bool(flags & 0x10), "added": time.strftime("%Y-%m-%d %H:%M")}


def load_enroll():
    try:
        with open(ENROLL) as f:
            codes = json.load(f)
    except (OSError, ValueError):
        codes = {}
    return {c: e for c, e in codes.items() if e["exp"] > time.time()}


def save_enroll(codes):
    with open(ENROLL, "w") as f:
        json.dump(codes, f)
    os.chmod(ENROLL, 0o600)


AUTH_HTML = r"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>wolbot</title>
<style>
:root{--bg:#0f1117;--card:#181b24;--fg:#e8eaf0;--mut:#9aa1b2;--acc:#6c8cff;--ok:#3ecf8e;--err:#ff6b6b;--line:#2a2f3c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;min-height:100vh}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 32px;border-bottom:1px solid var(--line)}
header b{font-size:18px}
.lang button{background:none;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:4px 10px;cursor:pointer}
.lang button.on{color:var(--fg);border-color:var(--acc)}
main{width:100%;padding:24px 32px}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;text-align:center}
.icon{font-size:64px;line-height:1}
h1{font-size:22px;margin:12px 0 4px}
.what{font-size:18px;color:var(--acc);margin:8px 0 20px;direction:ltr;unicode-bidi:plaintext}
#go{background:var(--acc);color:#fff;border:0;border-radius:12px;padding:16px 28px;font-size:18px;width:100%;max-width:520px;cursor:pointer}
#go:disabled{opacity:.5}
#msg{margin-top:18px;min-height:1.7em;color:var(--mut)}
#msg.ok{color:var(--ok)} #msg.err{color:var(--err)}
.note{color:var(--mut);font-size:14px;margin-top:20px}
@media(max-width:600px){header,main{padding-left:16px;padding-right:16px}}
</style></head><body>
<header><b>🔐 wolbot</b><span class="lang"><button data-l="fa">فارسی</button> <button data-l="en">English</button></span></header>
<main><div class="box">
<div class="icon" id="icon">👆</div>
<h1 id="title"></h1>
<div class="what" id="what"></div>
<button id="go"></button>
<div id="msg"></div>
<div class="note" id="note"></div>
</div></main>
<script>
const D = __DATA__;
const T = {
 fa: {get: "تأیید با اثر انگشت", create: "ثبت این گوشی", gone: "این لینک منقضی شده یا قبلاً استفاده شده",
      btnget: "تأیید", btncreate: "ثبت اثر انگشت", wait: "منتظر اثر انگشت...", send: "در حال بررسی...",
      okget: "✅ تأیید شد. نتیجه را در بله ببینید.", okcreate: "✅ این گوشی ثبت شد. حالا دکمه‌های ربات اثر انگشت می‌خواهند.",
      bad: "❌ رد شد:", cancel: "لغو شد یا خطا داد:", old: "مرورگر شما Passkey را پشتیبانی نمی‌کند. با Chrome باز کنید.",
      noteget: "لینک فقط یک بار و تا ۲ دقیقه کار می‌کند. اگر رد شد، دوباره دکمه را در بله بزنید.",
      notecreate: "لینک فقط یک بار کار می‌کند. قفل گوشی باید PIN قوی باشد.", gonenote: "دکمه را دوباره در بله بزنید."},
 en: {get: "Confirm with your fingerprint", create: "Register this phone", gone: "This link expired or was already used",
      btnget: "Confirm", btncreate: "Register fingerprint", wait: "Waiting for your fingerprint...", send: "Checking...",
      okget: "✅ Confirmed. See the result in the bot.", okcreate: "✅ This phone is registered. Bot buttons now need your fingerprint.",
      bad: "❌ Refused:", cancel: "Cancelled or failed:", old: "This browser has no passkey support. Open the link in Chrome.",
      noteget: "The link works once, for 2 minutes. If it is refused, press the button in the bot again.",
      notecreate: "The link works once. Your phone lock should be a strong PIN.", gonenote: "Press the button in the bot again."}
};
let L = localStorage.getItem("lang") || "fa", state = null;
const $ = id => document.getElementById(id);
function paint() {
  const t = T[L];
  document.documentElement.lang = L; document.documentElement.dir = L === "fa" ? "rtl" : "ltr";
  document.querySelectorAll(".lang button").forEach(b => b.classList.toggle("on", b.dataset.l === L));
  $("title").textContent = t[D.mode]; $("what").textContent = D.what || "";
  $("go").textContent = t["btn" + D.mode] || ""; $("go").style.display = D.mode === "gone" ? "none" : "";
  $("note").textContent = D.mode === "gone" ? t.gonenote : t["note" + D.mode];
  if (D.mode === "gone") $("icon").textContent = "⌛";
  if (state) { $("msg").textContent = state[0] === "raw" ? state[1] : t[state[0]] + (state[1] ? " " + state[1] : ""); $("msg").className = state[2] || ""; }
}
function show(key, extra, cls) { state = [key, extra || "", cls]; paint(); }
document.querySelectorAll(".lang button").forEach(b => b.onclick = () => { L = b.dataset.l; try { localStorage.setItem("lang", L) } catch (e) {} paint(); });
const enc = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const dec = s => { s = s.replace(/-/g, "+").replace(/_/g, "/"); return Uint8Array.from(atob(s + "=".repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0)); };
async function go() {
  if (!window.PublicKeyCredential) return show("old", "", "err");
  $("go").disabled = true; show("wait");
  let body;
  try {
    if (D.mode === "get") {
      const c = await navigator.credentials.get({publicKey: {challenge: dec(D.challenge), rpId: D.rpId, timeout: 60000,
        userVerification: "required", allowCredentials: D.allow.map(id => ({type: "public-key", id: dec(id)}))}});
      body = {id: c.id, clientDataJSON: enc(c.response.clientDataJSON), authenticatorData: enc(c.response.authenticatorData), signature: enc(c.response.signature)};
    } else {
      const c = await navigator.credentials.create({publicKey: {challenge: dec(D.challenge), rp: {id: D.rpId, name: "wolbot"},
        user: {id: dec(D.uid), name: D.name, displayName: D.name}, timeout: 120000, attestation: "none",
        pubKeyCredParams: [-7, -8, -257].map(alg => ({type: "public-key", alg})),
        authenticatorSelection: {userVerification: "required", residentKey: "preferred"}}});
      const r = c.response;
      if (!r.getPublicKey || !r.getAuthenticatorData) { $("go").disabled = false; return show("old", "", "err"); }
      body = {id: c.id, clientDataJSON: enc(r.clientDataJSON), authenticatorData: enc(r.getAuthenticatorData()), publicKey: enc(r.getPublicKey()), alg: r.getPublicKeyAlgorithm()};
    }
  } catch (e) { $("go").disabled = false; return show("cancel", e.name || e.message, "err"); }
  show("send");
  try {
    const res = await fetch(location.pathname, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await res.json().catch(() => ({}));
    if (res.ok) { $("icon").textContent = "✅"; $("go").style.display = "none"; show("ok" + D.mode, "", "ok"); }
    else { $("icon").textContent = "⛔"; $("go").style.display = "none"; show("bad", j.error || res.status, "err"); }
  } catch (e) { show("cancel", e.message, "err"); }
}
$("go").onclick = go;
paint();
</script></body></html>
"""


def auth_server(api):
    """Confirmation pages behind the reverse proxy (Caddy): /a/<token> = confirm, /e/<code> = add a passkey."""
    a = auth_cfg(load())
    if not a:
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def head(self, code, ctype, n):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(n))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.head(code, "application/json", len(body))
            self.wfile.write(body)

        def page(self, data, code=200):
            body = AUTH_HTML.replace("__DATA__", json.dumps(data).replace("</", "<\\/")).encode()
            self.head(code, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)

        def ip(self):
            return self.headers.get("X-Real-IP") or self.client_address[0]

        def do_GET(self):  # noqa: N802 - http.server API
            cfg = load()
            a = auth_cfg(cfg)
            path = urllib.parse.urlsplit(self.path).path
            gone = {"mode": "gone"}
            m = re.fullmatch(r"/a/([\w-]{20,64})", path)
            if a and m:
                c = CONFIRMS.get(m.group(1))
                if not c or c["exp"] < time.time() or a.get("locked"):
                    return self.page(gone, 410)
                return self.page({"mode": "get", "challenge": c["challenge"], "rpId": rp(a)[0], "what": c["label"],
                                  "allow": [k["id"] for k in user_keys(cfg, c["uid"])]})
            m = re.fullmatch(r"/e/([A-Z0-9]{4}-[A-Z0-9]{4})", path)
            if a and m:
                e = load_enroll().get(m.group(1))
                if not e:
                    return self.page(gone, 410)
                ch = ENROLL_CH.setdefault(m.group(1), b64u(secrets.token_bytes(32)))
                return self.page({"mode": "create", "challenge": ch, "rpId": rp(a)[0], "what": e["name"],
                                  "uid": b64u(str(e["owner"]).encode()), "name": f"wolbot {e['name']}"})
            return self.reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802 - http.server API
            path = urllib.parse.urlsplit(self.path).path
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 16384)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.reply(400, {"error": "bad request"})
            m = re.fullmatch(r"/a/([\w-]{20,64})", path)
            if m:
                return self.confirm(m.group(1), body)
            m = re.fullmatch(r"/e/([A-Z0-9]{4}-[A-Z0-9]{4})", path)
            if m:
                return self.enroll(m.group(1), body)
            return self.reply(404, {"error": "not found"})

        def confirm(self, token, body):
            with LOCK:
                c = CONFIRMS.pop(token, None)  # one try per link
            if not c or c["exp"] < time.time():
                return self.reply(410, {"error": "expired"})
            try:
                with LOCK:
                    cfg = load()
                    a = auth_cfg(cfg)
                    if not a or a.get("locked"):
                        raise ValueError("kill switch is on")
                    key = next((k for k in user_keys(cfg, c["uid"]) if k["id"] == body.get("id")), None)
                    if key is None:
                        raise ValueError("unknown passkey")
                    key["count"] = verify_assertion(a, key, c["challenge"], body)
                    key["used"] = time.strftime("%Y-%m-%d %H:%M")
                    save(cfg)
            except Exception as e:  # noqa: BLE001 - any failure is a refused check
                self.reply(403, {"error": str(e)[:120] or "bad signature"})
                api.edit(c["chat"], c["mid"], f"⛔ Fingerprint check failed: {c['label']}\nNothing was done.",
                         [[btn("⬅️ Machines", "m")]])
                return auth_fail(api, f"{c['label']}: {str(e)[:80] or 'bad signature'} (from {self.ip()})")
            self.reply(200, {"ok": True})
            threading.Thread(target=run_confirmed, args=(api, c), daemon=True).start()
            return None

        def enroll(self, code, body):
            with LOCK:
                codes = load_enroll()
                e = codes.pop(code, None)
                ch = ENROLL_CH.pop(code, None)
                if e:
                    save_enroll(codes)
            if not e or not ch:
                auth_fail(api, f"passkey sign-up with a wrong or used code (from {self.ip()})")
                return self.reply(410, {"error": "expired"})
            try:
                with LOCK:
                    cfg = load()
                    key = verify_registration(auth_cfg(cfg), ch, body)
                    key.update(owner=str(e["owner"]), name=e["name"])
                    keys = cfg["auth"].setdefault("keys", [])
                    keys[:] = [k for k in keys if k["id"] != key["id"]] + [key]
                    save(cfg)
            except Exception as ex:  # noqa: BLE001
                return self.reply(400, {"error": str(ex)[:120]})
            self.reply(200, {"ok": True})
            notify(api, cfg, f"🔑 Passkey '{e['name']}' added for {e['owner']} (from {self.ip()})"
                   + (" - synced to the phone's account (Google/Apple)" if key["synced"] else ""))
            return None

        def log_message(self, fmt, *args):
            print(f"auth {self.ip()}: {fmt % args}"[:200], flush=True)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(a.get("port", 8791))), Handler)
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
  wolbot relay-code <name>                     one-time code for relay.py (modem without public IP)
  wolbot pubkey                                key to put on the PC (setup-pc.ps1 -BotKey)

Fingerprint confirmation (passkeys) + kill switch:
  wolbot auth <https://auth.example.com> [port]
                                               turn it on; the reverse proxy sends that site to
                                               127.0.0.1:<port> (default 8791). `wolbot auth off` = off
  wolbot passkey-add <name> [user id]          one-time link to register a phone (default: first admin)
  wolbot passkey-list | passkey-del <name>
  wolbot kill                                  kill switch now (same as the bot button)
  wolbot unlock                                allow actions again after a kill switch
"""


def auth_cli(cmd, args):
    cfg = load()
    a = cfg.setdefault("auth", {})
    if cmd == "auth":
        if not args:
            raise SystemExit("wolbot auth <https://auth.example.com> [port]   or   wolbot auth off")
        if args[0] == "off":
            a["url"] = ""
        else:
            if not re.fullmatch(r"https://[\w.-]+(:\d+)?/?", args[0]):
                raise SystemExit("needs an https:// address with no path, e.g. https://auth.example.com")
            try:
                import cryptography  # noqa: F401 - only checks it is there
            except ImportError:
                raise SystemExit("needs the Python cryptography module: apt install python3-cryptography")
            a["url"] = args[0].rstrip("/")
            a["port"] = int(args[1]) if len(args) > 1 else a.get("port", 8791)
            a.setdefault("protect", PROTECT)
            a.setdefault("kill_after", 3)
            a.setdefault("keys", [])
        save(cfg)
        return print("Saved. Restart the bot: systemctl restart wol-bot")
    if cmd == "unlock":
        a["locked"] = False
        save(cfg)
        return print("Unlocked: bot actions work again (no restart needed).")
    if cmd == "kill":
        return kill(Api(cfg), "started on the server (wolbot kill)")
    if cmd == "passkey-list":
        for k in a.get("keys", []):
            print(f"{k['name']}  owner={k['owner']}  added={k['added']}  last used={k.get('used', '-')}"
                  + ("  (synced)" if k.get("synced") else ""))
        return print("Kill switch: ON" if a.get("locked") else f"{len(a.get('keys', []))} passkey(s)")
    if cmd == "passkey-del":
        before = len(a.get("keys", []))
        a["keys"] = [k for k in a.get("keys", []) if k["name"] != args[0]]
        save(cfg)
        return print(f"Removed {before - len(a['keys'])} passkey(s).")
    # passkey-add
    if not auth_cfg(cfg):
        raise SystemExit("Turn it on first: wolbot auth https://auth.example.com")
    if not args:
        raise SystemExit("wolbot passkey-add <name> [user id]")
    owner = args[1] if len(args) > 1 else str((cfg.get("admins") or [""])[0]).lstrip("b")
    if owner not in {str(x).lstrip("b") for x in cfg.get("admins", [])}:
        raise SystemExit(f"{owner} is not an admin (wolbot admin <id>)")
    code = one_time_code()
    codes = load_enroll()
    codes[code] = {"exp": time.time() + PAIR_TTL, "owner": owner, "name": re.sub(r"[^\w .-]", "", args[0])[:30]}
    save_enroll(codes)
    return print(f"Open on the phone (Chrome / Safari), within {PAIR_TTL // 60} min, once:\n"
                 f"{a['url'].rstrip('/')}/e/{code}")


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
    if cmd == "relay-code":
        cfg = load()
        m = find(cfg, argv[1])
        return print(f"python relay.py {cfg.get('public_host', '<server>')}:{cfg.get('pair_port', 8790)}/"
                     f"{new_pair_code(m['name'])}  (one use, {PAIR_TTL // 60} min)")
    if cmd in ("auth", "passkey-add", "passkey-list", "passkey-del", "kill", "unlock"):
        return auth_cli(cmd, argv[1:])
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
        m["wake"] = [x for x in m.get("wake", []) if not (x["type"] == w["type"] and x.get("host") == w["host"])]
        m["wake"].append(w)
    elif cmd == "del":
        cfg["machines"] = [x for x in cfg["machines"] if x["name"].lower() != argv[1].lower()]
    elif cmd == "list":
        for m in cfg["machines"]:
            print(f"{m['name']}  {m['mac']}  {status_line(m)}  wake={[w['type'] + ':' + w.get('host', w.get('id', '')) for w in m.get('wake', [])]}"
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
