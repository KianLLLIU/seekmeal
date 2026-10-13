@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem Go to the project root (this file lives in the tools folder).
cd /d "%~dp0.."
set "PROJECT=%CD%"

rem Find any working Python 3 just to launch the setup engine.
rem The engine tools\setup_env.py then picks a proper base interpreter,
rem creates the venv and installs the dependencies.
set "BOOT_EXE="
set "BOOT_ARGS="

call :try_run "%PROJECT%\.venv\Scripts\python.exe"
if not defined BOOT_EXE call :try_run "%USERPROFILE%\.workbuddy\binaries\python\envs\seekmeal\Scripts\python.exe"
if not defined BOOT_EXE call :try_run "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined BOOT_EXE call :try_run "py" "-3"
if not defined BOOT_EXE call :try_run "python"
if not defined BOOT_EXE goto no_python

%BOOT_EXE% %BOOT_ARGS% "%PROJECT%\tools\setup_env.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" echo   [OK] Finished successfully.
if not "%RC%"=="0" echo   [FAIL] Something went wrong. Please read the messages above.
echo.
pause
endlocal & exit /b %RC%

:try_run
%1 %2 -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
set "BOOT_EXE=%1"
set "BOOT_ARGS=%2"
exit /b 0

:no_python
echo.
echo   [x] No Python 3 interpreter was found on this computer.
echo       Install Python 3.10 or later first:
echo           https://www.python.org/downloads/
echo       During setup, tick "Add python.exe to PATH".
echo.
echo   [x] See README.md section 1 for the step-by-step guide.
echo       https://www.python.org/downloads/
echo.
pause
endlocal
exit /b 1
