# run-quick.ps1 — Windows (PowerShell) version of run-quick.sh
#
# Expose the scanner over HTTPS with a Cloudflare quick tunnel (no account needed),
# for testing push on your phone. The URL is ephemeral and changes each run.
#
# Run from anywhere:   powershell -ExecutionPolicy Bypass -File tunnel\run-quick.ps1
# Or right-click the file > Run with PowerShell.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# Load .env (strip quotes, skip comments/blanks) into this process.
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

  Write-Host ""
  Write-Host "Opening a Cloudflare quick tunnel - look for the https://<...>.trycloudflare.com URL below."
  Write-Host "Open that URL on your phone, then Add to Home Screen and tap the bell to enable alerts."
  Write-Host ""
  cloudflared tunnel --url "http://localhost:$port"
}
finally {
  if ($app -and -not $app.HasExited) {
    Write-Host "Shutting down app..."
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
  }
}
