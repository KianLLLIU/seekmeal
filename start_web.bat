@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"

call :find_python
if not defined PY_EXE goto no_deps

call :say "\u6b63\u5728\u542f\u52a8 SeekMeal \u672c\u5730\u670d\u52a1\u2026"
if not exist "%PROJECT_DIR%diet_assistant\api.py" (
  echo [warn] diet_assistant not found next to this file.
  echo [warn] keep this .bat inside the SeekMeal folder.
)
start "SeekMeal API" /min %PY_EXE% %PY_ARGS% -m uvicorn diet_assistant.api:app --host 127.0.0.1 --port 8000
call :say "\u6b63\u5728\u7b49\u5f85\u670d\u52a1\u5c31\u7eea\u2026"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8000"
call :say "\u6d4f\u89c8\u5668\u5df2\u6253\u5f00\uff0c\u5730\u5740 http://127.0.0.1:8000"
call :say "\u5173\u95ed\u6807\u9898\u4e3a\u300cSeekMeal API\u300d\u7684\u7a97\u53e3\u5373\u53ef\u505c\u6b62\u670d\u52a1\u3002"
endlocal
exit /b 0

:no_deps
call :say "\u672a\u627e\u5230\u5df2\u5b89\u88c5 fastapi / uvicorn \u7684 Python \u73af\u5883\u3002"
call :say "\u8bf7\u5728\u672c\u76ee\u5f55\u6267\u884c\uff1apython -m pip install -r requirements.txt"
call :say "\u6216\u76f4\u63a5\u4f7f\u7528\u7ebf\u4e0a\u7248\u672c\uff1ahttps://seekmeal.app.workbuddy.host/"
pause
endlocal
exit /b 1
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

