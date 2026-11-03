# wol-gate watchdog: facts about something new, so the owner can tell an install from an attack.
# Input: env WOLGATE_EXPLAIN = {"kind":"services|tasks|remote","names":[...]}. Output: JSON list.
$ErrorActionPreference = "SilentlyContinue"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$q = $env:WOLGATE_EXPLAIN | ConvertFrom-Json

# "C:\a b\x.exe" /arg  or  C:\a b\x.exe /arg  or  x.exe /arg -> path + args
function Split-Cmd([string]$c) {
  $c = [Environment]::ExpandEnvironmentVariables("$c").Trim()
  if ($c -match '^"([^"]+)"\s*(.*)$') { return @($Matches[1], $Matches[2]) }
  if ($c -match '^(.+?\.(exe|com|bat|cmd|ps1|vbs|js))(\s+.*)?$') { return @($Matches[1], "$($Matches[3])".Trim()) }
  return @($c, "")
}

function New-ChangeItem([string]$name, [string]$path, [string]$cmdArgs, [string]$account, [string]$workDir = "") {
  $path = [Environment]::ExpandEnvironmentVariables("$path").Trim('"', ' ')
  if ($path -and $path -notmatch '[\\/]') {
    # a task may name just "App" and rely on its working folder
    $workDir = [Environment]::ExpandEnvironmentVariables("$workDir").Trim('"', ' ')
    $inDir = if ($workDir) { @("$path", "$path.exe") | ForEach-Object { Join-Path $workDir $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1 }
    if ($inDir) { $path = $inDir }
    else {
      $found = Get-Command -Name $path -CommandType Application | Select-Object -First 1
      if ($found) { $path = $found.Source }
    }
  }
  $sig = ""; $signer = ""
  if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) {
    $s = Get-AuthenticodeSignature -LiteralPath $path
    $sig = "$($s.Status)"
    if ("$($s.SignerCertificate.Subject)" -match 'CN="?([^,"]+)') { $signer = $Matches[1].Trim() }
  } else { $sig = "NoFile" }
  [pscustomobject]@{ name = $name; path = $path; args = "$cmdArgs".Trim(); account = $account; sig = $sig; signer = $signer }
}

$out = @()
foreach ($n in @($q.names)) {
  $hit = $null
  switch ($q.kind) {
    "services" {
      # the watchdog drops the random suffix of per-user services
      $re = '^' + [regex]::Escape($n) + '(_[0-9a-f]{4,})?$'
      $svc = @(Get-CimInstance Win32_Service | Where-Object { $_.Name -match $re }) | Select-Object -First 1
      if ($svc) { $p = Split-Cmd $svc.PathName; $hit = New-ChangeItem $n $p[0] $p[1] $svc.StartName }
    }
    "tasks" {
      $t = @(Get-ScheduledTask | Where-Object { "$($_.TaskPath)$($_.TaskName)" -eq $n }) | Select-Object -First 1
      if ($t) { $a = @($t.Actions)[0]; $hit = New-ChangeItem $n $a.Execute $a.Arguments $t.Principal.UserId $a.WorkingDirectory }
    }
    "remote" {
      $pr = @(Get-Process | Where-Object { $_.Name -eq $n -and $_.Path }) | Select-Object -First 1
      if ($pr) { $hit = New-ChangeItem $n $pr.Path "" "" }
    }
  }
  if (-not $hit) { $hit = [pscustomobject]@{ name = $n; path = ""; args = ""; account = ""; sig = "NoFile"; signer = "" } }
  $out += $hit
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
