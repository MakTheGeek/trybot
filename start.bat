@echo off
title SolSnipBot
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo [SolSnipBot] Creation de l'environnement Python...
  python -m venv venv
  venv\Scripts\python.exe -m pip install -r requirements.txt
)
echo [SolSnipBot] Demarrage du serveur...
venv\Scripts\python.exe run.py --host 127.0.0.1 --port 8787
pause
