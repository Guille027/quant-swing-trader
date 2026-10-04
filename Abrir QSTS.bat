@echo off
rem Abre QSTS sin consola (alternativa al icono del escritorio).
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw -m qsts.app.launcher) || (start "" python -m qsts.app.launcher)
