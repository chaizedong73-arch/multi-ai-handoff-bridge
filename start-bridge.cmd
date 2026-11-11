@echo off
rem ============================================================
rem  多AI交接桥 · 控制台启动（需先在 WSL 仓库目录运行 install.sh）
rem  UI: http://localhost:8765/   停止: stop-bridge.cmd
rem ============================================================
for /f "usebackq delims=" %%i in (`wsl.exe wslpath -u "%~dp0"`) do set WSLREPO=%%i
echo [bridge] starting uvicorn in WSL (port 8765) ...
wsl.exe -e bash "%WSLREPO%/serve-bridge.sh"
pause
