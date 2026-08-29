@echo off
setlocal
cd /d "%~dp0"

set "CODEX_PY=C:\Users\M S I\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%CODEX_PY%" (
  set "PYTHON_EXE=%CODEX_PY%"
) else (
  set "PYTHON_EXE=python"
)

echo Starting MARLIN BrainOS...
echo Hands-free mode. Say "Marlin" to wake me. Ctrl+C to stop.
echo.
"%PYTHON_EXE%" main.py jarvis
pause
