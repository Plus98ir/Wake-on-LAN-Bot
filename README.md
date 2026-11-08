<p align="center"><img src="Wake-on-LAN-Bot.png" alt="Wake-on-LAN Bot"></p>

<h1 align="center">Wake-on-LAN Bot</h1>

<p align="center">Turn your PC on from your phone, control it, and get an alert the moment something changes on it. Phone app (PWA), with an optional Bale or Telegram bot.<br>
<a href="#فارسی">فارسی</a> · <a href="https://corenetbase.com/Wake-on-LAN-Bot/">Website</a> · <a href="../../releases/latest">Download</a></p>

---

## English

### What it does
- **Wake** a shut-down PC from your phone: installable app (PWA), or a Bale / Telegram bot if you want one.
- **Control** it once it is on: start the apps you pick (up to 12), lock, restart, shut down, see who is logged on.
- **Claude Remote**: start Claude Code Remote Control in the background, or in a VS Code window you can see at the PC.
- **VS Code from anywhere** with Remote-SSH.
- **Wake from outside on most modems**: WOL Agent opens the wake port by UPnP (UDP + TCP). No public IP (CGNAT, TD-LTE, 4G)? A small relay inside the home sends the packet.
- **No open ports for control**: the PC connects out to your server through a reverse SSH tunnel.
- **Fingerprint on every action** (passkey), and each PC checks that signature itself, so a hacked server cannot run anything on it.
- **Watchdog**: a change that could open a way in locks the screen and asks you. It names the file and its signature, so you can tell your own install from an attack. Dangerous changes are undone within about 15 s.
- **Phone alerts with sound**, even with the app closed.
- **Kill switch** with a recovery page: password at the PC + fingerprint on the phone bring everything back.

### How it fits together
```
Phone (app or bot)  ──►  Your Linux server (wolbot)  ──►  router / modem  ──magic packet──►  PC
                                 ▲                                                        │
                                 └──────────── reverse SSH tunnel (opened by the PC) ◄────┘
```
| Part | Where | What |
|---|---|---|
| `wolbot` | Linux server (Ubuntu/Debian, Python 3 + `python3-cryptography`) | phone app, optional bot, pairing, wake, PC control, alerts |
| `WOL Agent.exe` | each Windows PC | one-time setup: links the PC, installs the tunnel, gate and watchdog, opens the modem port |
| `wol-gate.exe` | each Windows PC | checks every action's fingerprint signature; runs the watchdog and the recovery page |
| router / modem | home | delivers the magic packet (UPnP or a manual forward, a relay, or OpenWrt over SSH) |

