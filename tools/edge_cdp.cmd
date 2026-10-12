@echo off
rem =====================================================================
rem  Start Edge with the remote-debugging port, so tools\cdp.py can drive it.
rem
rem  WHY THIS SCRIPT EXISTS
rem  ----------------------
rem  Edge only honours --remote-debugging-port when it STARTS. If an Edge
rem  is already running, a new invocation just hands the URL to the
rem  existing process and the flag is silently ignored.
rem
rem  That is exactly what made the bot's \agent do the wrong thing:
rem  it needed the port, could not add it to the running Edge, so it ran
rem  `Stop-Process -Name msedge -Force` and killed the browser the user
rem  was using -- after being told not to.
rem
rem  Two more things that make "leave it open" impossible from inside
rem  \agent: anything the agent starts with Start-Process is reclaimed
rem  the moment its instruction ends (verified: a launched cmd was gone
rem  right after the run). Starting the browser HERE -- outside the
rem  agent's process tree -- sidesteps that too.
rem
rem  SO: start the browser yourself with this script, then ask the bot's
rem  \agent to operate on it. It will attach and never need to kill
rem  anything.
rem
rem  READ THIS BEFORE USING IT
rem  -------------------------
rem  While this Edge is running, ANY local process can drive your browser
rem  over http://127.0.0.1:9222 -- read pages, click, and act as you on
rem  sites where you are logged in. That is the whole point (it is what
rem  lets the bot click through 学习通), but it is a real trade-off:
rem  close Edge when you are done.
rem
rem  SECURITY NOTE: this file is ASCII-only on purpose. cmd.exe reads a
rem  .cmd with the console code page (GBK on zh-CN), so non-ASCII text
rem  would corrupt parsing. Same rule as start.cmd.
rem =====================================================================
setlocal EnableExtensions

set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
    echo ERROR: msedge.exe not found. Edit this file and set EDGE.
    pause
    exit /b 2
)

rem  Already running? Then the port flag would be ignored -- say so instead
rem  of pretending it worked.
tasklist /fi "imagename eq msedge.exe" 2>nul | find /i "msedge.exe" >nul
if not errorlevel 1 (
    echo.
    echo   Edge is ALREADY running, so --remote-debugging-port will be ignored.
    echo.
    echo   Close Edge completely first, then run this again.
    echo   ^(This script deliberately does NOT close it for you -- killing a
    echo    browser that might have unsaved work in it is not something a
    echo    script should decide.^)
    echo.
    pause
    exit /b 1
)

rem  --user-data-dir is passed explicitly on purpose: with the default
rem  profile directory some Edge builds refuse to open the debugging port.
rem  This is a junction to the real profile (created once, points at
rem  %LOCALAPPDATA%\Microsoft\Edge\User Data), so the login state and the
rem  installed userscripts are all still there.
set "PROFILE=%USERPROFILE%\EdgeCDP"
if not exist "%PROFILE%" (
    echo Creating "%PROFILE%" as a junction to the real Edge profile...
    mklink /J "%PROFILE%" "%LOCALAPPDATA%\Microsoft\Edge\User Data" >nul
)

echo Starting Edge with remote debugging on http://127.0.0.1:9222
start "" "%EDGE%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%" --profile-directory=Default --no-first-run

echo.
echo   Done. Now check the port is really up:
echo     venv\Scripts\python.exe tools\cdp.py tabs
echo.
exit /b 0
