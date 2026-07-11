@echo off
REM run-quick.bat - Windows (cmd) version of run-quick.sh
REM Cloudflare quick tunnel (no account) for testing push. Ephemeral URL.
REM Run from the project root:   tunnel\run-quick.bat

setlocal enabledelayedexpansion
cd /d "%~dp0.."

REM --- load .env (skip # comments, strip surrounding quotes) ---
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    set "k=%%A"
    set "v=%%B"
    if defined k (
      set "v=!v:"=!"
      set "!k!=!v!"
    )
  )
)

if "%PORT%"=="" set "PORT=8000"

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo cloudflared not found. Install it:  winget install --id Cloudflare.cloudflared
  exit /b 1
)
where python >nul 2>nul
if errorlevel 1 (
  echo python not found on PATH.
  exit /b 1
)

echo Starting app on http://127.0.0.1:%PORT% ...
start "scanner-app" /min cmd /c "python -m uvicorn api:app --host 127.0.0.1 --port %PORT%"

REM --- wait for the app to answer /api/health ---
for /l %%i in (1,1,20) do (
  curl -sf "http://127.0.0.1:%PORT%/api/health" >nul 2>nul && goto :ready
  timeout /t 1 >nul
)
:ready

echo.
echo Opening a Cloudflare quick tunnel - look for the https://^<...^>.trycloudflare.com URL below.
echo Open that URL on your phone, then Add to Home Screen and tap the bell.
echo.
cloudflared tunnel --url "http://localhost:%PORT%"

REM --- clean up the app window when the tunnel stops ---
taskkill /F /FI "WINDOWTITLE eq scanner-app*" >nul 2>nul
endlocal