### Setup
This section sets up the bot. For the app only, without a bot, see [Phone app](#phone-app-pwa---no-bale--telegram-needed).

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
2. In the app or bot press **➕ Add PC**, run **WOL Agent.exe**, paste the code, choose a kill-switch **recovery password** (required the first time), press **Install & link**.
3. Windows auto-logon (so apps can start after a wake): [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon). The agent locks the screen right after logon.

**3. Modem** — WOL Agent tries the first option by itself and tells you if it cannot work:
- **Public IP and UPnP on the modem (automatic):** WOL Agent forwards UDP + TCP port 9 (or 47009 where the modem refuses low ports) and refreshes it every hour. New PCs get both UDP and TCP wake; TCP carries the magic packet inside raw TCP segments, for networks that drop UDP. The server follows your home IP while the PC is on.
- **No public IP (TD-LTE, 4G, CGNAT) or a changing IP → relay.** Any always-on device in the same home network (old Android phone with [Termux](https://f-droid.org/packages/com.termux/), Raspberry Pi, another PC) runs `relay.py`. It asks the server for wake jobs over outgoing HTTP and sends the packet inside the home. No modem setup. In the bot: PC → **📡 Relay**, then on the phone:
  ```
  pkg install python
  curl -O http://<server>:8790/relay.py
  python relay.py <server>:8790/<CODE>
  ```
  In Termux tap **Acquire wakelock** in its notification and keep the phone charging.
- **Public IP, no UPnP:** forward **UDP 9** (and/or TCP 9) to the PC's IP and reserve that IP in DHCP.
- **OpenWrt / Linux box at home:** `wolbot add-wake <PC> ssh <host> <port> root br-lan` (needs `etherwake`).

> Many cheap modems forget the PC's address a few minutes after it shuts down and then drop the packet. If waking fails only after a long time off, bind the IP to the MAC in the modem or use a relay. On a PC that has been off for long, the modem route depends on your modem: test it once.

### Server commands
```
wolbot list | wake <name> | status <name> | pair-code | del <name>
wolbot add-wake <name> udp <host> [port]
wolbot add-wake <name> tcp <host> [port]
wolbot add-wake <name> ssh <host> <port> <user> [iface]
wolbot relay-code <name>
wolbot auth <https://auth.example.com> [port] | auth off
wolbot passkey-add <name> [user id] | passkey-list | passkey-del <name>
wolbot auth login <minutes> | login off
wolbot user add <id> <name> <perm,perm|all> [machine,...] | user list | user del <id>
wolbot kill | unlock
wolbot gate <name> on|off
```
Config: `/etc/wol-bot/config.json`. Service: `systemctl status wol-bot`. Uninstall: `bash install.sh --uninstall`.

### Phone app (PWA) - no Bale / Telegram needed
After `wolbot auth https://auth.example.com`, that address is also an installable phone app: open it in Chrome / Safari, **Add to Home screen**, log in with your fingerprint. Same buttons as the bot, live status every 5 s, the fingerprint prompt right inside the app, admins see the events log. Without a bot token (`token` empty in the config) wolbot runs as the app only. Login = passkey (no password), session cookie `HttpOnly; Secure; SameSite=Strict`, every request needs the app's own header and origin.

**Phone alerts with sound:** tap **🔕 Alerts off** in the app, allow notifications, and a test arrives. From then on the phone is told about watchdog changes, the kill switch, unlock requests, failed checks, new passkeys and strangers, even with the app closed. Everyday things (logins, starting apps) stay silent. It is Web Push, encrypted end to end so the push service cannot read it, and turning it on for a new device is itself an alert.
- The sound is the phone's own notification sound: a web app cannot pick another or ring through silent mode.
- Alerts can take up to about 30 s: the server checks each PC every 15 s and waits a moment so the alert can name the file. An app left open also beeps.
- iPhone: **Add to Home Screen** first, open the app from that icon, then tap the bell.
- The server must reach the push services (Google, Apple, Mozilla, Microsoft) and the phone must reach its own. If turning alerts on fails behind a VPN, try once without it.

**Quick start for your own setup**
1. Server: `sudo bash install.sh --public-host <server>` (no bot token needed), then a domain with HTTPS to `127.0.0.1:8791` and `wolbot auth https://auth.example.com && systemctl restart wol-bot`.
2. Your phone: `wolbot admin <any id you choose, e.g. 1>`, `wolbot passkey-add Phone 1`, open the link on the phone, touch the sensor.
3. Each PC: `wolbot pair-code` on the server, run **WOL Agent.exe** on the PC, paste the code. The PC gate (below) is installed and trusts the passkeys registered at that moment.
4. Open `https://auth.example.com` on the phone, **Add to Home screen**, log in.

### PC gate - the PC approves every action itself
WOL Agent installs `wol-gate.exe` and forces the server's key to it (`command=` in `administrators_authorized_keys`): no shell, no terminal, no port forwarding. Every action that changes something (lock, who, restart, shut down, apps, Claude) must carry a fresh passkey signature over exactly that action, on exactly that PC, with a time and a one-time nonce, and the **PC checks it against the passkeys it trusts**. A hacked server cannot run anything on the PC. Without a signature it only gets online/offline, the public IP, button names, Claude running yes/no and the watchdog's alerts (what changed: names, file paths, signers), and it can start the kill switch or deny a change. So at worst it can cut the PC off or show you wrong text in the app, not get in. The notice on the PC's own screen does not pass through the server.
- Every request is logged in `C:\ProgramData\wol-agent\gate.log` and shown on the PC screen; 3 refused requests in 10 minutes and the PC itself starts the kill switch.
- **Watchdog** (task `WOL\Watch`, SYSTEM, every 15 s): any change below locks the screen, shows a notice on the PC, alerts your phone and adds a card in the app. **Approve with your fingerprint** (checked by the PC) or **deny** (this starts the kill switch).

  | What changed | Without an answer | Undone by itself |
  |---|---|---|
  | Remote Desktop, UAC, firewall | kill switch after 90 s | yes, back to the approved setting within about 15 s |
  | administrators, users, a running remote-control tool (AnyDesk, TeamViewer, RustDesk, Chrome Remote Desktop...) | kill switch after 90 s | yes: extra members removed, new accounts disabled, the tool closed |
  | SSH keys / sshd settings, trusted passkeys, a failed Windows logon | kill switch after 90 s | no |
  | a new or removed service or scheduled task | stays locked once, waits in the app with no deadline | no; undoing it yourself closes the alert |

- **It tells you what changed.** For a new service, task or remote tool you see the name, file, arguments, account and signer, with a verdict: **✅ looks like a normal install** (valid signature, inside a program folder) or **🚨 suspicious** (no or bad signature, file not found, outside the program folders, or a program that runs other commands, such as PowerShell or `cmd`, whose Microsoft signature proves nothing). Installing Steam, for example, reads: `new service "Steam Client Service", signed: Valve Corp.` The verdict is only a hint, because a signed tool can still be unwanted. The real question stays "did I just do this?": if not, deny.
- **Approving covers only what you were shown.** Something added after that opens its own alert and locks again, so nothing can slip through behind an approval.
- **Deny** starts the full kill switch, even for a small change, and does not remove the change itself: clean it up at the PC, or the alert returns. For something you installed, press approve.
- `wol-gate snapshot` shows what it watches, `wol-gate explain <services|tasks|remote> <name>` what it would report; the full history is in `gate.log`.
- **After a kill switch**: the PC shows a red full-screen page that asks for your **recovery password** (separate from Windows; set it in WOL Agent, or in an admin terminal: `C:\ProgramData\wol-agent\wol-gate.exe set-recovery`). The right password brings back only the internet and sends an unlock request to the app; approve it with your fingerprint and the PC itself checks the signature and brings SSH, the tunnel and the server back. 5 wrong passwords lock it for 15 min and longer; no approval in time = internet off again. Without a recovery password (or with a lost phone): `C:\ProgramData\wol-agent\wol-unkill.ps1` as administrator, then `wolbot unlock`.
- Trusted passkeys: `wol-gate list`. A new phone: register it on the server, then **WOL Agent -> Update** (or `wol-gate trust https://auth.example.com` to pick keys by hand). Remove one: `wol-gate untrust <name>`.
- Any other key in `administrators_authorized_keys` (e.g. a laptop for VS Code Remote-SSH) is a door around the gate: keep that file to the gated key only.
- Recommended on the PC (admin PowerShell): block all inbound connections (the tunnel is outgoing, Wake-on-LAN works below Windows), make UAC ask, and stop remote-desktop tools you do not use:
  ```powershell
  Set-NetFirewallProfile -All -DefaultInboundAction Block -AllowInboundRules False
  Set-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System ConsentPromptBehaviorAdmin 2
  Set-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System PromptOnSecureDesktop 1
  ```

### Fingerprint confirmation and kill switch (optional, recommended)
With it on, every button that does something (wake, apps, lock, who, restart, shut down, Claude, add PC, relay, remove) first answers with **🔐 Confirm with fingerprint**. The link opens a page on your own domain, your phone asks for your fingerprint (a passkey, WebAuthn) and only then the command runs. Someone who gets into your Bale/Telegram account still cannot do anything without your phone and your finger.

Needs a domain with HTTPS on the server (Caddy, nginx, ...) that forwards to `127.0.0.1:8791`:
```bash
wolbot auth https://auth.example.com      # turn it on (needs python3-cryptography, install.sh adds it)
systemctl restart wol-bot
wolbot passkey-add Phone                  # prints a one-time link: open it on your phone, touch the sensor
```
With Caddy: `auth.example.com { reverse_proxy 127.0.0.1:8791 }`.

- A confirm link works **once** and for **2 minutes**, only for that one action on that one PC.
- **Kill switch**: 3 failed checks in 10 minutes (wrong signature, reused link or sign-up code), or the **🛑 Kill switch** button (no fingerprint needed, cutting off is always safe): the bot blocks every action, and every online Windows PC locks its screen, stops the tunnel and SSH and turns its network cards off. Undo at the PC: `C:\ProgramData\wol-agent\wol-unkill.ps1` as administrator, then `wolbot unlock` on the server.
- **Fingerprint login**: even an admin must log in with a fingerprint before the bot shows anything; the login lasts 10 minutes. `wolbot auth login <minutes>` or `wolbot auth login off`.
- **Per-user permissions**: admins can do everything; other people get only the buttons and PCs you give them:
  ```bash
  wolbot user add <id> <name> wake,lock,who [PC1,PC2]   # perms: wake apps claude lock who restart shutdown vscode | all
  wolbot passkey-add <name> <id>                        # their own fingerprint
  wolbot user list | user del <id>
  ```
  The other buttons are hidden for them, and pressing an old one is refused. Pairing, relays, removing PCs and the kill switch stay admin-only.
- Anyone who is not an admin or user writing to the bot is reported to the admins.
- More people: `wolbot admin <id>` then `wolbot passkey-add <name> <id>`; each person confirms with their own phone. `wolbot passkey-list`, `wolbot passkey-del <name>`.
- Your phone may accept its screen-lock PIN instead of a finger (Android/iOS allow it), so use a long PIN, not a pattern. Passkeys in Google Password Manager / iCloud sync with that account: protect it with 2FA.

### Claude Code from your phone
Button **🤖 Claude Remote** starts Claude on the PC, in the folder chosen in WOL Agent (Claude Code CLI or the VS Code extension must be installed and signed in). Continue in the Claude app or at claude.ai/code. Two modes: **background** runs `claude remote-control` out of sight; **VS Code** opens a window with the Claude panel, and the same chat also shows at claude.ai/code. The first time, you trust the folder once at the PC. **⏹ Stop Claude** ends it.

### VS Code Remote-SSH (optional)
Only for opening the PC in VS Code from another laptop. Paste your laptop's `id_ed25519.pub` into the app (“Laptop key”), then the bot button **VS Code Remote-SSH** shows the `~/.ssh/config` block (ProxyJump through your server).

### Build the app
```
cd src/pc/gate
go test ./... && go build -trimpath -ldflags "-s -w" -o wol-gate.exe .
copy wol-gate.exe ..\app\
cd ..\app
go build -trimpath -ldflags "-H windowsgui -s -w" -o "WOL Agent.exe" .
```
WOL Agent embeds `wol-gate.exe`, so build the gate first.
No dependencies. Icon/manifest live in `rsrc_windows_amd64.syso` (`windres -O coff -F pe-x86-64 -i app.rc -o rsrc_windows_amd64.syso`).

### Security notes
- Tunnel user `woltun` has no shell and may only open its own port on `127.0.0.1`.
- Pair codes are one-time, live 15 minutes; 5 wrong codes block an IP for 10 minutes. Only public keys cross the pairing port.
- The bot answers only the admin ids in the config; with `wolbot auth` every action also needs the admin's fingerprint.
- On the PC, keep `sshd` on loopback only (`ListenAddress 127.0.0.1`, `PasswordAuthentication no` in `C:\ProgramData\ssh\sshd_config`): the tunnel still reaches it, nothing on your home network can.

---

<div dir="rtl">

## فارسی

### چه کار می‌کند؟
- **روشن کردن** کامپیوتر خاموش از توی گوشی: با اپ نصب‌شدنی (PWA)، یا اگر بخواهی با بات بله و تلگرام.
- **کنترل** بعد از روشن شدن: اجرای برنامه‌هایی که خودت انتخاب می‌کنی (تا ۱۲ تا)، قفل، ری‌استارت، خاموش کردن، دیدن کاربر واردشده.
- **Claude Remote**: روشن کردن Claude Code Remote Control پشت صحنه، یا در پنجره‌ی VS Code که پشت کامپیوتر هم دیده می‌شود.
- **VS Code از هر جا** با Remote-SSH.
- **روشن کردن از بیرون روی بیشتر مودم‌ها**: WOL Agent پورت را با UPnP باز می‌کند (UDP و TCP). آی‌پی عمومی نداری (CGNAT، TD-LTE، ۴G)؟ یک رله‌ی کوچک داخل خانه بسته را می‌فرستد.
- **بدون پورت باز برای کنترل**: کامپیوتر خودش با تونل SSH به سرور وصل می‌شود.
- **اثر انگشت روی هر دستور** (Passkey) و خود کامپیوتر هم امضا را چک می‌کند، پس سرور هک‌شده نمی‌تواند چیزی روی آن اجرا کند.
- **نگهبان**: هر تغییری که بتواند راهی به کامپیوتر باز کند، صفحه را قفل می‌کند و از تو می‌پرسد. اسم فایل و امضایش را هم می‌گوید تا نصب خودت را از حمله تشخیص بدهی. تغییرهای خطرناک تا حدود ۱۵ ثانیه برگردانده می‌شوند.
- **هشدار با صدا روی گوشی**، حتی با اپ بسته.
- **کیل‌سوییچ** با صفحه‌ی بازیابی: رمز پشت کامپیوتر به‌علاوه‌ی اثر انگشت روی گوشی همه‌چیز را برمی‌گرداند.

### سه تکه دارد
| تکه | کجا | کارش |
|---|---|---|
| `wolbot` | سرور لینوکسی | اپ گوشی، بات اختیاری، اتصال، روشن کردن، کنترل و هشدارها |
| `WOL Agent.exe` | هر کامپیوتر ویندوزی | یک‌بار اجرا می‌شود: کامپیوتر را وصل می‌کند، تونل و دروازه و نگهبان را نصب می‌کند و پورت مودم را باز می‌کند |
| `wol-gate.exe` | هر کامپیوتر ویندوزی | امضای اثر انگشت هر دستور را چک می‌کند؛ نگهبان و صفحه‌ی بازیابی را اجرا می‌کند |
| مودم / روتر | خانه | بسته‌ی روشن‌کردن را به کامپیوتر می‌رساند (UPnP یا فوروارد دستی، رله، یا OpenWrt با SSH) |

### راه‌اندازی
این بخش راه‌اندازی با بات است. برای استفاده فقط با اپ، بخش «اپ گوشی» را ببین.

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
2. در اپ یا بات بزن **➕ Add PC**، برنامه‌ی **WOL Agent.exe** را اجرا کن، کد را بچسبان، یک **رمز بازیابی** برای کیل‌سوییچ انتخاب کن (بار اول اجباری است) و «نصب و اتصال» را بزن.
3. ورود خودکار ویندوز با [Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon). صفحه بلافاصله قفل می‌شود.

**۳. مودم** — گزینه‌ی اول را WOL Agent خودش امتحان می‌کند و اگر نشد خبر می‌دهد:
- **آی‌پی عمومی و UPnP روی مودم (خودکار):** WOL Agent پورت UDP و TCP شماره ۹ را فوروارد می‌کند (یا 47009 اگر مودم پورت پایین را قبول نکند) و هر ساعت تازه‌اش می‌کند. کامپیوترهای جدید هم روشن کردن با UDP و هم TCP می‌گیرند؛ TCP برای شبکه‌هایی است که UDP را می‌بندند. وقتی کامپیوتر روشن است، سرور آی‌پی خانه را خودش دنبال می‌کند.
- **اینترنت TD-LTE / ۴G یا آی‌پی که مدام عوض می‌شود ← رله.** یک دستگاه همیشه‌روشن در همان خانه (گوشی اندرویدی قدیمی با [Termux](https://f-droid.org/packages/com.termux/)، رزبری‌پای یا یک کامپیوتر دیگر) بسته‌ی روشن‌کردن را از داخل خانه می‌فرستد. مودم تنظیم نمی‌خواهد. در بات روی کامپیوتر بزن **📡 Relay** و دستورهایی که می‌دهد را در Termux بزن. بعد در اعلان Termux «Acquire wakelock» را بزن و گوشی را به شارژ بزن.
- **آی‌پی عمومی بدون UPnP:** پورت **UDP 9** (و یا TCP 9) را به آی‌پی کامپیوتر فوروارد کن و آن آی‌پی را رزرو کن.
- **OpenWrt:** `wolbot add-wake <اسم> ssh <آدرس> <پورت> root br-lan`

> بعضی مودم‌ها چند دقیقه بعد از خاموش شدن کامپیوتر آدرسش را فراموش می‌کنند. اگر بعد از مدت طولانی روشن نشد، در مودم آی‌پی را به MAC ببند یا از رله استفاده کن. این مسیر به مودم بستگی دارد، پس یک بار تستش کن.

### اپ گوشی (PWA) - بدون بله و تلگرام
بعد از `wolbot auth https://auth.example.com` همان آدرس یک اپ نصب‌شدنی است: در Chrome یا Safari بازش کن، **Add to Home screen** بزن و با اثر انگشت وارد شو. همان دکمه‌های بات، وضعیت زنده هر ۵ ثانیه، اثر انگشت داخل خود اپ و فهرست رویدادها برای ادمین. اگر توکن بات خالی باشد، wolbot فقط به‌صورت اپ کار می‌کند.

**هشدار با صدا روی گوشی:** در اپ **🔕 هشدار خاموش** را بزن، اجازه بده و یک تست می‌رسد. از آن به بعد تغییرهای نگهبان، کیل‌سوییچ، درخواست باز کردن قفل، تأییدهای ناموفق، Passkey جدید و غریبه‌ها حتی با اپ بسته خبر داده می‌شوند. کارهای روزمره (ورود، اجرای برنامه‌ها) صدا ندارند. این Web Push است و محتوایش رمز شده، پس سرویس واسط آن را نمی‌بیند. روشن شدن هشدار روی دستگاه جدید هم خودش یک هشدار است.
- صدا همان صدای اعلان گوشی است: اپ وب نمی‌تواند صدای دیگری انتخاب کند یا از حالت بی‌صدا رد شود.
- هشدار ممکن است تا حدود ۳۰ ثانیه طول بکشد: سرور هر ۱۵ ثانیه کامپیوتر را چک می‌کند و کمی صبر می‌کند تا اسم فایل هم در پیام باشد. اگر اپ باز باشد، خودش هم بوق می‌زند.
- آیفون: اول **Add to Home Screen**، بعد اپ را از همان آیکون باز کن و زنگوله را بزن.
- سرور باید به سرویس‌های Push (گوگل، اپل، موزیلا، مایکروسافت) و گوشی به سرویس خودش دسترسی داشته باشد. اگر روشن کردن هشدار پشت VPN نشد، یک بار بدون آن امتحان کن.

**راه‌اندازی برای خودت**
1. سرور: `sudo bash install.sh --public-host <سرور>` (توکن بات لازم نیست)، یک دامنه با HTTPS به `127.0.0.1:8791`، بعد `wolbot auth https://auth.example.com && systemctl restart wol-bot`
2. گوشی: `wolbot admin 1` و `wolbot passkey-add Phone 1`، لینک را در گوشی باز کن و انگشت بگذار.
3. هر کامپیوتر: روی سرور `wolbot pair-code`، روی کامپیوتر **WOL Agent.exe** و چسباندن کد. دروازه‌ی کامپیوتر (پایین) نصب می‌شود و به Passkeyهای همان لحظه اعتماد می‌کند.
4. در گوشی `https://auth.example.com` را باز کن، **Add to Home screen** و ورود.

### دروازه‌ی کامپیوتر: خود کامپیوتر هر دستور را تأیید می‌کند
WOL Agent برنامه‌ی `wol-gate.exe` را نصب می‌کند و کلید سرور فقط می‌تواند همین را اجرا کند: نه shell، نه ترمینال، نه port forward. هر دستوری که چیزی را تغییر می‌دهد باید امضای تازه‌ی اثر انگشت روی **همان دستور، همان کامپیوتر، با زمان و عدد یک‌بارمصرف** داشته باشد و **خود کامپیوتر** آن را با Passkeyهایی که به آن‌ها اعتماد دارد چک می‌کند. سرور هک‌شده نمی‌تواند چیزی روی کامپیوتر اجرا کند. بدون امضا فقط این‌ها را می‌گیرد: روشن یا خاموش بودن، آی‌پی عمومی، اسم دکمه‌ها، روشن بودن Claude و هشدارهای نگهبان (چه چیزی عوض شده: اسم‌ها، مسیر فایل‌ها، امضاکننده‌ها)، و می‌تواند کیل‌سوییچ بزند یا یک تغییر را رد کند. پس در بدترین حالت می‌تواند کامپیوتر را قطع کند یا در اپ متن غلط نشان بدهد، ولی وارد نمی‌شود. پیام روی صفحه‌ی خود کامپیوتر از سرور رد نمی‌شود.
- هر درخواست در `C:\ProgramData\wol-agent\gate.log` ثبت و روی صفحه‌ی کامپیوتر نشان داده می‌شود؛ ۳ رد در ۱۰ دقیقه = کیل‌سوییچ به تصمیم خود کامپیوتر.
- **نگهبان** (تسک `WOL\Watch`، هر ۱۵ ثانیه): هر تغییر از جدول زیر صفحه را قفل می‌کند، روی کامپیوتر پیام می‌دهد، روی گوشی هشدار می‌فرستد و در اپ کارت می‌سازد. **تأیید با اثر انگشت** (خود کامپیوتر چک می‌کند) یا **رد** (کیل‌سوییچ می‌زند).

  | چه چیزی عوض شده | اگر جواب ندهی | خودش برمی‌گرداند؟ |
  |---|---|---|
  | ریموت دسکتاپ، UAC، فایروال | کیل‌سوییچ بعد از ۹۰ ثانیه | بله، تا حدود ۱۵ ثانیه به تنظیم تأییدشده |
  | ادمین‌ها، کاربرها، اجرای ابزار ریموت (AnyDesk، TeamViewer، RustDesk، Chrome Remote Desktop و...) | کیل‌سوییچ بعد از ۹۰ ثانیه | بله: عضو اضافه حذف، حساب جدید غیرفعال، ابزار بسته می‌شود |
  | کلیدهای SSH و تنظیمات sshd، Passkeyهای مورد اعتماد، ورود ناموفق به ویندوز | کیل‌سوییچ بعد از ۹۰ ثانیه | نه |
  | سرویس یا تسک جدید یا حذف‌شده | یک بار قفل می‌شود و بدون مهلت در اپ می‌ماند | نه؛ اگر خودت برگردانی هشدار بسته می‌شود |

- **می‌گوید چه چیزی عوض شده.** برای سرویس، تسک یا ابزار ریموت جدید، اسم، فایل، آرگومان‌ها، حساب اجراکننده و امضاکننده را می‌بینی، با یک نتیجه: **✅ شبیه نصب عادی** (امضای معتبر، داخل پوشه‌ی برنامه‌ها) یا **🚨 مشکوک** (بدون امضا یا امضای خراب، فایل پیدا نشد، بیرون از پوشه‌ی برنامه‌ها، یا برنامه‌ای که دستور اجرا می‌کند مثل PowerShell و `cmd` که امضای مایکروسافتش چیزی را ثابت نمی‌کند). مثلاً نصب Steam این‌طور نشان داده می‌شود: `new service "Steam Client Service", signed: Valve Corp.` این نتیجه فقط راهنماست، چون ابزار امضادار هم می‌تواند ناخواسته باشد. سؤال اصلی همیشه این است: «الان خودم این کار را کردم؟» اگر نه، رد کن.
- **تأیید فقط همان چیزی را می‌پذیرد که دیده‌ای.** هر چیزی که بعدش اضافه شود هشدار جدا می‌دهد و دوباره قفل می‌کند، پس چیزی پشت یک تأیید رد نمی‌شود.
- **رد** حتی برای یک تغییر کوچک کیل‌سوییچ کامل می‌زند و خود تغییر را پاک نمی‌کند: پشت کامپیوتر پاکش کن، وگرنه هشدار برمی‌گردد. برای چیزی که خودت نصب کرده‌ای، تأیید بزن.
- `wol-gate snapshot` چیزهایی را که زیر نظر دارد نشان می‌دهد و `wol-gate explain <services|tasks|remote> <اسم>` چیزی را که گزارش می‌کند. تاریخچه‌ی کامل در `gate.log` است.
- **بعد از کیل‌سوییچ**: یک صفحه‌ی قرمز تمام‌صفحه **رمز بازیابی** را می‌خواهد (جدا از ویندوز؛ در WOL Agent یا در ترمینال ادمین تعریفش کن: `C:\ProgramData\wol-agent\wol-gate.exe set-recovery`). رمز درست فقط اینترنت را برمی‌گرداند و درخواست باز شدن به اپ می‌فرستد؛ با اثر انگشت تأیید کن تا خود کامپیوتر امضا را چک کند و همه‌ی ارتباط‌ها و قفل سرور را برگرداند. ۵ رمز اشتباه = قفل ۱۵ دقیقه‌ای به بالا. بدون رمز یا با گوشی گم‌شده: `wol-unkill.ps1` با دسترسی ادمین و بعد `wolbot unlock`.
- فهرست: `wol-gate list`؛ گوشی جدید: ثبت روی سرور، بعد **WOL Agent ← Update**؛ حذف: `wol-gate untrust <اسم>`.
- هر کلید دیگری در `administrators_authorized_keys` راهی دور از دروازه است؛ فقط کلید دروازه را نگه دار.
- پیشنهاد (PowerShell ادمین): بستن همه‌ی اتصال‌های ورودی و روشن کردن UAC (دستورها در بخش انگلیسی).

### تأیید با اثر انگشت و کیل‌سوییچ (اختیاری، پیشنهادی)
وقتی روشن باشد، هر دکمه‌ای که کاری انجام می‌دهد (روشن، برنامه‌ها، قفل، Who، ری‌استارت، خاموش، Claude، Add PC، Relay، حذف) اول دکمه‌ی **🔐 Confirm with fingerprint** می‌دهد. لینک یک صفحه روی دامنه‌ی خودت باز می‌کند، گوشی اثر انگشتت را می‌خواهد (Passkey) و فقط بعد از آن دستور اجرا می‌شود. کسی که حساب بله‌ات را داشته باشد، بدون گوشی و انگشت تو کاری نمی‌تواند بکند.

یک دامنه با HTTPS روی سرور لازم است که به `127.0.0.1:8791` برسد:
```bash
wolbot auth https://auth.example.com
systemctl restart wol-bot
wolbot passkey-add Phone      # لینک یک‌بار مصرف را در گوشی باز کن و انگشت بگذار
```
در Caddy: `auth.example.com { reverse_proxy 127.0.0.1:8791 }`

- لینک تأیید **یک بار** و تا **۲ دقیقه** کار می‌کند و فقط برای همان دستور روی همان کامپیوتر است.
- **کیل‌سوییچ**: ۳ تأیید ناموفق در ۱۰ دقیقه یا زدن دکمه‌ی **🛑 Kill switch** (اثر انگشت نمی‌خواهد): بات همه‌چیز را می‌بندد و کامپیوترها قفل می‌شوند، تونل و SSH خاموش و کارت شبکه قطع می‌شود. برگشت: پشت کامپیوتر `C:\ProgramData\wol-agent\wol-unkill.ps1` را با دسترسی ادمین اجرا کن، بعد روی سرور `wolbot unlock`.
- **ورود با اثر انگشت**: حتی ادمین قبل از دیدن هر چیزی باید با اثر انگشت وارد شود؛ ورود ۱۰ دقیقه اعتبار دارد. تغییر: `wolbot auth login <دقیقه>` یا `wolbot auth login off`.
- **دسترسی جدا برای هر کاربر**: ادمین همه‌کاره است؛ بقیه فقط دکمه‌ها و کامپیوترهایی را می‌بینند که تو تعیین کنی:
  ```bash
  wolbot user add <id> <اسم> wake,lock,who [PC1,PC2]   # wake apps claude lock who restart shutdown vscode | all
  wolbot passkey-add <اسم> <id>
  wolbot user list | user del <id>
  ```
  Add PC، رله، حذف کامپیوتر و کیل‌سوییچ فقط برای ادمین است.
- هر غریبه‌ای که به بات پیام بدهد به ادمین‌ها گزارش می‌شود.
- نفر دیگر: `wolbot admin <id>` و بعد `wolbot passkey-add <اسم> <id>`؛ هر کس با گوشی خودش تأیید می‌کند. باطل کردن: `wolbot passkey-del <اسم>`.
- گوشی ممکن است به‌جای اثر انگشت PIN قفل صفحه را هم قبول کند؛ پس PIN بلند بگذار، نه الگو. Passkey با حساب گوگل/iCloud همگام می‌شود؛ روی آن حساب 2FA بگذار.

### Claude از توی گوشی
دکمه‌ی **🤖 Claude Remote**، Claude را روی کامپیوتر در پوشه‌ای که در WOL Agent انتخاب کرده‌ای روشن می‌کند (Claude Code یا افزونه‌ی VS Code آن باید نصب و وارد حساب شده باشد). بعد از اپ Claude یا سایت claude.ai/code ادامه می‌دهی. دو حالت دارد: **پشت صحنه** که `claude remote-control` را مخفی اجرا می‌کند، و **VS Code** که پنجره‌ای با پنل Claude باز می‌کند و همان گفتگو در claude.ai/code هم دیده می‌شود. بار اول پشت کامپیوتر یک بار به پوشه اعتماد می‌کنی. **⏹ Stop Claude** آن را می‌بندد.

### استفاده
در اپ یا بات: ⚡ Wake (روشن) · 🤖 Claude Remote · ▶️ برنامه‌های انتخابی (اجرا) · 🔒 Lock (قفل) · ⏻ Shut down (خاموش) · 📡 Relay · ➕ Add PC (کامپیوتر جدید) · 🛑 Kill switch (قطع همه‌چیز)

</div>
