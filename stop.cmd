@echo off
rem =====================================================================
rem  EMERGENCY STOP for the WeChat bot.
rem
rem  All the real work (stop flag + killing the bot) lives in stop.py,
rem  because it needs to match processes by command line, not just by
rem  executable path -- an instance started with a different interpreter
rem  would otherwise survive the emergency stop.
rem
rem  NOTE: keep this file ASCII-only. cmd.exe reads a .cmd file with the
rem  console code page (GBK on zh-CN) before any "chcp" takes effect,
rem  so non-ASCII characters here would corrupt parsing.
rem =====================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>&1

set "ROOT=%~dp0"
set "PY=%ROOT%venv\Scripts\python.exe"

if not exist "%ROOT%data" mkdir "%ROOT%data"

if exist "%PY%" (
    "%PY%" "%ROOT%stop.py"
    exit /b 0
)

rem --- no venv: fall back to a plain flag + best-effort kill ---
echo stopped by stop.cmd at %DATE% %TIME%> "%ROOT%data\STOP"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.ExecutablePath -like '%ROOT%*' -or $_.CommandLine -match '-m\s+src\.main' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo.
echo Bot stopped. Restart with: %ROOT%start.cmd
timeout /t 4 >nul 2>&1
exit /b 0
