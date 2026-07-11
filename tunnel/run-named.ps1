# run-named.ps1 — Windows (PowerShell) version of run-named.sh
#
# Run the scanner behind a STABLE Cloudflare named tunnel. One-time setup is in
# TUNNEL.md. Point this at your tunnel with EITHER a token in .env
# (CLOUDFLARED_TOKEN=eyJ...) OR a tunnel\cloudflared.yml config file.
#
# Run:  powershell -ExecutionPolicy Bypass -File tunnel\run-named.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Test-Path ".env") {
  foreach ($line in Get-Content ".env") {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith("#") -and $t.Contains("=")) {
      $parts = $t -split "=", 2
      $k = $parts[0].Trim()
      $v = $parts[1].Trim().Trim('"')
      [Environment]::SetEnvironmentVariable($k, $v, "Process")
    }
  }
}

$port = if ($env:PORT) { $env:PORT } else { "8000" }

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Host "cloudflared not found. Install it:  winget install --id Cloudflare.cloudflared"
  exit 1
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "python not found on PATH."
  exit 1
}

Write-Host "Starting app on http://127.0.0.1:$port ..."
$app = Start-Process -FilePath "python" `
  -ArgumentList "-m","uvicorn","api:app","--host","127.0.0.1","--port",$port `
  -PassThru -WindowStyle Minimized

try {
  for ($i = 0; $i -lt 20; $i++) {
    try {
      Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
      break
    } catch { Start-Sleep -Seconds 1 }
  }

  $config = Join-Path $PSScriptRoot "cloudflared.yml"
  if ($env:CLOUDFLARED_TOKEN) {
    Write-Host "Running named tunnel in token mode..."
    cloudflared tunnel run --token $env:CLOUDFLARED_TOKEN
  }
  elseif (Test-Path $config) {
    Write-Host "Running named tunnel from $config ..."
    cloudflared tunnel --config $config run
  }
  else {
    Write-Host "No tunnel configured. Set CLOUDFLARED_TOKEN in .env, or create tunnel\cloudflared.yml."
    Write-Host "See tunnel\TUNNEL.md for the one-time setup."
    exit 1
  }
}
finally {
  if ($app -and -not $app.HasExited) {
    Write-Host "Shutting down app..."
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
  }
}
