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
set "VPY=%ROOT%.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PY="
set "BASE="

if not exist "%ROOT%Python\ebc_framework\webui.py" goto :nofolder

rem 1) the private environment from an earlier launch, if it is complete.
rem    It is never used as the BASE for building a new one: if it is
rem    broken, it gets finished or rebuilt below from a real Python.
call :tryvenv

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
call :venvusable
if defined VENV_OK goto :finishvenv
if defined BASE goto :makevenv
goto :nopython


:finishvenv
echo.
echo Finishing the private Python environment in .venv
echo (an earlier setup did not complete). This needs internet.
echo.
goto :pipinstall

:makevenv
if exist "%ROOT%.venv" rmdir /s /q "%ROOT%.venv"
echo.
echo First start: setting up a private Python environment in .venv
echo using %BASE%
echo It installs pyserial and matplotlib once. This needs internet.
echo.
%BASE% -m venv "%ROOT%.venv" || goto :setupfailed

:pipinstall
"%VPY%" -m pip install --upgrade pip >nul 2>&1
"%VPY%" -m pip install -r "%ROOT%requirements.txt" || goto :setupfailed
set PY="%VPY%"


:run
cd /d "%ROOT%Python\ebc_framework"
echo Using %PY%
echo.
%PY% webui.py %*
if errorlevel 1 pause
exit /b 0


rem ---- the private environment, only if it already has pyserial ----
:tryvenv
if not exist "%VPY%" exit /b 0
"%VPY%" -c "import serial" >nul 2>&1 || exit /b 0
set PY="%VPY%"
exit /b 0

rem ---- does a leftover private environment at least run? ----
:venvusable
set "VENV_OK="
if not exist "%VPY%" exit /b 0
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "VENV_OK=1"
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
echo Setting up the environment failed. Check the internet connection
echo and double-click this file again; it picks up where it stopped.
echo.
pause
exit /b 1
