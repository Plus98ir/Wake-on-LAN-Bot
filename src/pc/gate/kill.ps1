$a = "$env:ProgramData\wol-agent"
@'
# wolbot kill switch: cut every remote way in. Undo on the PC: wol-unkill.ps1 (as administrator)
Add-Content "$env:ProgramData\wol-agent\kill.log" "$(Get-Date -Format s) kill switch"
schtasks /run /tn "WOL\Lock" | Out-Null
schtasks /change /tn "WOL\Tunnel" /disable | Out-Null
schtasks /end /tn "WOL\Tunnel" | Out-Null
Get-Process ssh -ErrorAction SilentlyContinue | Stop-Process -Force
Set-Service sshd -StartupType Disabled
Stop-Service sshd -Force
Get-NetAdapter | Where-Object Status -ne Disabled | Disable-NetAdapter -Confirm:$false
# red full-screen page: recovery password, then a fingerprint on the phone brings everything back
schtasks /change /tn "WOL\Rescue" /enable | Out-Null
schtasks /change /tn "WOL\RescueUI" /enable | Out-Null
schtasks /run /tn "WOL\Rescue" | Out-Null
schtasks /run /tn "WOL\RescueUI" | Out-Null
'@ | Set-Content "$a\wol-kill.ps1" -Encoding utf8
if (Test-Path "$a\wol-gate.exe") {
  $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  $r = New-ScheduledTaskAction -Execute "$a\wol-gate.exe" -Argument "rescue"
  Register-ScheduledTask -TaskPath "\WOL\" -TaskName Rescue -Action $r -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $set `
    -Principal (New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest) -Force | Out-Null
  $u = New-ScheduledTaskAction -Execute "$a\wol-gate.exe" -Argument "rescue-ui"
  Register-ScheduledTask -TaskPath "\WOL\" -TaskName RescueUI -Action $u -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings $set `
    -Principal (New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited) -Force | Out-Null
}
@'
# Undo the wolbot kill switch (run as administrator), then on the server: wolbot unlock
Get-NetAdapter | Enable-NetAdapter -Confirm:$false
Set-Service sshd -StartupType Automatic
Start-Service sshd
schtasks /change /tn "WOL\Tunnel" /enable | Out-Null
schtasks /run /tn "WOL\Tunnel" | Out-Null
'@ | Set-Content "$a\wol-unkill.ps1" -Encoding utf8
foreach ($f in "wol-kill.ps1", "wol-unkill.ps1") { icacls "$a\$f" /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null }
$act = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$a\wol-kill.ps1`""
$sys = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskPath "\WOL\" -TaskName Kill -Action $act -Principal $sys -Force | Out-Null
Start-ScheduledTask -TaskPath "\WOL\" -TaskName Kill
