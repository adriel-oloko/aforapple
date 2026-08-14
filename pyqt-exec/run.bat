@echo off
setlocal

set "VENV_DIR=d:\video-stuff\.venv"
set "APP=d:\video-stuff\pyqt-exec\main.py"

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Could not find venv at "%VENV_DIR%".
    echo Create it first with: python -m venv "%VENV_DIR%"
    pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"

"%VENV_DIR%\Scripts\python.exe" "%APP%"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo App exited with code %EXIT_CODE%.
    pause
)

endlocal
