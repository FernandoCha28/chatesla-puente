# -*- coding: utf-8 -*-
"""
PRUEBA RÁPIDA — ¿este servidor puede hablar con el bróker?

Úsalo ANTES de instalar nada más. En la terminal de cPanel:

    python probar.py

Si dice que sí a todo, el puente puede vivir en tu hosting y no
necesitas VPS. Si dice que no, tu hosting bloquea las salidas y
el puente tiene que estar en otro lado.
"""
import socket, ssl, sys

DESTINOS = [
    ("ws.trade.exnova.com", 443, "Exnova"),
    ("iqoption.com", 443, "IQ Option"),
    ("github.com", 443, "GitHub (para instalar las librerías)"),
]

print("Probando salidas de red desde este servidor…\n")
todo_bien = True

for host, puerto, nombre in DESTINOS:
    try:
        cru = socket.create_connection((host, puerto), timeout=8)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(cru, server_hostname=host):
            pass
        print("  ✅  %-42s  abierto" % (nombre + " (" + host + ")"))
    except Exception as e:
        todo_bien = False
        print("  ❌  %-42s  %s" % (nombre + " (" + host + ")", e))

print()
try:
    import websocket  # noqa: F401
    print("  ✅  librería websocket-client instalada")
except ImportError:
    print("  ⚠️   falta websocket-client (se instala con requirements.txt)")

print()
if todo_bien:
    print("RESULTADO: este servidor SÍ puede hablar con los brókeres.")
    print("Puedes montar el puente aquí mismo, sin VPS.")
else:
    print("RESULTADO: este hosting bloquea las salidas hacia el bróker.")
    print("El puente no puede vivir aquí; hay que ponerlo en otro servidor.")
    sys.exit(1)
