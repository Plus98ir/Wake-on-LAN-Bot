# wol-gate neutralize: put one "way into the PC" back to its approved state the instant a dangerous
# change is seen (before the full network kill). Input on $env:WOLGATE_NEUT = {kind, base:[], cur:[]}.
# base/cur are the same strings the watchdog snapshot (watch.ps1) produces for that kind.
$ErrorActionPreference = "Stop"
$d = $env:WOLGATE_NEUT | ConvertFrom-Json
$kind = $d.kind
$base = @($d.base)
$cur = @($d.cur)
# names in base, lowercased, for "is this one approved?" checks
$keep = @{}
foreach ($n in $base) { if ("$n" -ne "") { $keep[("$n").ToLower()] = $true } }

switch ($kind) {
  "rdp" {
    if ($base.Count) { Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value ([int]$base[0]) }
  }
  "uac" {
    if ($base.Count) {
      $v = "$($base[0])" -split '\s+'
      $k = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
      if ($v.Count -ge 1 -and $v[0] -ne "") { Set-ItemProperty $k -Name EnableLUA -Value ([int]$v[0]) }
      if ($v.Count -ge 2 -and $v[1] -ne "") { Set-ItemProperty $k -Name ConsentPromptBehaviorAdmin -Value ([int]$v[1]) }
      if ($v.Count -ge 3 -and $v[2] -ne "") { Set-ItemProperty $k -Name PromptOnSecureDesktop -Value ([int]$v[2]) }
    }
  }
  "firewall" {
    foreach ($line in $base) {
      $p = "$line" -split '\s+'     # "Name Enabled DefaultInboundAction AllowInboundRules"
      if ($p.Count -ge 4) { Set-NetFirewallProfile -Name $p[0] -Enabled $p[1] -DefaultInboundAction $p[2] -AllowInboundRules $p[3] }
    }
  }
  "admins" {
    foreach ($n in $cur) { if ("$n" -ne "" -and -not $keep[("$n").ToLower()]) { try { Remove-LocalGroupMember -SID S-1-5-32-544 -Member $n -ErrorAction Stop } catch {} } }
  }
  "users" {
    foreach ($n in $cur) { if ("$n" -ne "" -and -not $keep[("$n").ToLower()]) { try { Disable-LocalUser -Name $n -ErrorAction Stop } catch {} } }
  }
  "remote" {
    foreach ($n in $cur) { if ("$n" -ne "" -and -not $keep[("$n").ToLower()]) { Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } }
  }
}
"neutralized $kind"
