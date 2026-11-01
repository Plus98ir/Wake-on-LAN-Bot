<#
  wolbot PC agent setup (Windows 10/11). Normally run by "WOL Agent.exe"; also works by hand in an elevated PowerShell:
    setup-pc.ps1 -PairCode 1.2.3.4:8790/ABCD-2345            (code from the bot: Add PC)
    setup-pc.ps1 -Server 1.2.3.4 -TunnelPort 2230 -BotKey "ssh-ed25519 AAAA..."   (manual, then wolbot add-pc)
  Options:
    -Name PC  -WakeHost <modem DDNS/IP for UDP wake>  -ExtraKey "<laptop pubkey for VS Code Remote-SSH>"
    -V2rayN <path>  -VSCode <path>  -NoLockAtLogon  -Detect (print system info as JSON)  -Uninstall
    -ClaudeDir <folder>  (where the bot's "Claude Remote" button starts Claude Code; default Documents)
    -ClaudeMode hidden|vscode  (run Claude Remote in the background, or in the VS Code Claude panel)
    -AppsFile <json>  ([{name, path, args}] = the bot's app buttons; default v2rayN + VS Code)
    -Update  (already paired: only save apps + Claude settings)
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
  [string]$ClaudeMode,
  [string]$AppsFile,
  [switch]$NoLockAtLogon,
  [switch]$Update,
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
  # slow (searches every drive), so only once per run
  if ($null -ne $script:v2) { return $script:v2 }
  $script:v2 = ""
  foreach ($root in (Get-PSDrive -PSProvider FileSystem).Root) {
    $f = Get-ChildItem -Path $root -Filter v2rayN.exe -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if ($f) { $script:v2 = $f; break }
  }
  $script:v2
}
function Find-VSCode {
  @("$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe", "$env:ProgramFiles\Microsoft VS Code\Code.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
# Programs in the Start menu (shortcuts to an .exe), for the app picker in WOL Agent
function Find-StartApps {
  $sh = New-Object -ComObject WScript.Shell
  $seen = @{}
  $list = @(foreach ($dir in "$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs") {
    Get-ChildItem $dir -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_.BaseName -match 'uninstall|readme|help|manual|documentation|website|release notes|license') { return }
      $l = $sh.CreateShortcut($_.FullName)
      if ($l.TargetPath -notmatch '\.exe$' -or $l.TargetPath -like "$env:WINDIR\*" -or -not (Test-Path $l.TargetPath)) { return }
      $k = "$($l.TargetPath)|$($l.Arguments)".ToLower()
      if ($seen[$k]) { return }
      $seen[$k] = 1
      [ordered]@{ name = $_.BaseName; path = $l.TargetPath; args = $l.Arguments }
    }
  })
  foreach ($x in @(@{ name = "v2rayN"; path = (Find-V2rayN) }, @{ name = "VS Code"; path = (Find-VSCode) })) {
    if ($x.path -and -not ($list | Where-Object { $_.path -eq $x.path })) { $list += [ordered]@{ name = $x.name; path = $x.path; args = "" } }
  }
  @($list | Sort-Object { $_.name })
}
function Get-Apps {
  # ForEach-Object unrolls the array: PowerShell 5.1 passes a JSON array on as one object
  if (Test-Path "$Agent\apps.json") { @(Get-Content "$Agent\apps.json" -Raw | ConvertFrom-Json | ForEach-Object { $_ }) } else { @() }
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
$FindClaude += @'
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
'@
. ([scriptblock]::Create($FindClaude))

$conf = if (Test-Path "$Agent\agent.json") { Get-Content "$Agent\agent.json" -Raw | ConvertFrom-Json } else { $null }
if (-not $ClaudeDir) { $ClaudeDir = if ($conf -and $conf.claude_dir) { $conf.claude_dir } else { [Environment]::GetFolderPath("MyDocuments") } }
if (-not $ClaudeMode) { $ClaudeMode = if ($conf -and $conf.claude_mode) { $conf.claude_mode } else { "hidden" } }
if ($ClaudeMode -notin "hidden", "vscode") { Write-Host "-ClaudeMode must be hidden or vscode" -ForegroundColor Red; exit 1 }

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
    claudeMode = $ClaudeMode
    apps    = @(Get-Apps)
    startApps = @(Find-StartApps)
    sshd   = "$((Get-Service sshd -ErrorAction SilentlyContinue).Status)"
    paired  = [bool]$conf
    server  = if ($conf) { "$($conf.server)" } else { "" }
    port    = if ($conf) { $conf.tunnel_port } else { 0 }
    tunnel  = [bool]$tunnel
  } | ConvertTo-Json -Compress -Depth 4
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

$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
# Interactive tasks: run on the logged-on desktop when the bot calls schtasks /run
$me = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

# The bot's app buttons: -AppsFile, else the saved list, else v2rayN + VS Code. Task WOL\AppN per app.
function Resolve-Apps {
  $in = if ($AppsFile) { @(Get-Content $AppsFile -Raw -Encoding utf8 | ConvertFrom-Json | ForEach-Object { $_ }) }
        elseif (Test-Path "$Agent\apps.json") { Get-Apps }
        else { @(@{ name = "v2rayN"; path = $(if ($V2rayN) { $V2rayN } else { Find-V2rayN }) }, @{ name = "VS Code"; path = $(if ($VSCode) { $VSCode } else { Find-VSCode }) }) }
  $n = 0
  foreach ($a in $in) {
    if (-not $a.path -or -not (Test-Path -LiteralPath $a.path) -or $n -ge 12) { continue }
    $name = ("$($a.name)" -replace '[\r\n"]', '').Trim()
    if ($name.Length -gt 30) { $name = $name.Substring(0, 30) }
    if (-not $name) { $name = [IO.Path]::GetFileNameWithoutExtension($a.path) }
    $n++
    [ordered]@{ name = $name; task = "App$n"; path = "$($a.path)"; args = "$($a.args)" }
  }
}
function Set-Apps($list) {
  Get-ScheduledTask -TaskPath "\WOL\" -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match '^(App\d+|v2rayN|VSCode)$' } |
    Unregister-ScheduledTask -Confirm:$false
  foreach ($a in $list) {
    $act = if ($a.args) { New-ScheduledTaskAction -Execute $a.path -Argument $a.args -WorkingDirectory (Split-Path $a.path) }
           else { New-ScheduledTaskAction -Execute $a.path -WorkingDirectory (Split-Path $a.path) }
    Register-ScheduledTask -TaskPath "\WOL\" -TaskName $a.task -Action $act -Principal $me -Settings $set -Force | Out-Null
    Write-Host "  $($a.name) -> $($a.path) $($a.args)"
  }
  if (-not $list) { Write-Host "  no app buttons" -ForegroundColor Yellow }
  ConvertTo-Json -InputObject @($list) -Depth 3 | Set-Content "$Agent\apps.json" -Encoding utf8
}

# Closes VS Code windows whose title contains a text (the WOL-Claude workspace), copied into wol-claude.ps1
$WinClose = @'
function Find-Window($title, [bool]$close) {
  Add-Type -ErrorAction SilentlyContinue -TypeDefinition '
using System; using System.Text; using System.Runtime.InteropServices;
public class WolWin {
  delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static int Close(string t, bool close) {
    int n = 0;
    EnumWindows((h, l) => { var s = new StringBuilder(512); GetWindowText(h, s, 512);
      if (IsWindowVisible(h) && s.ToString().Contains(t)) { if (close) PostMessage(h, 0x10, IntPtr.Zero, IntPtr.Zero); n++; } return true; }, IntPtr.Zero);
    return n;
  }
}'
  [WolWin]::Close($title, $close)
}
function Close-Window($title) { Find-Window $title $true }
'@

# Claude Code Remote Control: the bot starts it, then you continue from the Claude app / claude.ai/code
function Set-Claude {
  $ws = "$Agent\WOL-Claude.code-workspace"
  # vscode mode: a workspace of the Claude folder; the Claude panel is opened in it with vscode://anthropic.claude-code/open.
  # Panel sessions show up in claude.ai/code only with remoteControlAtStartup on, so turn it on.
  @{ folders = @(@{ path = $ClaudeDir }) } | ConvertTo-Json -Depth 4 | Set-Content $ws -Encoding utf8
  if ($ClaudeMode -eq "vscode") {
    $sf = "$env:USERPROFILE\.claude\settings.json"
    try { $s = if (Test-Path $sf) { Get-Content $sf -Raw | ConvertFrom-Json } else { [pscustomobject]@{} } } catch { $s = $null }
    if ($s -and $s.remoteControlAtStartup -ne $true) {
      $s | Add-Member -NotePropertyName remoteControlAtStartup -NotePropertyValue $true -Force
      New-Item -ItemType Directory -Force (Split-Path $sf) | Out-Null
      $s | ConvertTo-Json -Depth 20 | Set-Content $sf -Encoding utf8
      Write-Host "  Claude setting remoteControlAtStartup = true (VS Code Claude sessions show in claude.ai/code)"
    }
  }
  @"
# Starts Claude Code Remote Control in '$ClaudeDir' (task WOL\Claude), mode '$ClaudeMode':
# hidden = background 'claude remote-control' process,
# vscode = the VS Code window "WOL-Claude" ($ws) with the Claude panel open (remoteControlAtStartup puts it in claude.ai/code).
# -Status prints its log, -Stop ends it, -Setup = the one-time questions in a visible window,
# -Close = close the VS Code window (task WOL\ClaudeClose: ssh runs outside the desktop and cannot see windows).
param([switch]`$Status, [switch]`$Stop, [switch]`$Setup, [switch]`$Close)
`$mode = '$ClaudeMode'
`$log = '$Agent\claude.log'
`$ws = '$ws'
`$procs = @(Get-CimInstance Win32_Process -Filter "Name='claude.exe'" | Where-Object { `$_.CommandLine -match 'remote-control' })
$WinClose
function Open-VSCode {
  `$code = @("`$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe", "`$env:ProgramFiles\Microsoft VS Code\Code.exe") | Where-Object { Test-Path `$_ } | Select-Object -First 1
  if (-not `$code) { return `$false }
  Close-Window 'WOL-Claude' | Out-Null
  Start-Sleep -Seconds 2
  Start-Process -FilePath `$code -ArgumentList '--new-window', "``"`$ws``""
  for (`$i = 0; `$i -lt 30 -and -not (Find-Window 'WOL-Claude' `$false); `$i++) { Start-Sleep -Seconds 1 }
  Start-Sleep -Seconds 3
  # the Claude extension's URI handler opens its panel in the focused window (the new one)
  Start-Process 'vscode://anthropic.claude-code/open'
  `$true
}
if (`$Close) { Close-Window 'WOL-Claude' | Out-Null; exit 0 }
if (`$Stop) {
  `$procs | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }
  if (`$mode -eq 'vscode') { schtasks /run /tn 'WOL\ClaudeClose' | Out-Null; 'stopped (VS Code window WOL-Claude closed)'; exit 0 }
  "stopped (`$(`$procs.Count) process)"; exit 0
}
if (`$Status) {
  if (`$mode -eq 'vscode') { 'running' } elseif (`$procs.Count) { 'running' } else { 'not running' }
  foreach (`$f in `$log, "`$log.err") { if (Test-Path `$f) { Get-Content `$f -Tail 25 } }
  exit 0
}
$FindClaude
`$exe = Find-Claude
if (`$Setup) {
  if (`$mode -eq 'vscode') {
    Write-Host 'A VS Code window "WOL-Claude" opens with the Claude panel. One time only:' -ForegroundColor Cyan
    Write-Host '  Trust it: "Yes, I trust the authors" (or the Restricted Mode bar -> Manage -> Trust).'
    Write-Host '  The chat then shows in the Claude app / claude.ai/code too.'
    if (-not (Open-VSCode)) { Write-Host 'VS Code is not installed on this PC.' -ForegroundColor Red }
    Read-Host 'Press Enter to close this window'
    exit 0
  }
  `$procs | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }
  if (-not `$exe) { Write-Host 'Claude Code is not installed on this PC.' -ForegroundColor Red; exit 1 }
  Set-Location -LiteralPath '$ClaudeDir'
  if (-not (Test-ClaudeTrust '$ClaudeDir')) {
    Write-Host 'Claude asks whether you trust this folder - press Enter, then type /exit' -ForegroundColor Cyan
    & `$exe
  }
  Write-Host 'If it asks "Enable Remote Control? (y/n)" press y. Then continue from the Claude app / claude.ai/code.' -ForegroundColor Cyan
  & `$exe remote-control --name '$Name'
  exit 0
}
if (`$mode -eq 'vscode') {
  Remove-Item `$log, "`$log.err" -ErrorAction SilentlyContinue
  if (-not (Open-VSCode)) { 'error: VS Code not found on this PC' | Set-Content `$log; exit 1 }
  "vscode: `$(Get-Date -Format 'yyyy-MM-dd HH:mm') opened the VS Code window WOL-Claude with the Claude panel - continue in the Claude app / claude.ai/code (the chat is also in VS Code). Not there? On the PC: WOL Agent -> Claude one-time setup." | Set-Content `$log
  exit 0
}
if (`$procs.Count) { exit 0 }
if (-not `$exe) { 'error: Claude Code not found on this PC (install the VS Code extension or the CLI)' | Set-Content `$log; exit 1 }
Remove-Item `$log, "`$log.err" -ErrorAction SilentlyContinue
Start-Process -FilePath `$exe -ArgumentList 'remote-control', '--name', '$Name' -WorkingDirectory '$ClaudeDir' ``
  -RedirectStandardOutput `$log -RedirectStandardError "`$log.err" -WindowStyle Hidden
"@ | Set-Content -Path "$Agent\wol-claude.ps1" -Encoding utf8
  $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent\wol-claude.ps1`""
  Register-ScheduledTask -TaskPath "\WOL\" -TaskName "Claude" -Action $act -Principal $me -Settings $set -Force | Out-Null
  $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent\wol-claude.ps1`" -Close"
  Register-ScheduledTask -TaskPath "\WOL\" -TaskName "ClaudeClose" -Action $act -Principal $me -Settings $set -Force | Out-Null
  if (-not (Find-Claude)) {
    Write-Host "  Claude task added, but Claude Code is not installed yet" -ForegroundColor Yellow
    return
  }
  if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir -Force | Out-Null }
  Write-Host "  Claude -> remote-control in $ClaudeDir ($(if ($ClaudeMode -eq 'vscode') { 'in the VS Code Claude panel' } else { 'in the background' }))"
  if ($ClaudeMode -eq "hidden" -and -not (Test-ClaudeTrust $ClaudeDir)) {
    # one time: Claude asks "trust this folder?" and "enable Remote Control?" - the bot cannot answer, you do
    Start-Process powershell.exe -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Agent\wol-claude.ps1`"", "-Setup"
    Write-Host "  A Claude window opened: answer its questions once (Enter, y), then close it" -ForegroundColor Cyan
  } elseif ($ClaudeMode -eq "vscode") {
    Write-Host "  First time in VS Code mode: press 'Claude one-time setup' (Trust the VS Code window)" -ForegroundColor Cyan
  }
}

if ($Uninstall) {
  Get-ScheduledTask -TaskPath "\WOL\" -ErrorAction SilentlyContinue | ForEach-Object { Stop-ScheduledTask -InputObject $_ -ErrorAction SilentlyContinue; $_ } | Unregister-ScheduledTask -Confirm:$false
  Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -match "wol-agent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Remove-Item $Agent -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "Removed WOL tasks and $Agent. sshd and authorized keys were left as they are."
  Write-Host "Remove the PC in the bot too (or on the server: wolbot del <name>)."
  exit 0
}

if ($Update) {
  if (-not $conf) { Write-Host "Not linked yet: use a pair code first." -ForegroundColor Red; exit 1 }
  $Name = $conf.name
  Write-Host "App buttons"
  Set-Apps @(Resolve-Apps)
  Write-Host "Claude"
  Set-Claude
  $conf | Add-Member -NotePropertyName claude_dir -NotePropertyValue $ClaudeDir -Force
  $conf | Add-Member -NotePropertyName claude_mode -NotePropertyValue $ClaudeMode -Force
  $conf | ConvertTo-Json | Set-Content "$Agent\agent.json" -Encoding utf8
  Write-Host ""
  Write-Host "Saved. In the bot press 🔄 Refresh on this PC to see the new buttons." -ForegroundColor Green
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
  $AppList = @(Resolve-Apps)
  $body = @{ code = $code; name = $Name; mac = $Mac; os = "windows"; user = $env:USERNAME; tunnel_key = $pub; wake_host = $WakeHost
             apps = @($AppList | ForEach-Object { @{ name = $_.name; task = $_.task } }) } | ConvertTo-Json -Depth 4
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
@{ server = $Server; ssh_port = $ServerPort; tunnel_user = $TunnelUser; tunnel_port = $TunnelPort; name = $Name; claude_dir = $ClaudeDir; claude_mode = $ClaudeMode } | ConvertTo-Json | Set-Content "$Agent\agent.json" -Encoding utf8

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

$sys = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent\wol-tunnel.ps1`""
Register-ScheduledTask -TaskPath "\WOL\" -TaskName "Tunnel" -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $sys -Settings $set -Force | Out-Null
Write-Host "  Tunnel (at startup)"

if (-not $AppList) { $AppList = @(Resolve-Apps) }
Set-Apps $AppList
$lock = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\rundll32.exe" -Argument "user32.dll,LockWorkStation"
Register-ScheduledTask -TaskPath "\WOL\" -TaskName "Lock" -Action $lock -Principal $me -Settings $set -Force | Out-Null
Set-Claude

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
