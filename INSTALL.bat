@echo off
chcp 65001 >nul
echo Mudaqqiq Altabaay - installing (needs internet once)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
