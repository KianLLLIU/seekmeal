@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"
call :find_python
if errorlevel 1 goto no_python

:menu
cls
"%PY_EXE%" %PY_ARGS% -c "print('==================================================\n            SeekMeal \u667a\u80fd\u996e\u98df\u5065\u5eb7\u52a9\u624b\n==================================================\n\n  1. \u547d\u4ee4\u884c\u6a21\u5f0f\n  2. \u542f\u52a8\u7f51\u9875\n  3. \u8fd0\u884c\u5355\u5143\u6d4b\u8bd5\n  4. \u6253\u5f00\u6280\u672f\u65b9\u6848\u6587\u6863\n  5. \u9000\u51fa\n')"
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
call :say "\u6b63\u5728\u542f\u52a8\u547d\u4ee4\u884c\u6a21\u5f0f..."
"%PY_EXE%" %PY_ARGS% -m diet_assistant.cli
call :say "\u547d\u4ee4\u884c\u6a21\u5f0f\u5df2\u7ed3\u675f\u3002"
pause
goto menu

:web
cls
call "%PROJECT_DIR%start_web.bat"
call :say "\u7f51\u9875\u542f\u52a8\u6d41\u7a0b\u5df2\u5b8c\u6210\u3002\u5982\u6d4f\u89c8\u5668\u672a\u81ea\u52a8\u6253\u5f00\uff0c\u8bf7\u8bbf\u95ee http://127.0.0.1:8000"
pause
goto menu

:tests
cls
call :say "\u6b63\u5728\u8fd0\u884c\u5168\u90e8\u5355\u5143\u6d4b\u8bd5..."
"%PY_EXE%" %PY_ARGS% -m unittest discover -s diet_assistant\tests -t .
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

:no_python
echo No Python 3 interpreter was found.
echo Install Python 3.10 or later, or restore the delivery interpreter.
pause
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

:end
endlocal
exit /b 0
