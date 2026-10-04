@echo off
rem Crea el icono "QSTS" en el escritorio y en el menu Inicio. Solo hace falta una vez.
cd /d "%~dp0"
python -m qsts.app.shortcut
echo.
pause
