# Start Quant-Striker so iPhone can open it on the same Wi-Fi
$ErrorActionPreference = "Stop"
$dest = Split-Path (Split-Path $PSScriptRoot -Parent) -ErrorAction SilentlyContinue
# Prefer repo root = parent of scripts
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | ForEach-Object {
  try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}
}
Start-Sleep -Seconds 1
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
# Bind all interfaces for phone access
Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList "-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8765" -WorkingDirectory $root
Start-Sleep -Seconds 2
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
  $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown"
} | Select-Object -First 1 -ExpandProperty IPAddress)
Write-Output "LAN_URL=http://${ip}:8765/"
Write-Output "LOCAL_URL=http://127.0.0.1:8765/"
try {
  # Allow inbound TCP 8765 for private networks
  $rule = Get-NetFirewallRule -DisplayName "QuantStriker8765" -ErrorAction SilentlyContinue
  if (-not $rule) {
    New-NetFirewallRule -DisplayName "QuantStriker8765" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private -ErrorAction SilentlyContinue | Out-Null
  }
  Write-Output "FIREWALL=ok_or_needs_admin"
} catch { Write-Output "FIREWALL_SKIP=$($_.Exception.Message)" }
Write-Output "Open the LAN_URL on your iPhone (same Wi-Fi)."
