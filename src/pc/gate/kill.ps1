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
'@ | Set-Content "$a\wol-kill.ps1" -Encoding utf8
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
