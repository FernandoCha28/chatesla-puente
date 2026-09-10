# -*- coding: utf-8 -*-
"""
Puerta de entrada para cPanel → "Setup Python App".
En la casilla "Archivo de inicio de la aplicación" pon:  passenger_wsgi.py
En la casilla "Punto de entrada":                        application
No hay que tocar nada aquí dentro.
"""
from bridge import application   # noqa: F401
