@echo off
rem Programa QSTS para que se ejecute sola cada noche a las 23:00 (despierta el PC si esta en reposo).
rem Descarga los precios, revisa las senales del cierre y deja las ordenes en Alpaca para la apertura.
cd /d "%~dp0"
for /f "delims=" %%p in ('python -c "import sys,pathlib;print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"') do set PYW=%%p
if not exist "%PYW%" set PYW=pythonw
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=New-ScheduledTaskAction -Execute '%PYW%' -Argument '-m qsts.cli run-once' -WorkingDirectory '%cd%'; $t=New-ScheduledTaskTrigger -Daily -At 23:00; $s=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2); Register-ScheduledTask -TaskName 'QSTS cada noche' -Action $a -Trigger $t -Settings $s -Force | Out-Null"
if errorlevel 1 (
  echo.
  echo *** NO SE PUDO PROGRAMAR. Copia el texto de esta ventana y pasaselo a Claude. ***
  pause
  exit /b 1
)
echo.
echo Listo: QSTS se ejecutara sola cada noche a las 23:00 (aunque el PC este en reposo).
echo Lo que hace queda apuntado en var\nightly.log y te avisa por Telegram de cada orden.
echo Para quitarlo: Programador de tareas de Windows - tarea "QSTS cada noche" - Eliminar.
pause
