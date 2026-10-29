@echo off
rem Starts Domo Cleanup. Uses the .venv that "Install Domo Cleanup.bat" created when it works,
rem otherwise py -3 or python. Each check names what failed and pauses.
rem The build converts this file to CRLF and refuses non-ASCII text.
cd /d "%~dp0app" 2>nul
if errorlevel 1 goto noapp
if not exist cleanup.py goto noapp
set "PYCMD="
rem An absolute path: Python 3.15 warns about sys.prefix when started through "..".
set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" "%VPY%" -c "import requests, yaml, jsonschema" >nul 2>nul && set "PYCMD="%VPY%""
if defined PYCMD goto launch
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD goto nopython
%PYCMD% -c "import sys; sys.exit(not sys.version_info[:2] >= (3, 10))" >nul 2>nul
if errorlevel 1 goto nopython
%PYCMD% -c "import requests, yaml, jsonschema" >nul 2>nul
if errorlevel 1 goto noinstall
:launch
%PYCMD% cleanup.py open
if errorlevel 1 pause
exit /b
:noapp
echo Cannot find the app folder next to this file.
echo This file is at: %~dp0
echo If you opened it from inside the zip, close it, right-click Domo-Cleanup.zip,
echo choose Extract All, and open it from the extracted domo-cleanup folder.
pause
exit /b 1
:nopython
echo Python 3.10 or newer was not found, or "python" is only the Microsoft Store stub.
echo Install Python from python.org, keep "Add python.exe to PATH" ticked, then
echo double-click "Install Domo Cleanup.bat" in the domo-cleanup folder.
pause
exit /b 1
:noinstall
echo Domo Cleanup is not installed yet.
echo Double-click "Install Domo Cleanup.bat" in the domo-cleanup folder, wait for
echo "install ok", then double-click this file again.
echo Python checked: %PYCMD%
pause
exit /b 1
