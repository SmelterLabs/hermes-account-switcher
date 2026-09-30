@echo off
rem Hermes Account Switcher: install and set up with Hermes's own Python, so no separate Python is needed.
rem   setup.cmd                        install into the first profile and set up the Codex accounts
rem   setup.cmd profile <name>         install into one more profile (the accounts are shared)
rem   setup.cmd claude <key> <email>   add a Claude account and sign it in (the key alone signs it in again)
rem   setup.cmd service <name>         only if the gateway runs as a Windows service that setup did not find
rem   setup.cmd show                   show what setup would write, and write nothing (asks nothing)
rem   setup.cmd auto ["email=Name" ...]  write without asking; an account without a name gets the one offered
rem                                    ("claude:email=Name" names a Claude account)
setlocal
set "ROOT=%LOCALAPPDATA%\hermes"
if not exist "%ROOT%\config.yaml" (
  echo Hermes was not found at %ROOT%. Install Hermes first.
  exit /b 1
)
set "PY="
for /d %%D in ("%ROOT%\tools\python-*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY (
  echo Hermes's own Python was not found under %ROOT%\tools. Open Hermes once, then run this again.
  exit /b 1
)
set "HERE=%~dp0"
if /i "%~1"=="claude" goto claude
if /i "%~1"=="service" goto service
if /i "%~1"=="show" goto show
set "AUTO="
if /i "%~1"=="auto" goto auto
:install
set "TARGET=%ROOT%"
set "PROFILE="
if /i "%~1"=="profile" set "PROFILE=%~2"
if /i "%~1"=="profile" if "%~2"=="" (
  echo Usage: setup.cmd profile ^<name^>
  exit /b 1
)
if not defined AUTO if not "%~1"=="" if /i not "%~1"=="profile" (
  echo Usage: setup.cmd, setup.cmd show, setup.cmd auto, setup.cmd profile ^<name^>,
  echo        setup.cmd claude ^<key^> ^<email^>, or setup.cmd service ^<name^>
  exit /b 1
)
if defined PROFILE set "TARGET=%ROOT%\profiles\%PROFILE%"
if not exist "%TARGET%\config.yaml" (
  echo There is no Hermes profile at %TARGET%.
  exit /b 1
)
rem A copy Hermes installed from its catalog already sits in the plugins folder; only settings are needed then.
if /i "%HERE%"=="%TARGET%\plugins\codex-account-switch\" goto settings
"%PY%" "%HERE%install.py" install --home "%TARGET%"
if errorlevel 1 exit /b 1
:settings
if defined PROFILE goto enable
"%PY%" "%HERE%setup_settings.py" --home "%ROOT%" %AUTO%
if errorlevel 1 exit /b 1
:enable
echo.
echo Next, turn the plugin on yourself:
if defined PROFILE (echo   hermes -p %PROFILE% plugins enable codex-account-switch) else (echo   hermes plugins enable codex-account-switch)
echo Then restart Hermes Desktop. Under Capabilities, Plugins, Installed, turn on the switch on the plugin's
echo card; if no account button appears at the bottom right, open the card and turn on "Desktop" as well.
exit /b 0
:show
"%PY%" "%HERE%setup_settings.py" --home "%ROOT%" --show
exit /b %errorlevel%
:auto
set "AUTO=--yes"
:autonext
shift
if "%~1"=="" goto install
set "AUTO=%AUTO% --name "%~1""
goto autonext
:service
if "%~2"=="" (
  echo Usage: setup.cmd service ^<Windows service name^>
  exit /b 1
)
"%PY%" "%HERE%setup_settings.py" --home "%ROOT%" --gateway-service "%~2"
exit /b %errorlevel%
:claude
if "%~2"=="" (
  echo Usage: setup.cmd claude ^<key^> ^<email^>     for example: setup.cmd claude personal me@example.com
  echo        setup.cmd claude ^<key^>             to sign an account in again
  exit /b 1
)
if "%~3"=="" goto signin
"%PY%" "%HERE%setup_settings.py" --home "%ROOT%" --claude "%~2=%~3"
if errorlevel 1 exit /b 1
:signin
"%PY%" "%HERE%claude_login.py" %~2
exit /b %errorlevel%
