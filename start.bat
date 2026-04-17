@echo off
title StudioCut - Servidor
echo Instalando dependencias...
pip install -r requirements.txt
echo.
echo Iniciando servidor en http://localhost:5000
start http://localhost:5000
python app.py
pause
