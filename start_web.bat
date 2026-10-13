@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
call :find_python
if errorlevel 1 goto no_python

"%PY_EXE%" %PY_ARGS% -c "import uvicorn" >nul 2>&1
if errorlevel 1 goto no_uvicorn

call :say "\u6b63\u5728\u542f\u52a8 SeekMeal \u672c\u5730 API..."
start "SeekMeal API" /min "%PY_EXE%" %PY_ARGS% -m uvicorn diet_assistant.api:app --host 127.0.0.1 --port 8000
call :say "\u6b63\u5728\u7b49\u5f85\u670d\u52a1\u5c31\u7eea..."
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8000"
call :say "\u6d4f\u89c8\u5668\u5df2\u6253\u5f00\u3002API \u5730\u5740\uff1ahttp://127.0.0.1:8000"
endlocal
exit /b 0

:no_uvicorn
call :say "\u65e0\u6cd5\u542f\u52a8\u7f51\u9875\uff1a\u5f53\u524d Python \u73af\u5883\u672a\u5b89\u88c5 uvicorn\u3002"
call :say "\u8bf7\u5728\u9879\u76ee\u76ee\u5f55\u6267\u884c\uff1apython -m pip install -r requirements.txt"
pause
endlocal
exit /b 1

:no_python
echo No Python 3 interpreter was found.
echo Install Python 3.10 or later, or restore the delivery interpreter.
pause
endlocal
exit /b 1

:say
"%PY_EXE%" %PY_ARGS% -c "print('%~1')"
exit /b 0

:find_python
where py >nul 2>&1
if not errorlevel 1 (
  py -3 --version >nul 2>&1
  if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    exit /b 0
  )
)
where python >nul 2>&1
if not errorlevel 1 (
  python --version >nul 2>&1
  if not errorlevel 1 (
    set "PY_EXE=python"
    set "PY_ARGS="
    exit /b 0
  )
)
if exist "C:\Users\Kevin L\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PY_EXE=C:\Users\Kevin L\.workbuddy\binaries\python\versions\3.13.12\python.exe"
  set "PY_ARGS="
  exit /b 0
)
exit /b 1
