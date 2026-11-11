@echo off
rem ============================================================
rem  多AI交接桥 · 停止引擎（按 pidfile 发 SIGTERM，兜底 SIGKILL）
rem ============================================================
for /f "usebackq delims=" %%i in (`wsl.exe wslpath -u "%~dp0"`) do set WSLREPO=%%i
wsl.exe -e bash "%WSLREPO%/stop-bridge.sh"
pause
