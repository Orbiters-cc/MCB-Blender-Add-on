@echo off
setlocal

set "ROOT=%~dp0"
set "SRC=%ROOT%mcb_blender"
set "DST=%APPDATA%\Blender Foundation\Blender\5.0\scripts\addons\mcb_blender"

if exist "%DST%" rmdir /s /q "%DST%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Path '%SRC%' -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { if (Test-Path -LiteralPath $_.FullName) { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } }"
xcopy "%SRC%" "%DST%\" /e /i /y >nul

if errorlevel 1 (
  echo Failed to sync addon files.
  exit /b 1
)

echo Synced addon to %DST%
echo In Blender, run "Reload Scripts" from F3 to pick up changes without reinstalling the zip.
