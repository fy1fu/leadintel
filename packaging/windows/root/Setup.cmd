@echo off
title LeadIntel Setup
rem Installs (or updates) LeadIntel for this Windows user. No administrator rights needed.
rem On success Setup itself offers to start LeadIntel; the window only waits here if Setup stopped with an error.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\packaging\windows\setup.ps1" %*
if errorlevel 1 (
  echo.
  pause
)
