@echo off
rem ======================================================================
rem  Double-click to start the EBC-A20 web interface (Windows).
rem
rem  Looks for a Python 3.10+ that already has pyserial. If none does, it
rem  creates a private environment in .venv next to this file and installs
rem  the requirements there once (needs internet that first time only).
rem
rem  Keep the window that opens. It owns the tester's serial port.
rem  Closing it, or pressing Ctrl-C in it, sends STOP to the tester first.
rem ======================================================================
setlocal EnableExtensions
title EBC-A20 web interface
set "ROOT=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PY="
set "BASE="

if not exist "%ROOT%Python\ebc_framework\webui.py" goto :nofolder

rem 1) the private environment, if an earlier launch created it
call :try "%ROOT%.venv\Scripts\python.exe"

rem 2) an installed Python that already has pyserial
call :try py -3
call :try python
call :try "%USERPROFILE%\anaconda3\python.exe"
call :try "%USERPROFILE%\miniconda3\python.exe"
call :try "%USERPROFILE%\miniforge3\python.exe"
call :try "%LOCALAPPDATA%\anaconda3\python.exe"
call :try "%LOCALAPPDATA%\miniconda3\python.exe"
call :try "%ProgramData%\anaconda3\python.exe"
call :try "%ProgramData%\miniconda3\python.exe"
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try "%%D\python.exe"

if defined PY goto :run
if defined BASE goto :makevenv
goto :nopython


:makevenv
echo.
echo First start: setting up a private Python environment in .venv
echo It installs pyserial and matplotlib once. This needs internet.
echo.
%BASE% -m venv "%ROOT%.venv" || goto :setupfailed
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt" || goto :setupfailed
set PY="%ROOT%.venv\Scripts\python.exe"


:run
cd /d "%ROOT%Python\ebc_framework"
echo Using %PY%
echo.
%PY% webui.py %*
if errorlevel 1 pause
exit /b 0


rem ---- try one candidate: a working Python 3.10+, ideally with pyserial ----
:try
if defined PY exit /b 0
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 || exit /b 0
if not defined BASE set BASE=%*
%* -c "import serial" >nul 2>&1 || exit /b 0
set PY=%*
exit /b 0


:nofolder
echo This file has to stay in the EBC-20-Tester folder,
echo next to the Python\ebc_framework folder.
pause
exit /b 1

:nopython
echo.
echo No Python 3.10 or newer was found on this computer.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer.
echo Anaconda works too. Then double-click this file again.
echo.
pause
exit /b 1

:setupfailed
echo.
echo Setting up the environment failed. Check the internet connection,
echo delete the .venv folder next to this file, and try again.
echo.
pause
exit /b 1
