<p align="center"><img src="Wake-on-LAN-Bot.png" alt="Wake-on-LAN Bot"></p>

<h1 align="center">Wake-on-LAN Bot</h1>

<p align="center">Turn your PC on from your phone and start its apps — through a Bale or Telegram bot.<br>
<a href="#فارسی">فارسی</a> · <a href="https://plus98ir.dpdns.org/Wake-on-LAN-Bot/">Website</a> · <a href="../../releases/latest">Download</a></p>

---

## English

### What it does
- **Wake** a shut-down PC from a bot on your phone (Bale or Telegram).
- **Control** it once it is on: start the apps you pick (up to 12), lock, restart, shut down, see who is logged on.
- **Claude Remote**: start Claude Code Remote Control in the background or in a VS Code terminal you can see at the PC.
- **VS Code from anywhere** with Remote-SSH, no open ports at home.
- Works behind **any modem / NAT**: the PC connects out to your server, nothing is exposed at home.
- **Fingerprint confirmation**: every button that does something asks for your fingerprint (passkey) first, plus a **kill switch**.

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
- **No public IP (TD-LTE, 4G, CGNAT) or a changing IP → relay.** Any always-on device in the same home network (old Android phone with [Termux](https://f-droid.org/packages/com.termux/), Raspberry Pi, another PC) runs `relay.py`. It asks the server for wake jobs over outgoing HTTP and sends the packet inside the home. No modem setup. In the bot: PC → **📡 Relay**, then on the phone:
  ```
  pkg install python
  curl -O http://<server>:8790/relay.py
  python relay.py <server>:8790/<CODE>
  ```
  In Termux tap **Acquire wakelock** in its notification and keep the phone charging.
- **Public IP:** forward **UDP 9** to the PC's IP and reserve that IP in DHCP. The bot follows your home IP automatically while the PC is on (or set a DDNS name in the app).
- **OpenWrt / Linux box at home:** `wolbot add-wake <PC> ssh <host> <port> root br-lan` (needs `etherwake`).

> Many cheap modems forget the PC's address a few minutes after it shuts down and then drop the UDP packet. If waking fails only after a long time off, use a relay.

### Server commands
```
wolbot list | wake <name> | status <name> | pair-code | del <name>
wolbot add-wake <name> udp <host> [port]
wolbot add-wake <name> ssh <host> <port> <user> [iface]
wolbot relay-code <name>
wolbot auth <https://auth.example.com> [port] | auth off
wolbot passkey-add <name> [user id] | passkey-list | passkey-del <name>
wolbot auth login <minutes> | login off
wolbot user add <id> <name> <perm,perm|all> [machine,...] | user list | user del <id>
wolbot kill | unlock
```
Config: `/etc/wol-bot/config.json`. Service: `systemctl status wol-bot`. Uninstall: `bash install.sh --uninstall`.

### Phone app (PWA) - no Bale / Telegram needed
After `wolbot auth https://auth.example.com`, that address is also an installable phone app: open it in Chrome / Safari, **Add to Home screen**, log in with your fingerprint. Same buttons as the bot, live status every 5 s, the fingerprint prompt right inside the app, admins see the events log. Without a bot token (`token` empty in the config) wolbot runs as the app only. Login = passkey (no password), session cookie `HttpOnly; Secure; SameSite=Strict`, every request needs the app's own header and origin.

**Quick start for your own setup**
1. Server: `sudo bash install.sh --public-host <server>` (no bot token needed), then a domain with HTTPS to `127.0.0.1:8791` and `wolbot auth https://auth.example.com && systemctl restart wol-bot`.
2. Your phone: `wolbot admin <any id you choose, e.g. 1>`, `wolbot passkey-add Phone 1`, open the link on the phone, touch the sensor.
3. Each PC: `wolbot pair-code` on the server, run **WOL Agent.exe** on the PC, paste the code. The PC gate (below) is installed and trusts the passkeys registered at that moment.
4. Open `https://auth.example.com` on the phone, **Add to Home screen**, log in.

### PC gate - the PC approves every action itself
WOL Agent installs `wol-gate.exe` and forces the server's key to it (`command=` in `administrators_authorized_keys`): no shell, no terminal, no port forwarding. Every action that changes something (lock, who, restart, shut down, apps, Claude) must carry a fresh passkey signature over exactly that action, on exactly that PC, with a time and a one-time nonce, and the **PC checks it against the passkeys it trusts**. A hacked server cannot run anything; without a signature it only gets online/offline, button names, Claude running yes/no, and the kill switch.
- Every request is logged in `C:\ProgramData\wol-agent\gate.log` and shown on the PC screen; 3 refused requests in 10 minutes and the PC itself starts the kill switch.
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
Bot button **🤖 Claude Remote** starts `claude remote-control` on the PC (in the folder chosen in the app; Claude Code CLI or the VS Code extension must be installed and signed in). Continue in the Claude app or at claude.ai/code. The first install opens Claude once so you can trust that folder. **⏹ Stop Claude** ends it.

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
- **روشن کردن** کامپیوتر خاموش از توی گوشی، با بات بله یا تلگرام.
- **کنترل** بعد از روشن شدن: اجرای برنامه‌هایی که خودت انتخاب می‌کنی (تا ۱۲ تا)، قفل، ری‌استارت، خاموش کردن، دیدن کاربر واردشده.
- **Claude Remote**: روشن کردن Claude Code Remote Control پشت صحنه یا داخل ترمینال VS Code (پشت کامپیوتر هم دیده می‌شود).
- **VS Code از هر جا** با Remote-SSH، بدون باز کردن پورت در خانه.
- پشت **هر مودمی** کار می‌کند: کامپیوتر خودش به سرور وصل می‌شود.
- **تأیید با اثر انگشت**: هر دکمه‌ای که کاری انجام می‌دهد اول اثر انگشتت را می‌خواهد، به‌علاوه‌ی **کیل‌سوییچ**.

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

**۳. مودم — یکی از این‌ها:**
- **اینترنت TD-LTE / ۴G یا آی‌پی که مدام عوض می‌شود ← رله.** یک دستگاه همیشه‌روشن در همان خانه (گوشی اندرویدی قدیمی با [Termux](https://f-droid.org/packages/com.termux/)، رزبری‌پای یا یک کامپیوتر دیگر) بسته‌ی روشن‌کردن را از داخل خانه می‌فرستد. مودم تنظیم نمی‌خواهد. در بات روی کامپیوتر بزن **📡 Relay** و دستورهایی که می‌دهد را در Termux بزن. بعد در اعلان Termux «Acquire wakelock» را بزن و گوشی را به شارژ بزن.
- **آی‌پی عمومی:** پورت **UDP 9** را به آی‌پی کامپیوتر فوروارد کن و آن آی‌پی را رزرو کن. اگر آی‌پی عوض شود، بات خودش (وقتی کامپیوتر روشن است) به‌روزش می‌کند.
- **OpenWrt:** `wolbot add-wake <اسم> ssh <آدرس> <پورت> root br-lan`

> بعضی مودم‌ها چند دقیقه بعد از خاموش شدن کامپیوتر آدرسش را فراموش می‌کنند. اگر بعد از مدت طولانی روشن نشد، از رله استفاده کن.

### اپ گوشی (PWA) - بدون بله و تلگرام
بعد از `wolbot auth https://auth.example.com` همان آدرس یک اپ نصب‌شدنی است: در Chrome یا Safari بازش کن، **Add to Home screen** بزن و با اثر انگشت وارد شو. همان دکمه‌های بات، وضعیت زنده هر ۵ ثانیه، اثر انگشت داخل خود اپ و فهرست رویدادها برای ادمین. اگر توکن بات خالی باشد، wolbot فقط به‌صورت اپ کار می‌کند.

**راه‌اندازی برای خودت**
1. سرور: `sudo bash install.sh --public-host <سرور>` (توکن بات لازم نیست)، یک دامنه با HTTPS به `127.0.0.1:8791`، بعد `wolbot auth https://auth.example.com && systemctl restart wol-bot`
2. گوشی: `wolbot admin 1` و `wolbot passkey-add Phone 1`، لینک را در گوشی باز کن و انگشت بگذار.
3. هر کامپیوتر: روی سرور `wolbot pair-code`، روی کامپیوتر **WOL Agent.exe** و چسباندن کد. دروازه‌ی کامپیوتر (پایین) نصب می‌شود و به Passkeyهای همان لحظه اعتماد می‌کند.
4. در گوشی `https://auth.example.com` را باز کن، **Add to Home screen** و ورود.

### دروازه‌ی کامپیوتر: خود کامپیوتر هر دستور را تأیید می‌کند
WOL Agent برنامه‌ی `wol-gate.exe` را نصب می‌کند و کلید سرور فقط می‌تواند همین را اجرا کند: نه shell، نه ترمینال، نه port forward. هر دستوری که چیزی را تغییر می‌دهد باید امضای تازه‌ی اثر انگشت روی **همان دستور، همان کامپیوتر، با زمان و عدد یک‌بارمصرف** داشته باشد و **خود کامپیوتر** آن را با Passkeyهایی که به آن‌ها اعتماد دارد چک می‌کند. سرور هک‌شده هیچ کاری نمی‌تواند بکند.
- هر درخواست در `C:\ProgramData\wol-agent\gate.log` ثبت و روی صفحه‌ی کامپیوتر نشان داده می‌شود؛ ۳ رد در ۱۰ دقیقه = کیل‌سوییچ به تصمیم خود کامپیوتر.
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
دکمه‌ی **🤖 Claude Remote** در بات، Claude Code را روی کامپیوتر روشن می‌کند. بعد از اپ Claude یا سایت claude.ai/code ادامه می‌دهی (Claude Code یا افزونه‌ی VS Code آن باید نصب و وارد حساب شده باشد). بار اول برنامه یک پنجره‌ی Claude باز می‌کند: Enter بزن تا به پوشه اعتماد کند.

### استفاده
در بات: ⚡ Wake (روشن) · 🤖 Claude Remote · ▶️ برنامه‌های انتخابی (اجرا) · 🔒 Lock (قفل) · ⏻ Shut down (خاموش) · 📡 Relay · ➕ Add PC (کامپیوتر جدید) · 🛑 Kill switch (قطع همه‌چیز)

</div>
