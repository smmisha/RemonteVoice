@echo off
rem Elevated acceptance of the newest RemonteVoice setup package.
rem Cycle: install -> update (repair) -> uninstall -> final install.
rem Requires one UAC confirmation and leaves the product installed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-Acceptance.ps1"
if errorlevel 1 (
    echo.
    echo Acceptance FAILED. See host\build\package-acceptance\acceptance.log
    pause
    exit /b 1
)
echo.
echo Acceptance passed. Evidence: host\build\package-acceptance\acceptance.json
pause
exit /b 0
