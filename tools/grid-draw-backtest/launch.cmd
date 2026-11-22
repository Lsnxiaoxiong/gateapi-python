@echo off
setlocal
python "%~dp0serve.py" %*
if errorlevel 1 pause
