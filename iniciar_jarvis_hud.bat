@echo off
title J.A.R.V.I.S. - Servidor Neural + HUD
cd /d "%~dp0"
"C:\Users\monte\AppData\Local\Programs\Python\Python312\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 8000
pause
