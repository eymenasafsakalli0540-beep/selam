@echo off
cd /d "%~dp0"
if not exist server.py (
  echo HATA: Bu dosyayi server.py ile AYNI klasore koy, sonra tekrar calistir.
  pause
  exit /b 1
)
(
echo SMTP_HOST=smtp.gmail.com
echo SMTP_PORT=587
echo SMTP_USER=eymenasafsakalli0540@gmail.com
echo SMTP_PASS=wyxlpfhiulbocfmu
echo MAIL_FROM=Selam ^<eymenasafsakalli0540@gmail.com^>
) > .env
echo .env dosyasi olusturuldu. Test maili gonderiliyor...
where python >nul 2>nul
if %errorlevel%==0 (set PY=python) else (set PY=py)
%PY% -m pip install -q -r requirements.txt
%PY% server.py testmail eymenasafsakalli0540@gmail.com
pause
