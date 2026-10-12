@echo off
rem =====================================================================
rem  Start an ISOLATED Edge with the remote-debugging port for tools\cdp.py.
rem
rem  WHY THIS SCRIPT EXISTS
rem  ----------------------
rem  Edge only honours --remote-debugging-port when it STARTS. If an Edge
rem  is already running, a new invocation hands the URL to the existing
rem  process and the flag is silently ignored.
rem
rem  That is what made the bot's \agent do the wrong thing: it needed the
rem  port, could not add it to the running Edge, so it ran
rem  `Stop-Process -Name msedge -Force` and killed the browser the user
rem  was using -- after being told not to.
rem
rem  READ THIS: WHY THIS SCRIPT WAS REWRITTEN (2026-10-12)
rem  -----------------------------------------------------
rem  The first version pointed --user-data-dir at a JUNCTION into the real
rem  profile (%LOCALAPPDATA%\Microsoft\Edge\User Data), to keep the login
rem  state and the installed userscripts. That was a mistake and it caused
rem  real damage:
rem
rem    * Edge refuses remote debugging on the DEFAULT profile directory,
rem      so the junction was a way to defeat that guard -- not a fix.
rem    * Loaded through a different path, Edge treats it as a separate
rem      profile: it shows "enable 'Allow user scripts'" and the userscript
rem      (Tampermonkey / the course helper) does NOT run.
rem    * Two Edge instances over one profile directory, plus forced kills,
rem      corrupted profile state: site logins were lost and the user had to
rem      sign in again.
rem
rem  So: NEVER point this at the user's real profile. This script now uses
rem  a dedicated directory that nothing else touches.
rem
rem  ONE-TIME SETUP (do it once, in the window this script opens)
rem  ------------------------------------------------------------
rem    1. sign in to whatever site you want automated
rem    2. install the userscript manager + the script you need
rem    3. open the extensions page and turn ON "Allow user scripts"
rem       (Developer mode); Edge asks for this per profile and shows the
rem       blue banner until it is on
rem  That profile lives only for automation. Your normal Edge is untouched.
rem
rem  WHILE IT IS RUNNING
rem  -------------------
rem  ANY local process can drive this browser over http://127.0.0.1:9222.
rem  That is the point, and it is why the profile is isolated: it holds no
rem  credentials you did not deliberately put there. Close it when done.
rem
rem  NOTE: keep this file ASCII-only. cmd.exe reads a .cmd with the console
rem  code page (GBK on zh-CN), so non-ASCII text corrupts parsing.
rem  (The previous version had a Chinese word in a comment and it broke.)
rem =====================================================================
setlocal EnableExtensions

set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
    echo ERROR: msedge.exe not found. Edit this file and set EDGE.
    pause
    exit /b 2
)

rem  Dedicated automation profile. NOT a junction. NOT the real profile.
set "PROFILE=%LOCALAPPDATA%\wechat-addon-edge-automation"

rem  Only a REAL msedge.exe counts as "already running". The old check
rem  matched any leftover background process, so after closing every window
rem  the script still refused -- the user had to close Edge twice for no
rem  reason.
tasklist /fi "imagename eq msedge.exe" 2>nul | find /i "msedge.exe" >nul
if not errorlevel 1 (
    echo.
    echo   An Edge process is already running, so --remote-debugging-port
    echo   would be ignored by the new instance.
    echo.
    echo   This script will NOT close it for you: your Edge may have
    echo   unsaved work, and killing someone's browser is not a decision a
    echo   script should make.
    echo.
    echo   If you are sure it is only a leftover background process:
    echo       tasklist ^| findstr /i msedge
    echo   and close it from Task Manager. Then run this again.
    echo.
    pause
    exit /b 1
)

if not exist "%PROFILE%" (
    echo First run: creating the automation profile at
    echo   %PROFILE%
    mkdir "%PROFILE%" 2>nul
)

echo Starting Edge (isolated automation profile)
echo   profile : %PROFILE%
echo   debug   : http://127.0.0.1:9222
echo.
echo First time only: sign in there, install your userscript, and turn on
echo "Allow user scripts" in the extensions page.
echo.

start "" "%EDGE%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%" --profile-directory=Default --no-first-run

echo Check that the port is really up:
echo   venv\Scripts\python.exe tools\cdp.py tabs
echo.
exit /b 0
