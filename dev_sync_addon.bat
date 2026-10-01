@echo off
setlocal

rem Copies the extension into Blender's local "user_default" extension repository.
rem Usage: dev_sync_addon.bat [blender version, default 5.0]

set "ROOT=%~dp0"
set "SRC=%ROOT%mcb_blender"
set "BLENDER_VERSION=%~1"
if "%BLENDER_VERSION%"=="" set "BLENDER_VERSION=5.0"
set "DST=%APPDATA%\Blender Foundation\Blender\%BLENDER_VERSION%\extensions\user_default\mcb_blender"

if exist "%DST%" rmdir /s /q "%DST%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Path '%SRC%' -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { if (Test-Path -LiteralPath $_.FullName) { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } }"
xcopy "%SRC%" "%DST%\" /e /i /y >nul

if errorlevel 1 (
  echo Failed to sync extension files.
  exit /b 1
)

echo Synced extension to %DST%
echo In Blender, enable "MCB" in Preferences ^> Add-ons once, then use "Reload Scripts" from F3 to pick up changes.
