<p align="center"><img src="src/pc/app/icon.png" width="96" alt="WOL Agent"></p>

<h1 align="center">Wake-on-LAN Bot</h1>

<p align="center">Turn your PC on from your phone and start its apps — through a Bale or Telegram bot.<br>
<a href="#فارسی">فارسی</a> · <a href="https://plus98ir.github.io/Wake-on-LAN-Bot/">Website</a> · <a href="../../releases/latest">Download</a></p>

---

## English

### What it does
- **Wake** a shut-down PC from a bot on your phone (Bale or Telegram).
- **Control** it once it is on: start v2rayN or VS Code, lock, restart, shut down, see who is logged on.
- **VS Code from anywhere** with Remote-SSH, no open ports at home.
- Works behind **any modem / NAT**: the PC connects out to your server, nothing is exposed at home.

### How it fits together
```
Phone (bot)  ──►  Your Linux server (wolbot)  ──►  router / modem  ──magic packet──►  PC
                          ▲                                                        │
                          └──────────── reverse SSH tunnel (opened by the PC) ◄────┘
```
| Part | Where | What |
|---|---|---|
| `wolbot` | Linux server (Ubuntu/Debian, Python 3 only) | the bot, pairing, wake, PC control |
| `WOL Agent.exe` | each Windows PC | one-time setup: detects the PC and links it to the bot |
| router / modem | home | delivers the magic packet (OpenWrt over SSH, or any modem with a UDP port forward) |

### Setup
**1. Server** (once)
```bash
git clone https://github.com/Plus98ir/Wake-on-LAN-Bot.git
cd Wake-on-LAN-Bot/src/server
sudo bash install.sh --token <BOT_TOKEN> --public-host <server IP or domain>
```
- Bale token: [@botfather](https://ble.ir/botfather) in Bale. Telegram: `wolbot set-token <token> telegram`.
- Send any message to your bot: it replies `Not allowed. Your id: 123`. Then `wolbot admin 123 && systemctl restart wol-bot`.
- Open TCP port **8790** (pairing) if your server has a firewall.

**2. Each PC** (once)
1. BIOS: **ErP** = Disabled, **Power On By PCI-E / Wake on LAN** = Enabled. The PC must be on a **cable**.
2. In the bot press **➕ Add PC**, run **WOL Agent.exe**, paste the code, press **Install & link**.
3. Windows auto-logon (so apps can start after a wake): [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon). The agent locks the screen right after logon.

**3. Modem** — pick one:
- **Any modem:** forward **UDP 9** to the PC's IP and reserve that IP in DHCP. The bot sends the packet to your public IP (or DDNS name set in the app).
- **OpenWrt / Linux box at home:** `wolbot add-wake <PC> ssh <host> <port> root br-lan` (needs `etherwake`).

> Many cheap modems forget the PC's address a few minutes after it shuts down and then drop the UDP packet. If waking fails only after a long time off, use the OpenWrt/Linux method or a modem with static ARP.

### Server commands
```
wolbot list | wake <name> | status <name> | pair-code | del <name>
wolbot add-wake <name> udp <host> [port]
wolbot add-wake <name> ssh <host> <port> <user> [iface]
```
Config: `/etc/wol-bot/config.json`. Service: `systemctl status wol-bot`. Uninstall: `bash install.sh --uninstall`.

### VS Code Remote-SSH
Paste your laptop's `id_ed25519.pub` into the app (“Laptop key”), then the bot button **VS Code Remote-SSH** shows the `~/.ssh/config` block (ProxyJump through your server).

### Build the app
```
cd src/pc/app
go build -trimpath -ldflags "-H windowsgui -s -w" -o "WOL Agent.exe" .
```
No dependencies. Icon/manifest live in `rsrc_windows_amd64.syso` (`windres -O coff -F pe-x86-64 -i app.rc -o rsrc_windows_amd64.syso`).

### Security notes
- Tunnel user `woltun` has no shell and may only open its own port on `127.0.0.1`.
- Pair codes are one-time, live 15 minutes; 5 wrong codes block an IP for 10 minutes. Only public keys cross the pairing port.
- The bot answers only the admin ids in the config.

---

<div dir="rtl">

## فارسی

### چه کار می‌کند؟
- **روشن کردن** کامپیوتر خاموش از توی گوشی، با بات بله یا تلگرام.
- **کنترل** بعد از روشن شدن: اجرای v2rayN و VS Code، قفل، ری‌استارت، خاموش کردن، دیدن کاربر واردشده.
- **VS Code از هر جا** با Remote-SSH، بدون باز کردن پورت در خانه.
- پشت **هر مودمی** کار می‌کند: کامپیوتر خودش به سرور وصل می‌شود.

### سه تکه دارد
| تکه | کجا | کارش |
|---|---|---|
| بات (`wolbot`) | سرور لینوکسی | ریموت کنترل توی گوشی |
| `WOL Agent.exe` | هر کامپیوتر ویندوزی | یک‌بار اجرا می‌شود تا کامپیوتر به بات وصل شود |
| مودم / روتر | خانه | بسته‌ی روشن‌کردن را به کامپیوتر می‌رساند |

### راه‌اندازی
**۱. سرور (یک‌بار)**
```bash
git clone https://github.com/Plus98ir/Wake-on-LAN-Bot.git
cd Wake-on-LAN-Bot/src/server
sudo bash install.sh --token <توکن-بات> --public-host <آی‌پی-سرور>
```
- توکن را از [@botfather](https://ble.ir/botfather) در بله بگیر.
- به بات پیام بده؛ جواب می‌دهد `Your id: 123`. بعد: `wolbot admin 123 && systemctl restart wol-bot`
- اگر سرور فایروال دارد، پورت TCP **8790** را باز کن.

**۲. هر کامپیوتر (یک‌بار)**
1. BIOS: گزینه‌ی **ErP** = Disabled و **Power On By PCI-E** (یا Wake on LAN) = Enabled. کامپیوتر باید **با کابل** وصل باشد.
2. در بات بزن **➕ Add PC**، برنامه‌ی **WOL Agent.exe** را اجرا کن، کد را بچسبان و «نصب و اتصال» را بزن.
3. ورود خودکار ویندوز با [Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon). صفحه بلافاصله قفل می‌شود.

**۳. مودم — یکی از این دو:**
- **هر مودمی:** پورت **UDP 9** را به آی‌پی کامپیوتر فوروارد کن و آن آی‌پی را رزرو کن.
- **OpenWrt:** `wolbot add-wake <اسم> ssh <آدرس> <پورت> root br-lan`

> بعضی مودم‌ها چند دقیقه بعد از خاموش شدن کامپیوتر آدرسش را فراموش می‌کنند. اگر بعد از مدت طولانی روشن نشد، روش OpenWrt را استفاده کن.

### استفاده
در بات: ⚡ Wake (روشن) · ▶️ v2rayN / VS Code (اجرا) · 🔒 Lock (قفل) · ⏻ Shut down (خاموش) · ➕ Add PC (کامپیوتر جدید)

</div>
