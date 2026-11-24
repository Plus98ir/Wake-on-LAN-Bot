# 🆘 بعد از کیل‌سوییچ: برگرداندن سریع

وقتی کیل‌سوییچ می‌زند، کارت شبکه، SSH و تونل کامپیوتر خاموش می‌شوند و یک صفحه‌ی قرمز تمام‌صفحه رمز بازیابی را می‌خواهد. در این حالت کامپیوتر در اپ و `wolbot status` **آفلاین** دیده می‌شود، حتی اگر روشن باشد.

اگر پشت کامپیوتر هستی، بدون رمز بازیابی هم می‌شود همه‌چیز را برگرداند.

## ۱. پشت کامپیوتر: PowerShell ادمین

<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Del</kbd> ← **Task Manager** ← **Run new task** ← بنویس `powershell` ← تیک **Create this task with administrative privileges** ← OK

این خط‌ها را یکی‌یکی بزن. دقت کن اسم فایل `wol-unkill.ps1` است، نه `wol-kill.ps1`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
& C:\ProgramData\wol-agent\wol-unkill.ps1
schtasks /end /tn "WOL\RescueUI"; schtasks /end /tn "WOL\Rescue"
schtasks /change /tn "WOL\RescueUI" /disable; schtasks /change /tn "WOL\Rescue" /disable
```

خط دوم اینترنت، SSH و تونل را برمی‌گرداند. خط‌های سوم و چهارم صفحه‌ی قرمز را می‌بندند. با کیل‌سوییچ بعدی، این دو تسک خودشان دوباره روشن می‌شوند.

## ۲. روی سرور

```
wolbot unlock
wolbot list
```

`wolbot list` اسم کامپیوترها و وضعیتشان را نشان می‌دهد. باید 🟢 online ببینی (برای یک کامپیوتر: `wolbot status <اسم>`).

## ۳. رمز بازیابی را یادت رفته؟

رمز فقط به شکل هش در `recovery.json` ذخیره می‌شود، پس پیدا کردنش ممکن نیست؛ یک رمز تازه بگذار (رمز قبلی لازم نیست). در PowerShell ادمین:

```powershell
$p = Read-Host "New recovery password"
$p | & C:\ProgramData\wol-agent\wol-gate.exe set-recovery --stdin
Remove-Variable p; Clear-Host
```

باید `recovery password saved` را ببینی. حداقل ۸ حرف. از صفحه‌ای که رمز رویش است عکس نگیر.

اگر `wol-gate.exe set-recovery` (بدون `--stdin`) پنجره‌ای باز نکرد، صفحه‌ی قرمز هنوز پورت `127.0.0.1:47811` را گرفته است. اول قدم ۱ را انجام بده یا از روش بالا استفاده کن.

## ۴. چرا کیل‌سوییچ زد؟

```powershell
Get-Content C:\ProgramData\wol-agent\kill.log -Tail 5
Get-Content C:\ProgramData\wol-agent\gate.log -Tail 30
```

رایج‌ترین دلیل: یک تغییر خطرناک (اجرای **AnyDesk / TeamViewer / RustDesk**، تغییر فایروال، UAC، ریموت دسکتاپ، ادمین‌ها) که ظرف **۹۰ ثانیه** با اثر انگشت تأیید نشده است. دلیل‌های دیگر: ۳ تأیید ناموفق در ۱۰ دقیقه، یا زدن دکمه‌ی 🛑.

**دفعه‌ی بعد:** قبل از نصب یا اجرای ابزار ریموت یا تغییر تنظیمات امنیتی، اپ WOL را روی گوشی باز کن، کارت هشدار را بخوان و با اثر انگشت **تأیید** کن.
