@echo off
REM Launch the GateAPI K-Line & Ranking service.
REM Usage: run_server.bat [host] [port]    (defaults: 0.0.0.0 8787)
setlocal
set HOST=%~1
if "%HOST%"=="" set HOST=0.0.0.0
set PORT=%~2
if "%PORT%"=="" set PORT=8787

cd /d "%~dp0"
set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
echo Starting GateAPI K-Line service on http://%HOST%:%PORT% ...
echo Python: %PYTHON_EXE%
echo Web UI: http://localhost:%PORT%/
echo API docs: http://localhost:%PORT%/docs
echo.
"%PYTHON_EXE%" -m tools.api_server --host %HOST% --port %PORT%
endlocal
