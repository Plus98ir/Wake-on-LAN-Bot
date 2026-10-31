<#
  wolbot PC agent setup (Windows 10/11). Normally run by "WOL Agent.exe"; also works by hand in an elevated PowerShell:
    setup-pc.ps1 -PairCode 1.2.3.4:8790/ABCD-2345            (code from the bot: Add PC)
    setup-pc.ps1 -Server 1.2.3.4 -TunnelPort 2230 -BotKey "ssh-ed25519 AAAA..."   (manual, then wolbot add-pc)
  Options:
    -Name PC  -WakeHost <modem DDNS/IP for UDP wake>  -ExtraKey "<laptop pubkey for VS Code Remote-SSH>"
    -V2rayN <path>  -VSCode <path>  -NoLockAtLogon  -Detect (print system info as JSON)  -Uninstall
    -ClaudeDir <folder>  (where the bot's "Claude Remote" button starts Claude Code; default Documents)
#>
param(
  [string]$PairCode,
  [string]$Server,
  [int]$ServerPort = 22,
  [string]$TunnelUser = "woltun",
  [int]$TunnelPort = 2230,
  [string]$BotKey,
  [string]$Name = $env:COMPUTERNAME,
  [string]$WakeHost,
  [string[]]$ExtraKey = @(),
  [string]$V2rayN,
  [string]$VSCode,
  [string]$ClaudeDir,
  [switch]$NoLockAtLogon,
  [switch]$Detect,
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Agent = "C:\ProgramData\wol-agent"
$Key = "$Agent\tunnel_ed25519"
$AdminKeys = "C:\ProgramData\ssh\administrators_authorized_keys"
$User = "$env:USERDOMAIN\$env:USERNAME"

function Find-Nic {
  $n = Get-NetAdapter -Physical | Where-Object { $_.Status -eq "Up" -and $_.MediaType -eq "802.3" } | Select-Object -First 1
  if (-not $n) { $n = Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq "802.3" } | Select-Object -First 1 }
  $n
}
function Find-V2rayN {
  foreach ($root in (Get-PSDrive -PSProvider FileSystem).Root) {
    $f = Get-ChildItem -Path $root -Filter v2rayN.exe -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if ($f) { return $f }
  }
}
function Find-VSCode {
  @("$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe", "$env:ProgramFiles\Microsoft VS Code\Code.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
# Same lookup is copied into wol-claude.ps1, so a VS Code extension update (new folder) is still found.
$FindClaude = @'
function Find-Claude {
  $c = Get-Command claude.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
  if (-not $c -and (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) { $c = "$env:USERPROFILE\.local\bin\claude.exe" }
  if (-not $c) {
    $c = Get-ChildItem "$env:USERPROFILE\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
  }
  $c
}
'@
. ([scriptblock]::Create($FindClaude))
# Claude Code asks once per folder whether to trust it; remote-control refuses untrusted folders.
function Test-ClaudeTrust($dir) {
  try { $j = Get-Content "$env:USERPROFILE\.claude.json" -Raw | ConvertFrom-Json } catch { return $false }
  if (-not $j.projects) { return $false }
  $d = ($dir -replace '\\', '/').TrimEnd('/').ToLower()
  foreach ($p in $j.projects.PSObject.Properties) {
    $k = ($p.Name -replace '\\', '/').TrimEnd('/').ToLower()
    if ($p.Value.hasTrustDialogAccepted -and ($d -eq $k -or $d.StartsWith("$k/"))) { return $true }
  }
  $false
}

$conf = if (Test-Path "$Agent\agent.json") { Get-Content "$Agent\agent.json" -Raw | ConvertFrom-Json } else { $null }
if (-not $ClaudeDir) { $ClaudeDir = if ($conf -and $conf.claude_dir) { $conf.claude_dir } else { [Environment]::GetFolderPath("MyDocuments") } }

if ($Detect) {
  $nic = Find-Nic
  $tunnel = Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -match "wol-agent" }
  [ordered]@{
    name    = $env:COMPUTERNAME
    user    = $env:USERNAME
    os      = (Get-CimInstance Win32_OperatingSystem).Caption
    board   = ((Get-CimInstance Win32_BaseBoard) | ForEach-Object { "$($_.Manufacturer) $($_.Product)" })
    nic     = if ($nic) { $nic.InterfaceDescription } else { "" }
    mac     = if ($nic) { $nic.MacAddress -replace '-', ':' } else { "" }
    link    = if ($nic) { "$($nic.Status) $($nic.LinkSpeed)" } else { "" }
    wol     = if ($nic) { "$((Get-NetAdapterPowerManagement -Name $nic.Name).WakeOnMagicPacket)" } else { "" }
    v2rayn  = "$(if ($V2rayN) { $V2rayN } else { Find-V2rayN })"
    vscode  = "$(Find-VSCode)"
    claude  = "$(Find-Claude)"
    claudeDir = $ClaudeDir
    sshd    = "$((Get-Service sshd -ErrorAction SilentlyContinue).Status)"
    paired  = [bool]$conf
    server  = if ($conf) { "$($conf.server)" } else { "" }
    port    = if ($conf) { $conf.tunnel_port } else { 0 }
    tunnel  = [bool]$tunnel
  } | ConvertTo-Json -Compress
  exit 0
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Run this as Administrator." -ForegroundColor Red; exit 1
}

function Lock-Acl($path) {
  # OpenSSH refuses keys that other users can read; the owner must be SYSTEM too, or the
  # SYSTEM tunnel task rejects its own key. ssh-keygen adds an ACE for the current user - drop it.
  icacls $path /inheritance:r /remove:g "$env:USERDOMAIN\$env:USERNAME" /grant:r "*S-1-5-18:F" /grant:r "*S-1-5-32-544:F" | Out-Null
  icacls $path /setowner "*S-1-5-18" | Out-Null
}

if ($Uninstall) {
  Get-ScheduledTask -TaskPath "\WOL\" -ErrorAction SilentlyContinue | ForEach-Object { Stop-ScheduledTask -InputObject $_ -ErrorAction SilentlyContinue; $_ } | Unregister-ScheduledTask -Confirm:$false
  Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -match "wol-agent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Remove-Item $Agent -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "Removed WOL tasks and $Agent. sshd and authorized keys were left as they are."
  Write-Host "Remove the PC in the bot too (or on the server: wolbot del <name>)."
  exit 0
}

Write-Host "[1/7] Network card"
$nic = Find-Nic
if ($nic) { $Mac = $nic.MacAddress -replace '-', ':'; Write-Host "  $($nic.InterfaceDescription)  $Mac" }
else { Write-Host "  No wired network card found - Wake-on-LAN needs a cable." -ForegroundColor Red; exit 1 }

Write-Host "[2/7] Tunnel key"
New-Item -ItemType Directory -Path $Agent -Force | Out-Null
if (-not (Test-Path $Key)) { & ssh-keygen.exe -q -t ed25519 -f $Key -N '""' -C "wol-$Name" }
Lock-Acl $Key
$pub = ((Get-Content "$Key.pub" -Raw).Trim() -split '\s+')[0..1] -join ' '

if ($PairCode) {
  Write-Host "[3/7] Pairing with the server"
  $c = $PairCode.Trim() -replace '^wol://', ''
  if ($c -notmatch '^([^/\s]+)/([A-Za-z0-9-]+)$') { Write-Host "  Bad code. It looks like: 1.2.3.4:8790/ABCD-2345" -ForegroundColor Red; exit 1 }
  $hostPort = $Matches[1]; $code = $Matches[2]
  $body = @{ code = $code; name = $Name; mac = $Mac; os = "windows"; user = $env:USERNAME; tunnel_key = $pub; wake_host = $WakeHost } | ConvertTo-Json
  try {
    $r = Invoke-RestMethod -Uri "http://$hostPort/pair" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 20
  } catch {
    $msg = $_.ErrorDetails.Message; if (-not $msg) { $msg = $_.Exception.Message }
    Write-Host "  Pairing failed: $msg" -ForegroundColor Red; exit 1
  }
  $Server = $r.server; $ServerPort = $r.ssh_port; $TunnelUser = $r.tunnel_user; $TunnelPort = $r.tunnel_port; $BotKey = $r.bot_key; $Name = $r.name
  Write-Host "  Paired as '$Name' (tunnel port $TunnelPort on $Server)"
} else {
  Write-Host "[3/7] Manual mode (no pair code)"
  if (-not $Server -or -not $BotKey) { Write-Host "  Need -PairCode, or -Server and -BotKey" -ForegroundColor Red; exit 1 }
}
@{ server = $Server; ssh_port = $ServerPort; tunnel_user = $TunnelUser; tunnel_port = $TunnelPort; name = $Name; claude_dir = $ClaudeDir } | ConvertTo-Json | Set-Content "$Agent\agent.json" -Encoding utf8

Write-Host "[4/7] OpenSSH Server + bot key"
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  Write-Host "  installing OpenSSH Server (needs internet, a few minutes)..."
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
if (-not (Test-Path $AdminKeys)) { New-Item -ItemType File -Path $AdminKeys -Force | Out-Null }
$have = Get-Content $AdminKeys -ErrorAction SilentlyContinue
foreach ($k in @($BotKey) + $ExtraKey) {
  if (-not $k) { continue }
  $kb = ($k -split '\s+')[0..1] -join ' '
  if (-not ($have | Where-Object { $_ -like "*$kb*" })) { Add-Content -Path $AdminKeys -Value $k -Encoding ascii }
}
Lock-Acl $AdminKeys

Write-Host "[5/7] Scheduled tasks"
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"
@"
# Keeps the reverse tunnel to the wolbot server up (started by task WOL\Tunnel)
while (`$true) {
  & '$ssh' -N -T -i '$Key' -p $ServerPort ``
    -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 ``
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='$Agent\known_hosts' ``
    -E '$Agent\tunnel.log' -R 127.0.0.1:${TunnelPort}:127.0.0.1:22 $TunnelUser@$Server
  # keep the log small
  if ((Get-Item '$Agent\tunnel.log' -ErrorAction SilentlyContinue).Length -gt 1MB) { Remove-Item '$Agent\tunnel.log' }
  Start-Sleep -Seconds 10
}
"@ | Set-Content -Path "$Agent\wol-tunnel.ps1" -Encoding utf8
Lock-Acl "$Agent\wol-tunnel.ps1"

$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$sys = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent\wol-tunnel.ps1`""
Register-ScheduledTask -TaskPath "\WOL\" -TaskName "Tunnel" -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $sys -Settings $set -Force | Out-Null
Write-Host "  Tunnel (at startup)"

# Interactive tasks: run on the logged-on desktop when the bot calls schtasks /run
$me = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
function Add-AppTask($task, $exe, $arg = $null) {
  if (-not $exe -or -not (Test-Path $exe)) { Write-Host "  skip $task (not found)" -ForegroundColor Yellow; return }
  $a = if ($arg) { New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory (Split-Path $exe) } else { New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe) }
  Register-ScheduledTask -TaskPath "\WOL\" -TaskName $task -Action $a -Principal $me -Settings $set -Force | Out-Null
  Write-Host "  $task -> $exe"
}
if (-not $V2rayN) { $V2rayN = Find-V2rayN }
if (-not $VSCode) { $VSCode = Find-VSCode }
Add-AppTask "v2rayN" $V2rayN
Add-AppTask "VSCode" $VSCode
Add-AppTask "Lock" "$env:WINDIR\System32\rundll32.exe" "user32.dll,LockWorkStation"

# Claude Code Remote Control: the bot starts it, then you continue from the Claude app / claude.ai/code
@"
# Starts Claude Code Remote Control in '$ClaudeDir' (task WOL\Claude). -Status prints its log, -Stop ends it.
param([switch]`$Status, [switch]`$Stop)
`$log = '$Agent\claude.log'
`$procs = @(Get-CimInstance Win32_Process -Filter "Name='claude.exe'" | Where-Object { `$_.CommandLine -match 'remote-control' })
if (`$Stop) { `$procs | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }; "stopped (`$(`$procs.Count) process)"; exit 0 }
if (`$Status) {
  if (`$procs.Count) { 'running' } else { 'not running' }
  foreach (`$f in `$log, "`$log.err") { if (Test-Path `$f) { Get-Content `$f -Tail 25 } }
  exit 0
}
if (`$procs.Count) { exit 0 }
$FindClaude
`$exe = Find-Claude
if (-not `$exe) { 'error: Claude Code not found on this PC (install the VS Code extension or the CLI)' | Set-Content `$log; exit 1 }
Remove-Item `$log, "`$log.err" -ErrorAction SilentlyContinue
Start-Process -FilePath `$exe -ArgumentList 'remote-control', '--name', '$Name' -WorkingDirectory '$ClaudeDir' ``
  -RedirectStandardOutput `$log -RedirectStandardError "`$log.err" -WindowStyle Hidden
"@ | Set-Content -Path "$Agent\wol-claude.ps1" -Encoding utf8
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent\wol-claude.ps1`""
Register-ScheduledTask -TaskPath "\WOL\" -TaskName "Claude" -Action $act -Principal $me -Settings $set -Force | Out-Null
$claude = Find-Claude
if (-not $claude) {
  Write-Host "  Claude task added, but Claude Code is not installed yet" -ForegroundColor Yellow
} else {
  if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir -Force | Out-Null }
  Write-Host "  Claude -> remote-control in $ClaudeDir"
  if (-not (Test-ClaudeTrust $ClaudeDir)) {
    # one time: Claude asks "trust this folder?" - the bot cannot answer that, so ask now
    $cmd = "Set-Location -LiteralPath '$ClaudeDir'; Write-Host 'WOL Agent: press Enter to trust this folder, then type /exit and close this window.' -ForegroundColor Cyan; & '$claude'"
    Start-Process powershell.exe -ArgumentList "-NoProfile", "-EncodedCommand", ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd)))
    Write-Host "  A Claude window opened: press Enter there to trust the folder (once), then /exit" -ForegroundColor Cyan
  }
}
if (-not $NoLockAtLogon) {
  $lock = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\rundll32.exe" -Argument "user32.dll,LockWorkStation"
  Register-ScheduledTask -TaskPath "\WOL\" -TaskName "LockAtLogon" -Action $lock -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $User) -Principal $me -Settings $set -Force | Out-Null
  Write-Host "  LockAtLogon (screen locks right after auto-logon)"
} else {
  Unregister-ScheduledTask -TaskPath "\WOL\" -TaskName "LockAtLogon" -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host "[6/7] Wake-on-LAN on the network card"
foreach ($kw in @{ "*WakeOnMagicPacket" = "1"; "S5WakeOnLan" = "1"; "EnableGreenEthernet" = "0"; "*EEE" = "0" }.GetEnumerator()) {
  # exact match: -RegistryKeyword treats "*" as a wildcard
  $p = Get-NetAdapterAdvancedProperty -Name $nic.Name -AllProperties | Where-Object { $_.RegistryKeyword -eq $kw.Key }
  if ($p -and "$($p.RegistryValue)" -ne $kw.Value) {
    Set-NetAdapterAdvancedProperty -Name $nic.Name -RegistryKeyword $kw.Key -RegistryValue $kw.Value -NoRestart
    Write-Host "  $($p.DisplayName) -> $($kw.Value) (applies after reboot)"
  }
}
Set-NetAdapterPowerManagement -Name $nic.Name -WakeOnMagicPacket Enabled -ErrorAction SilentlyContinue
# Fast Startup leaves the NIC unarmed on many boards
powercfg /hibernate off | Out-Null
Write-Host "  done"

Write-Host "[7/7] Start tunnel"
Stop-ScheduledTask -TaskPath "\WOL\" -TaskName "Tunnel" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -match "wol-agent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskPath "\WOL\" -TaskName "Tunnel"
$up = $false
for ($i = 0; $i -lt 12 -and -not $up; $i++) {
  Start-Sleep -Seconds 2
  $up = [bool](Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -match "wol-agent" })
}
if ($up) { Write-Host "  tunnel is up" -ForegroundColor Green }
else { Write-Host "  tunnel not up yet - it retries every 10 s (log: $Agent\tunnel.log)" -ForegroundColor Yellow }

Write-Host ""
if ($PairCode) {
  Write-Host "Done. '$Name' is now in the bot." -ForegroundColor Green
} else {
  Write-Host "Done. On the server run:" -ForegroundColor Green
  Write-Host "  wolbot add-pc $Name $Mac $TunnelPort `"$pub`" $env:USERNAME windows"
}
Write-Host "Still to do by hand: BIOS (Power On By PCI-E = Enabled, ErP = Disabled) and auto-logon (Sysinternals Autologon)."
