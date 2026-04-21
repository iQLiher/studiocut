#!/bin/bash
echo "Instalando dependencias..."
pip3 install -r requirements.txt
echo ""
echo "Iniciando servidor en http://localhost:5000"
(sleep 2 && xdg-open http://localhost:5000 2>/dev/null || open http://localhost:5000 2>/dev/null) &
python3 app.py
