@echo off
chcp 65001 >nul
rem Сборка Gerber2DXF.exe (один файл, без консоли)
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --name Gerber2DXF ^
  --exclude-module matplotlib --exclude-module PIL --exclude-module PyQt5 ^
  --exclude-module PyQt6 --exclude-module PySide6 --exclude-module IPython ^
  --exclude-module pandas --exclude-module scipy --exclude-module pytest ^
  gerber2dxf.py || goto :error
echo.
echo Готово: dist\Gerber2DXF.exe
pause
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
