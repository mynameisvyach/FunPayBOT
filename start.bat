@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if exist ".venv\Scripts\python.exe" goto launch
py -3.12 -c "import sys" >nul 2>nul
if errorlevel 1 (set "PY_CMD=py -3") else (set "PY_CMD=py -3.12")
%PY_CMD% -c "import sys; assert sys.version_info >= (3,11), 'Нужен Python 3.11 или новее'"
if errorlevel 1 exit /b 1
%PY_CMD% -m venv .venv
if errorlevel 1 exit /b 1
:launch
".venv\Scripts\python.exe" main.py --run %*
exit /b %errorlevel%
