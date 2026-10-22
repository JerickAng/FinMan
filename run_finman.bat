@echo off
cd /d "C:\Users\Jeric\Downloads\FinMan_Starter\finman"

start "ngrok tunnel" cmd /k ngrok http --url=shortness-easiness-multitude.ngrok-free.dev 5000
timeout /t 3 /nobreak >nul
start "FinMan Flask" cmd /k ".venv\Scripts\activate && python app.py"
start http://localhost:5000