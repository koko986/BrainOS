@echo off
setlocal
cd /d "%~dp0"

set "CODEX_PY=C:\Users\M S I\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%CODEX_PY%" (
  set "PYTHON_EXE=%CODEX_PY%"
) else (
  set "PYTHON_EXE=python"
)

echo Second Brain AI CLI Demo
echo ========================
echo.
echo Using Python:
"%PYTHON_EXE%" --version
echo.

echo 1. Seeding demo knowledge...
"%PYTHON_EXE%" -m second_brain.app.main seed-demo
echo.

echo 2. Listing entities...
"%PYTHON_EXE%" -m second_brain.app.main list-entities
echo.

echo 3. Asking local LLM: "List entities"
"%PYTHON_EXE%" -m second_brain.app.main ask "List entities"
echo.

echo Done. This is a CLI prototype, so output appears in this terminal window.
pause
