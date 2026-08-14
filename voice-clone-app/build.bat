@echo off
rem Build VoiceCloneApp.exe (single-file, windowed) with PyInstaller.
rem Run from Windows:  build.bat
rem Requires: pyinstaller installed in ..\.venv (pip install pyinstaller)
cd /d %~dp0
..\.venv\Scripts\python.exe -m PyInstaller ^
  --noconfirm --clean --onefile --windowed ^
  --name VoiceCloneApp ^
  --icon ui\app-icon.ico ^
  --add-data "profiles;profiles" ^
  --add-data ".env.example;." ^
  --add-data "ui\app-icon.ico;ui" ^
  --collect-all sounddevice ^
  main.py
echo.
echo Done. Output: dist\VoiceCloneApp.exe
