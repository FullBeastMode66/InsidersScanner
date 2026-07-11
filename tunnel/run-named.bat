@echo off
REM run-named.bat - Windows (cmd) version of run-named.sh
REM Stable Cloudflare named tunnel. Point it via CLOUDFLARED_TOKEN in .env, or
REM a tunnel\cloudflared.yml config file. One-time setup is in TUNNEL.md.
REM Run from the project root:   tunnel\run-named.bat

setlocal enabledelayedexpansion
cd /d "%~dp0.."

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

for /l %%i in (1,1,20) do (
  curl -sf "http://127.0.0.1:%PORT%/api/health" >nul 2>nul && goto :ready
  timeout /t 1 >nul
)
:ready

if not "%CLOUDFLARED_TOKEN%"=="" (
  echo Running named tunnel in token mode...
  cloudflared tunnel run --token %CLOUDFLARED_TOKEN%
) else if exist "tunnel\cloudflared.yml" (
  echo Running named tunnel from tunnel\cloudflared.yml ...
  cloudflared tunnel --config "tunnel\cloudflared.yml" run
) else (
  echo No tunnel configured. Set CLOUDFLARED_TOKEN in .env, or create tunnel\cloudflared.yml.
  echo See tunnel\TUNNEL.md for the one-time setup.
  taskkill /F /FI "WINDOWTITLE eq scanner-app*" >nul 2>nul
  exit /b 1
)

taskkill /F /FI "WINDOWTITLE eq scanner-app*" >nul 2>nul
endlocal
