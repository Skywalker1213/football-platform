# Registers a Windows Scheduled Task "FootballPlatformDaily" at 09:00 local time
# to run the daily collect → learn → predict pipeline.
#
# Expected layout (laptop Documents copy):
#   %USERPROFILE%\Documents\football-platform\.venv\Scripts\python.exe
#   %USERPROFILE%\Documents\football-platform\scripts\daily_update.py
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows_daily_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows_daily_task.ps1 -Root "D:\apps\football-platform"
#
# Administrator / elevated PowerShell may be required depending on policy.
# To remove: Unregister-ScheduledTask -TaskName FootballPlatformDaily -Confirm:$false

param(
    [string]$Root = (Join-Path $env:USERPROFILE "Documents\football-platform"),
    [string]$Time = "09:00"
)

$ErrorActionPreference = "Stop"
$TaskName = "FootballPlatformDaily"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\daily_update.py"

if (-not (Test-Path $Python)) {
    Write-Error "Python not found: $Python — create the venv under the platform root first."
}
if (-not (Test-Path $Script)) {
    Write-Error "daily_update.py not found: $Script"
}

# cmd.exe wrapper so we can set PYTHONPATH + FOOTBALL_SKIP_WEATHER for the process
$Cmd = "$env:ComSpec"
$Arg = "/c set PYTHONPATH=$Root&& set FOOTBALL_SKIP_WEATHER=1&& `"$Python`" `"$Script`""

$Action = New-ScheduledTaskAction -Execute $Cmd -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Force | Out-Null
} catch {
    Write-Warning "Register-ScheduledTask failed (admin may be needed): $_"
    Write-Host ""
    Write-Host "Manual Task Scheduler setup:"
    Write-Host "  Program/script : $Cmd"
    Write-Host "  Arguments      : $Arg"
    Write-Host "  Start in       : $Root"
    Write-Host "  Trigger        : Daily at $Time"
    exit 1
}

Write-Host "OK — scheduled task '$TaskName' runs daily at $Time local."
Write-Host "  Root   : $Root"
Write-Host "  Python : $Python"
Write-Host "  Script : $Script"
Write-Host "Admin elevation may be required on locked-down machines."
