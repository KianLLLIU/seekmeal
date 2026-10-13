@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"

call :find_python
if not defined BOOT_EXE goto no_python

:menu
cls
%BOOT_EXE% %BOOT_ARGS% -c "print('==================================================\n            SeekMeal \u667a\u80fd\u996e\u98df\u5065\u5eb7\u52a9\u624b\n==================================================\n\n  1. \u547d\u4ee4\u884c\u6a21\u5f0f\n  2. \u542f\u52a8\u7f51\u9875\n  3. \u8fd0\u884c\u5355\u5143\u6d4b\u8bd5\n  4. \u6253\u5f00\u6280\u672f\u65b9\u6848\u6587\u6863\n  5. \u9000\u51fa\n\n  \u5f53\u524d\u670d\u52a1\u89e3\u91ca\u5668\uff1a %PY_EXE%\n')"
set "CHOICE="
set /p "CHOICE=Select [1-5]: "
if "%CHOICE%"=="1" goto cli
if "%CHOICE%"=="2" goto web
if "%CHOICE%"=="3" goto tests
if "%CHOICE%"=="4" goto docs
if "%CHOICE%"=="5" goto end
call :say "\u8f93\u5165\u65e0\u6548\uff0c\u8bf7\u8f93\u5165 1 \u5230 5\u3002"
pause
goto menu

:cli
cls
call :say "\u6b63\u5728\u542f\u52a8\u547d\u4ee4\u884c\u6a21\u5f0f\u2026"
%BOOT_EXE% %BOOT_ARGS% -m diet_assistant.cli
call :say "\u547d\u4ee4\u884c\u6a21\u5f0f\u5df2\u7ed3\u675f\u3002"
pause
goto menu

:web
cls
if not defined PY_EXE (
  call :say "\u672a\u627e\u5230\u5df2\u5b89\u88c5 fastapi / uvicorn \u7684 Python \u73af\u5883\u3002"
  call :say "\u8bf7\u5728\u672c\u76ee\u5f55\u6267\u884c\uff1apython -m pip install -r requirements.txt"
  call :say "\u6216\u76f4\u63a5\u4f7f\u7528\u7ebf\u4e0a\u7248\u672c\uff1ahttps://seekmeal.app.workbuddy.host/"
  pause
  goto menu
)
call "%PROJECT_DIR%start_web.bat"
call :say "\u7f51\u9875\u542f\u52a8\u6d41\u7a0b\u5df2\u5b8c\u6210\u3002\u82e5\u6d4f\u89c8\u5668\u672a\u81ea\u52a8\u6253\u5f00\uff0c\u8bf7\u8bbf\u95ee http://127.0.0.1:8000"
pause
goto menu

:tests
cls
call :say "\u6b63\u5728\u8fd0\u884c\u5168\u90e8\u5355\u5143\u6d4b\u8bd5\u2026"
%BOOT_EXE% %BOOT_ARGS% -m unittest discover -s diet_assistant\tests -t .
if errorlevel 1 (
  call :say "\u5355\u5143\u6d4b\u8bd5\u672a\u5168\u90e8\u901a\u8fc7\uff0c\u8bf7\u67e5\u770b\u4e0a\u65b9\u8f93\u51fa\u3002"
) else (
  call :say "\u5355\u5143\u6d4b\u8bd5\u5df2\u5168\u90e8\u901a\u8fc7\u3002"
)
pause
goto menu

:docs
cls
set "DOC_FOUND="
for %%F in ("%PROJECT_DIR%docs\*.html") do (
  if exist "%%~fF" (
    start "" "%%~fF"
    set "DOC_FOUND=1"
  )
)
if defined DOC_FOUND (
  call :say "\u5df2\u7528\u9ed8\u8ba4\u6d4f\u89c8\u5668\u6253\u5f00\u6280\u672f\u65b9\u6848\u6587\u6863\u3002"
) else (
  call :say "\u672a\u5728 docs \u76ee\u5f55\u627e\u5230 HTML \u6280\u672f\u65b9\u6848\u6587\u6863\u3002"
)
pause
goto menu
:say
%BOOT_EXE% %BOOT_ARGS% -c "print('%~1')"
exit /b 0
:find_python
rem Two-stage detection:
rem   BOOT_EXE / BOOT_ARGS -> any working Python (used for echo, CLI, unittest)
rem   PY_EXE  / PY_ARGS    -> a Python that can really import uvicorn + fastapi
rem Every candidate is accepted only after a real import test, so a bare
rem python.exe sitting on PATH can never be picked just because it exists.
set "PY_EXE="
set "PY_ARGS="
set "BOOT_EXE="
set "BOOT_ARGS="

call :try_run "%PROJECT_DIR%.venv\Scripts\python.exe"
if not defined BOOT_EXE call :try_run "%USERPROFILE%\.workbuddy\binaries\python\envs\seekmeal\Scripts\python.exe"
if not defined BOOT_EXE call :try_run "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined BOOT_EXE call :try_run "py" "-3"
if not defined BOOT_EXE call :try_run "python"
if not defined BOOT_EXE goto no_python

call :try_deps "%PROJECT_DIR%.venv\Scripts\python.exe"
if defined PY_EXE exit /b 0
call :try_deps "%PROJECT_DIR%..\.venv\Scripts\python.exe"
if defined PY_EXE exit /b 0
call :try_deps "%USERPROFILE%\.workbuddy\binaries\python\envs\seekmeal\Scripts\python.exe"
if defined PY_EXE exit /b 0
call :try_deps "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if defined PY_EXE exit /b 0
call :try_deps "py" "-3"
if defined PY_EXE exit /b 0
call :try_deps "python"
if defined PY_EXE exit /b 0

rem No interpreter has the web deps. PY_EXE stays undefined on purpose so the
rem caller can print a clear hint instead of dumping a uvicorn traceback.
exit /b 0

:try_run
%1 %2 -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
set "BOOT_EXE=%1"
set "BOOT_ARGS=%2"
exit /b 0

:try_deps
%1 %2 -c "import uvicorn,fastapi" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY_EXE=%1"
set "PY_ARGS=%2"
exit /b 0
:no_python
echo No Python 3 interpreter was found.
echo Install Python 3.10 or later, then run this file again.
echo.
pause
endlocal
exit /b 1
:end
endlocal
exit /b 0

