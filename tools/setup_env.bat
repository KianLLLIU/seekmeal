@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem Move to the project root (this file lives in tools\)
cd /d "%~dp0.."
set "PROJECT=%CD%"

rem --------------------------------------------------------------------
rem Find ANY working Python 3 just to run the setup engine.
rem The engine itself (tools\setup_env.py) picks the proper base
rem interpreter, creates the venv and installs the dependencies.
rem --------------------------------------------------------------------
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
if "%RC%"=="0" (
  %BOOT_EXE% %BOOT_ARGS% -c "print('  [OK] Setup finished. You can close this window and run start.bat.')"
) else (
  %BOOT_EXE% %BOOT_ARGS% -c "print('  [FAIL] Setup did not complete. See the messages above.')"
)
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
echo       Remember to tick "Add python.exe to PATH" during setup.
echo.
echo   [x] 未找到任何 Python 3 解释器，请先安装 Python 3.10 或更高版本。
echo       https://www.python.org/downloads/
echo.
pause
endlocal
exit /b 1
