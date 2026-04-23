@echo off
title StudioCut - Server
start http://localhost:5000
cd /d "%~dp0"
py -3.11 app.py
pause
