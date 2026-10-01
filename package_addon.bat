@echo off
setlocal

rem Builds dist\mcb_blender-<version>.zip with Blender's extension builder, which also validates the manifest.
rem Usage: package_addon.bat [path\to\blender.exe]   (or set BLENDER; defaults to blender on PATH)

set "ROOT=%~dp0"
set "DIST=%ROOT%dist"
set "BLENDER_EXE=%~1"
if "%BLENDER_EXE%"=="" set "BLENDER_EXE=%BLENDER%"
if "%BLENDER_EXE%"=="" set "BLENDER_EXE=blender"

if not exist "%DIST%" mkdir "%DIST%"

"%BLENDER_EXE%" --factory-startup --command extension build --source-dir "%ROOT%mcb_blender" --output-dir "%DIST%"
if errorlevel 1 (
  echo Failed to build the extension zip. Pass the Blender executable as the first argument or set BLENDER.
  exit /b 1
)

echo Built the extension zip in %DIST%
