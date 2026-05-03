@echo off
setlocal

set "ROOT=%~dp0"
set "DIST=%ROOT%dist"
set "ZIP=%DIST%\mcb_blender.zip"

if not exist "%DIST%" mkdir "%DIST%"
if exist "%ZIP%" del "%ZIP%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Path '%ROOT%mcb_blender' -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { if (Test-Path -LiteralPath $_.FullName) { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } }"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Path '%ROOT%mcb_blender' -DestinationPath '%ZIP%' -Force"

if errorlevel 1 (
  echo Failed to build addon zip.
  exit /b 1
)

echo Built %ZIP%
