@echo off
rem =====================================================================
rem  WeChat group assistant launcher (batch version).
rem  Batch file, so the PowerShell execution policy does not apply.
rem
rem  Usage:
rem    start.cmd            run normally (draft/auto per config.yaml mode)
rem    start.cmd -Setup     only create the virtualenv and install deps
rem    start.cmd -Groups    list WeChat sessions (to fill whitelist_groups)
rem    start.cmd -Check     config + brain self-check, no WeChat involved
rem    start.cmd -SelfTest  read the whitelisted groups and check field mapping
rem    start.cmd -Watch [n] watch for new messages for n seconds (default 90)
rem    start.cmd -Demo      console demo, never touches WeChat
rem    start.cmd -Probe     print the real wxauto4 API surface
rem    start.cmd -Console   use the console instead of WeChat
rem    start.cmd -UI        open the settings UI (web + WebView2 window)
rem    start.cmd -UIWeb     open it in the system browser instead (for debugging)
rem
rem  NOTE: keep this file ASCII-only. cmd.exe reads it with the console code
rem  page (GBK on zh-CN) before "chcp 65001" takes effect, so non-ASCII
rem  characters would corrupt parsing.
rem
rem  Everything lives under the folder this script sits in:
rem      <repo>\        source code, config, persona, data
rem      <repo>\venv\   virtualenv (deps installed here)
rem
rem  Nothing here is hardcoded to a machine -- paths are derived from %~dp0
rem  and %USERPROFILE%, so the repo can be cloned anywhere and just run.
rem
rem  Keep the virtualenv OUTSIDE the DSH working directory. A python
rem  interpreter located inside the working directory gets confined to it,
rem  which makes dsh fail to write ~/.dsh/profiles/wechat/cordis.yml.
rem =====================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>&1

rem  %~dp0 always ends with a backslash, so %ROOT%venv works.
set "ROOT=%~dp0"
set "VENVROOT=%ROOT%venv"
set "PY=%VENVROOT%\Scripts\python.exe"
rem  pythonw.exe = no console window. The bot redirects its own
rem  stdout/stderr to data\logs\bot-console.log, so nothing is lost.
set "PYW=%VENVROOT%\Scripts\pythonw.exe"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

rem  Base interpreter used to CREATE the venv (first run only).
rem  Order: explicit override -> DSH's bundled python -> python on PATH.
set "BASE="
if defined WECHAT_BOT_PYTHON if exist "%WECHAT_BOT_PYTHON%" set "BASE=%WECHAT_BOT_PYTHON%"
if not defined BASE if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "BASE=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined BASE for /f "usebackq delims=" %%i in (`where python 2^>nul`) do if not defined BASE set "BASE=%%i"

rem  config.yaml is not in the repo (it holds the passphrase, the allow-list
rem  and your group names). Seed it from the example on first run.
if not exist "%ROOT%config.yaml" if exist "%ROOT%config.example.yaml" (
    copy /y "%ROOT%config.example.yaml" "%ROOT%config.yaml" >nul
    echo.
    echo   Created config.yaml from config.example.yaml.
    echo   Edit it first: fill in whitelist_groups, commands.allowed_senders
    echo   and commands.secret.  Then run start.cmd again.
    echo.
    pause
    exit /b 0
)

if exist "%PY%" goto deps
echo [1/2] Creating virtualenv: %VENVROOT%
if not exist "%BASE%" goto nobase
"%BASE%" -m venv "%VENVROOT%"
if errorlevel 1 exit /b 2

:deps
if exist "%VENVROOT%\.deps-installed" goto run
echo [2/2] Installing dependencies (first run, may take a few minutes)...
"%PY%" -m pip install --upgrade pip -i %MIRROR%
"%PY%" -m pip install -r "%ROOT%requirements.txt" -i %MIRROR%
if errorlevel 1 exit /b 2
echo ok> "%VENVROOT%\.deps-installed"

:run
cd /d "%ROOT%"
rem NOTE: do NOT clear data\STOP here. -Setup/-Check/-Groups/-Watch/-Probe
rem are diagnostics; wiping the emergency-stop flag as a side effect of
rem running a diagnostic is wrong (and dangerous). The flag is cleared
rem only on the real start path, further down.
if /i "%~1"=="-Setup"   goto setup
if /i "%~1"=="-Groups"  goto groups
if /i "%~1"=="-Check"   goto check
if /i "%~1"=="-SelfTest" goto selftest
if /i "%~1"=="-Watch"   goto watch
if /i "%~1"=="-Demo"    goto demo
if /i "%~1"=="-Probe"   goto probe
if /i "%~1"=="-Console" goto console
if /i "%~1"=="-UI"      goto ui
if /i "%~1"=="-UIWeb"   goto uiweb

rem --- real start: now it is safe to clear the emergency-stop flag ---
if exist "%ROOT%data\STOP" del /q "%ROOT%data\STOP"
rem  The bot runs under pythonw (GUI subsystem), so it prints NOTHING to this
rem  console. Running it here directly would leave an EMPTY black window on
rem  screen for as long as the bot lives -- that is the "empty cmd window"
rem  complaint. `start` is not enough either: the child would inherit this
rem  console and keep the window alive.
rem  launch.py spawns the bot DETACHED (no console attached), so this window
rem  closes as soon as the script returns. On failure it returns non-zero and
rem  we pause, so the error stays readable.
"%PY%" "%ROOT%launch.py"
if errorlevel 1 pause
exit /b %errorlevel%

:setup
echo Environment ready: %PY%
exit /b 0

:groups
"%PY%" -m src.main --groups
exit /b %errorlevel%

:check
rem  Configuration self-check, then a static pass over the Python sources.
rem  The static pass exists because of a real miss: send_file() used Path()
rem  without importing it.  Import-time was fine, unit imports were fine, and
rem  it only blew up when a screenshot was actually sent.  pyflakes catches
rem  that class of error without running anything.
"%PY%" -m src.main --check
if errorlevel 1 exit /b 1
echo.
"%PY%" "%ROOT%tools\check_py.py"
if errorlevel 1 exit /b 1
exit /b 0

:selftest
"%PY%" -m src.main --selftest
exit /b %errorlevel%

:watch
"%PY%" -m src.main --watch %2
exit /b %errorlevel%

:demo
"%PY%" -m src.main --demo
exit /b %errorlevel%

:probe
"%PY%" -m src.main --probe
exit /b %errorlevel%

:console
"%PY%" -m src.main --console
exit /b %errorlevel%

:ui
rem  The settings UI is now a web page: local server + WebView2 window.
rem  The old hand-drawn Win32 UI (ui\\wechat_ui.exe) is retired.
if not exist "%VENVROOT%\Scripts\python.exe" goto noui
start "" "%VENVROOT%\Scripts\pythonw.exe" "%ROOT%ui_app.py"
exit /b 0

:uiweb
if not exist "%VENVROOT%\Scripts\python.exe" goto noui
start "" "%VENVROOT%\Scripts\pythonw.exe" "%ROOT%ui_app.py" --browser
exit /b 0

:noui
echo Virtualenv not found. Run start.cmd -Setup first.
pause
exit /b 2

:nobase
echo ERROR: base python not found: %BASE%
exit /b 2
