@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (set PY=python) else (set PY=py)
set /p TO=Test maili hangi adrese gitsin? 
%PY% server.py testmail %TO%
pause
