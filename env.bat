@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PROJECT=%CD%"
set "SEEKMEAL_VENV=%USERPROFILE%\.workbuddy\binaries\python\envs\seekmeal"
if defined SEEKMEAL_ENV_DIR set "SEEKMEAL_VENV=%SEEKMEAL_ENV_DIR%"
set "VPY=%SEEKMEAL_VENV%\Scripts\python.exe"

if not exist "%VPY%" (
  echo.
  echo   [x] SeekMeal dedicated Python environment not found:
  echo       %SEEKMEAL_VENV%
  echo.
  echo       Run once to install it:  tools\setup_env.bat
  echo.
  pause
  endlocal & exit /b 1
)

rem ---- Double-clicked? Then relaunch inside a fresh console so it stays open ----
if /i "%~1"=="activate" goto activate
echo "%cmdcmdline%" | find /i "%~nx0" >nul
if not errorlevel 1 (
  start "SeekMeal Python" cmd /k ""%~f0" activate"
  endlocal & exit /b 0
)

:activate
call "%SEEKMEAL_VENV%\Scripts\activate.bat"
cd /d "%PROJECT%"
"%VPY%" -c "import sys,os;print();print('  SeekMeal \u4e13\u7528\u73af\u5883\u5df2\u6fc0\u6d3b');print('  \u89e3\u91ca\u5668 : ' + sys.version.split()[0]);print('  \u8def\u5f84   : ' + sys.executable);print('  \u76ee\u5f55   : ' + os.getcwd());print();print('  \u5e38\u7528\u547d\u4ee4:');print('    uvicorn diet_assistant.api:app --reload --port 8000');print('    python -m unittest discover -s diet_assistant/tests -v');print('    python -m diet_assistant.cli');print()"
endlocal & exit /b 0
