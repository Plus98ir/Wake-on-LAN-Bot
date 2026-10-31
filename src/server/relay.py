#!/usr/bin/env python3
"""wolbot relay - sends Wake-on-LAN packets inside the home network for a wolbot server.

For modems without a public IP (TD-LTE, 4G, CGNAT) or with a changing IP: an always-on device
in the same home (old Android phone with Termux, Raspberry Pi, another PC) asks the server for
wake jobs over plain outgoing HTTP, so no port forward is needed. Standard library only.

  First run:  python relay.py <server>:8790/<CODE>    (code from the bot: PC -> Relay)
  Later:      python relay.py
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay.json")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def magic_packet(mac):
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16


def broadcast_addrs():
    addrs = ["255.255.255.255"]
    try:  # this device's LAN address -> its /24 broadcast (no packet is sent by connect)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        addrs.append(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    return addrs


def send(mac):
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for addr in broadcast_addrs():
                for port in (9, 7):
                    try:
                        s.sendto(pkt, (addr, port))
                    except OSError:
                        pass
            time.sleep(0.2)
    log(f"magic packet sent to {mac}")


def pair(arg):
    arg = arg.strip().replace("http://", "")
    if "/" not in arg:
        raise SystemExit("Code looks like: 1.2.3.4:8790/ABCD-2345")
    server, code = arg.rsplit("/", 1)
    req = urllib.request.Request(f"http://{server}/relay/pair", json.dumps({"code": code}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Pairing failed: {e.read().decode(errors='replace')}")
    conf = {"server": server, "id": j["id"], "key": j["key"], "name": j["name"]}
    with open(CONF, "w") as f:
        json.dump(conf, f)
    log(f"paired as relay '{j['name']}' for {j['machine']}. Settings saved to {CONF}")
    return conf


def loop(conf):
    q = urllib.parse.urlencode({"id": conf["id"], "key": conf["key"]})
    url = f"http://{conf['server']}/relay/poll?{q}"
    log(f"relay '{conf['name']}' running - keep this device on and in the same network as the PC")
    while True:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                for mac in json.load(r).get("wake", []):
                    send(mac)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise SystemExit("Server refused this relay (removed?). Pair again with a new code.")
            log(f"server: HTTP {e.code}, retrying in 10 s")
            time.sleep(10)
        except Exception as e:  # noqa: BLE001 - network down etc.: keep trying
            log(f"no connection ({e}), retrying in 10 s")
            time.sleep(10)


def main():
    if len(sys.argv) > 1:
        conf = pair(sys.argv[1])
    else:
        try:
            with open(CONF) as f:
                conf = json.load(f)
        except OSError:
            raise SystemExit(__doc__)
    try:
        loop(conf)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
