@echo off
setlocal
rem Installs Domo Cleanup into a private Python environment (.venv) next to app\.
rem Every exit pauses, so a double-click always leaves the reason on screen.
rem The build converts this file to CRLF and refuses non-ASCII text.
cd /d "%~dp0" 2>nul
set "STEP=Checking the app folder"
if not exist "app\cleanup.py" goto noapp
echo Installing Domo Cleanup in %CD%

set "STEP=Finding Python 3.10 or newer"
echo.
echo [1/5] %STEP%
set "PYCMD="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(not sys.version_info[:2] >= (3, 10))" >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && python -c "import sys; sys.exit(not sys.version_info[:2] >= (3, 10))" >nul 2>nul && set "PYCMD=python"
if not defined PYCMD goto nopython
%PYCMD% -c "import sys; print('Using Python ' + sys.version.split()[0] + ' at ' + sys.executable)"

set "STEP=Creating the private Python environment (.venv)"
echo.
echo [2/5] %STEP%
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" "%VPY%" -c "import sys; sys.exit(not sys.version_info[:2] >= (3, 10))" >nul 2>nul && goto venvready
%PYCMD% -m venv --clear .venv
if errorlevel 1 goto failed
:venvready
"%VPY%" -c "import sys; print('Environment Python ' + sys.version.split()[0])"
if errorlevel 1 goto failed

set "STEP=Upgrading pip"
echo.
echo [3/5] %STEP%
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto failed

set "STEP=Installing Domo Cleanup and its libraries"
echo.
echo [4/5] %STEP%
"%VPY%" -m pip install --prefer-binary --find-links .\app\wheels -e .\app
if errorlevel 1 goto failed

set "STEP=Checking the install"
echo.
echo [5/5] %STEP%
"%VPY%" -c "import requests, yaml, jsonschema, collector, shared; print('install ok')"
if errorlevel 1 goto failed
echo.
echo Domo Cleanup is installed. Double-click "Open Domo Cleanup.bat" to start it.
pause
exit /b 0

:noapp
echo Cannot find the app folder next to this file.
echo This file is at: %~dp0
echo If you opened it from inside the zip, close it, right-click Domo-Cleanup.zip,
echo choose Extract All, and open it from the extracted domo-cleanup folder.
pause
exit /b 1

:nopython
echo.
echo Install stopped at: %STEP%
echo Python 3.10 or newer was not found, or "python" is only the Microsoft Store stub.
echo Install Python from python.org, keep "Add python.exe to PATH" ticked, then
echo close this window and double-click this file again.
pause
exit /b 1

:failed
echo.
echo Install stopped at: %STEP%
echo Nothing outside this domo-cleanup folder was changed. Common causes:
echo  - No internet access to pypi.org. Ask IT whether a proxy is needed (HTTPS_PROXY).
echo  - Domo Cleanup is still open. Close its window, then run this file again.
echo If it keeps failing, send the text in this window to Dalton.
pause
exit /b 1
