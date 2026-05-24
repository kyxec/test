@echo off
echo Starting WP Bot...
start "ngrok" C:\Users\Kyxec\Downloads\ngrok-v3-stable-windows-amd64\ngrok.exe http 8000
timeout /t 2 /nobreak >nul
C:\Users\Kyxec\AppData\Local\Programs\Python\Python310\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000

echo.
echo ✅ Всё запущено!
echo.
echo Открываем ngrok dashboard для получения URL...
timeout /t 2 /nobreak > nul
start http://localhost:4040
echo.
echo Скопируй HTTPS URL из ngrok и вставь его + /webhook в Meta Dashboard
pause
