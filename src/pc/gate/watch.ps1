# wol-gate watchdog snapshot: what matters for "who can get into this PC", as JSON lists.
# A category that fails to read is left out (the watchdog then skips it instead of raising an alarm).
$ErrorActionPreference = "Stop"
$o = [ordered]@{}
try { $o.admins = @(Get-LocalGroupMember -SID S-1-5-32-544 | ForEach-Object { "$($_.Name)" }) } catch {}
try { $o.users = @(Get-LocalUser | Where-Object Enabled | ForEach-Object { "$($_.Name)" }) } catch {}
# per-user services get a new random suffix at every logon: drop it
# Intel's Wi-Fi service comes and goes with its network card (every kill switch turned that into a false
# alarm). Skipped only when it is the real driver's file: something else with that name is still reported.
try {
  $flap = @(Get-CimInstance Win32_Service -Filter "Name='PIEServiceNew'" | Where-Object { $_.PathName -match '^"?[a-z]:\\windows\\system32\\driverstore\\filerepository\\[^\\]+\\intel_pie_service\.exe"?$' } | ForEach-Object { $_.Name })
  $o.services = @(Get-Service | Where-Object { $flap -notcontains $_.Name } | ForEach-Object { $_.Name -replace '_[0-9a-f]{4,}$', '' } | Sort-Object -Unique)
} catch {}
try { $o.tasks = @(Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' -and $_.TaskPath -ne '\WOL\' } | ForEach-Object { "$($_.TaskPath)$($_.TaskName)" }) } catch {}
try { $o.firewall = @(Get-NetFirewallProfile | ForEach-Object { "$($_.Name) $($_.Enabled) $($_.DefaultInboundAction) $($_.AllowInboundRules)" }) } catch {}
try {
  $p = Get-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System
  $o.uac = @("$($p.EnableLUA) $($p.ConsentPromptBehaviorAdmin) $($p.PromptOnSecureDesktop)")
} catch {}
try { $o.rdp = @("$((Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections)") } catch {}
try {
  $o.keys = @(foreach ($f in "$env:ProgramData\ssh\administrators_authorized_keys", "$env:ProgramData\ssh\sshd_config") {
    if (Test-Path $f) { "$f " + (Get-FileHash $f).Hash } })
} catch {}
try {
  $o.remote = @(Get-Process | Where-Object { $_.Name -match '^(anydesk|teamviewer|tv_w32|tv_x64|rustdesk|remoting_host|winvnc|tvnserver|vncserver|parsec|splashtop|srservice|ammyy|aa_v3|supremo|screenconnect|zohoassist|ultraviewer|getscreen|dwagent|meshagent|radmin|rserver3)' } |
    ForEach-Object { $_.Name } | Sort-Object -Unique)
} catch {}
try {
  $o.failed = @(Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4625; StartTime = (Get-Date).AddMinutes(-10) } -MaxEvents 20 -ErrorAction SilentlyContinue |
    ForEach-Object { "$($_.RecordId)" })
} catch {}
$o | ConvertTo-Json -Compress -Depth 3
