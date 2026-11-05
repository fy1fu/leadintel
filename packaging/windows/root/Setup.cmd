@echo off
title LeadIntel Setup
rem Installs (or updates) LeadIntel for this Windows user. No administrator rights needed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\packaging\windows\setup.ps1" %*
echo.
pause
