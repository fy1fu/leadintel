@echo off
title Stop LeadIntel
set "LIW=%LOCALAPPDATA%\LeadIntel\app\packaging\windows"
if not exist "%LIW%\stop.ps1" (
  echo LeadIntel is not installed yet. Double-click Setup.cmd first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LIW%\stop.ps1" %*
