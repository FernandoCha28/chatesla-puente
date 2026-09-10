# -*- coding: utf-8 -*-
"""
============================================================
CHATESLA VIP — PUENTE HACIA LOS BRÓKERES
============================================================
Este programa es el único que habla con Exnova y con IQ Option.
La página web (PHP) le pide las cosas y él responde.

Por qué existe:
  el navegador NO puede conectarse al bróker (usan WebSocket con
  firma propia y bloquean al navegador). Hace falta un programa
  corriendo en un servidor. Este es ese programa.

Dónde puede vivir:
  A) En tu mismo hosting, con cPanel → "Setup Python App"
     (no hace falta VPS). Ahí se usa passenger_wsgi.py.
  B) En un VPS, con:  gunicorn -b 127.0.0.1:8100 bridge:application

Seguridad:
  - todas las rutas exigen la cabecera  X-Token
  - el SSID llega desde PHP y nunca se guarda en disco aquí
============================================================
"""

import os, time, threading, logging
from flask import Flask, request, jsonify

# ------------------------------------------------------------
# AJUSTES  (en cPanel se ponen como "variables de entorno")
# ------------------------------------------------------------
TOKEN = os.environ.get("PUENTE_TOKEN", "").strip()
VIDA_CONEXION = int(os.environ.get("PUENTE_VIDA", "600"))   # segundos
ESPERA_BROKER = int(os.environ.get("PUENTE_ESPERA", "15"))  # tope al esperar al bróker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("puente")

application = Flask(__name__)     # cPanel busca este nombre
app = application                 # alias cómodo


# ------------------------------------------------------------
# CONEXIONES VIVAS
# Se guarda una por (bróker + ssid) para no reconectar en cada
# llamada: conectar tarda varios segundos.
# ------------------------------------------------------------
_conexiones = {}
_candado = threading.Lock()


class Aviso(Exception):
    """Error que se le puede enseñar al usuario tal cual."""
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.codigo = codigo


def revisar_token():
    if not TOKEN:
        raise Aviso("El puente no tiene token configurado", 500)
    if request.headers.get("X-Token", "") != TOKEN:
        raise Aviso("Token inválido", 401)


def datos():
    return request.get_json(silent=True) or {}


def _llave(broker, ssid):
    return broker + "|" + ssid[:40]


def _limpiar_viejas():
    ahora = time.time()
    for k in list(_conexiones.keys()):
        if ahora - _conexiones[k]["visto"] > VIDA_CONEXION:
            try:
                _conexiones[k]["api"].close()
            except Exception:
                pass
            _conexiones.pop(k, None)


# ------------------------------------------------------------
# CONEXIÓN CON SSID
#
# Las librerías (exnovaapi / iqoptionapi) son de la comunidad y
# cada versión guarda el SSID en un sitio distinto. Aquí se
# prueban las formas conocidas, de la más nueva a la más vieja,
# y si ninguna sirve se avisa con un mensaje claro.
# ------------------------------------------------------------
def _parchear_exnova():
    """
    La librería de Exnova espera la respuesta del bróker con un bucle sin
    freno (`while self.profile.msg == None: pass`). Si el bróker no contesta
    —por ejemplo con un SSID caducado— ese bucle no para nunca y se come el
    procesador entero. Aquí se le pone un límite de tiempo y un respiro.
    """
    from exnovaapi.api import ExnovaAPI
    import exnovaapi.global_value as gv
    if getattr(ExnovaAPI, "_adaptado", False):
        return

    def send_ssid(self, limite=ESPERA_BROKER):
        self.profile.msg = None
        self.ssid(gv.SSID)                     # pylint: disable=not-callable
        fin = time.time() + limite
        while self.profile.msg is None:
            if time.time() > fin:
                return False
            time.sleep(0.05)
        return self.profile.msg is not False

    ExnovaAPI.send_ssid = send_ssid
    ExnovaAPI._adaptado = True
    log.info("exnovaapi: espera del bróker limitada a %ss", ESPERA_BROKER)


def _parchear_iqoption():
    """
    La librería de IQ Option está escrita para websocket-client 0.56, que
    llamaba a las funciones con menos parámetros. Nosotros usamos la 1.8.0
    (la que necesita Exnova), así que aquí se adaptan las llamadas.
    """
    from iqoptionapi.ws import client as c
    K = c.WebsocketClient
    if getattr(K, "_adaptado", False):
        return
    orig = {n: getattr(K, n) for n in ("on_message", "on_error", "on_open", "on_close")}

    def on_message(self, *a):
        return orig["on_message"](self, a[-1]) if a else None

    def on_error(self, *a):
        try:
            return orig["on_error"](self, a[-1]) if a else None
        except Exception:
            return None

    def on_open(self, *a):
        try:
            return orig["on_open"](self)
        except Exception:
            return None

    def on_close(self, *a):
        try:
            return orig["on_close"](self)
        except Exception:
            return None

    K.on_message, K.on_error = on_message, on_error
    K.on_open, K.on_close = on_open, on_close
    K._adaptado = True
    log.info("iqoptionapi adaptado a websocket-client 1.x")


def _abrir(broker, ssid):
    if broker == "iqoption":
        try:
            from iqoptionapi.stable_api import IQ_Option as Cliente
            import iqoptionapi.global_value as gv
            _parchear_iqoption()
        except ImportError as e:
            raise Aviso("La librería de IQ Option no está bien instalada en el "
                        "servidor (%s). Revisa el Build Command." % e, 500)
    else:
        try:
            from exnovaapi.stable_api import Exnova as Cliente
            import exnovaapi.global_value as gv
        except ImportError as e:
            raise Aviso("La librería de Exnova no está bien instalada en el "
                        "servidor (%s). Revisa el Build Command." % e, 500)
        _parchear_exnova()

    api = Cliente("sesion@chatesla", "sesion")   # no se usa: entramos con SSID

    try:
        gv.SSID = ssid
    except Exception:
        pass

    if hasattr(api, "set_session"):
        try:
            api.set_session({"User-Agent": "Mozilla/5.0"}, {"ssid": ssid})
        except Exception:
            pass

    ok = False
    for nombre in ("connect_with_ssid", "connect_ssid", "login_with_ssid"):
        if hasattr(api, nombre):
            try:
                r = getattr(api, nombre)(ssid)
                ok = bool(r[0] if isinstance(r, tuple) else r)
                break
            except Exception as e:
                log.warning("%s falló: %s", nombre, e)

    if not ok:
        try:
            r = api.connect()
            ok = bool(r[0] if isinstance(r, tuple) else r)
        except Exception as e:
            log.warning("connect() falló: %s", e)

    if not ok and hasattr(api, "api"):
        try:
            api.api.start_websocket()
            time.sleep(2)
            ok = api.check_connect()
        except Exception as e:
            log.warning("socket manual falló: %s", e)

    if not ok:
        raise Aviso("No se pudo entrar con ese SSID. Suele pasar cuando el SSID "
                    "caducó: vuelve a copiarlo desde el bróker en Configuración.")

    log.info("conectado a %s", broker)
    return api


def _abrir_vigilado(broker, ssid, limite=None):
    """
    Abre la conexión en un hilo aparte con tope de tiempo. Si la librería se
    queda esperando al bróker, no arrastra al puente entero: se responde con
    un aviso y el hilo se abandona.
    """
    limite = limite or (ESPERA_BROKER + 8)
    caja = {}

    def tarea():
        try:
            caja["api"] = _abrir(broker, ssid)
        except Aviso as e:
            caja["aviso"] = e
        except Exception as e:                       # noqa: BLE001
            caja["aviso"] = Aviso("El bróker falló al conectar: %s" % e, 500)

    h = threading.Thread(target=tarea, daemon=True)
    h.start()
    h.join(limite)
    if h.is_alive():
        raise Aviso("El bróker no contestó a tiempo. Suele ser el SSID caducado: "
                    "cópialo otra vez desde el bróker en Configuración.", 504)
    if "aviso" in caja:
        raise caja["aviso"]
    return caja["api"]


def cliente(broker, ssid, cuenta):
    if not ssid:
        raise Aviso("Falta el SSID")
    with _candado:
        _limpiar_viejas()
        k = _llave(broker, ssid)
        reg = _conexiones.get(k)
        if reg:
            try:
                vivo = reg["api"].check_connect()
            except Exception:
                vivo = False
            if not vivo:
                _conexiones.pop(k, None)
                reg = None
        if not reg:
            reg = {"api": _abrir_vigilado(broker, ssid), "cuenta": None}
            _conexiones[k] = reg
        reg["visto"] = time.time()

        quiere = "REAL" if cuenta == "real" else "PRACTICE"
        if reg["cuenta"] != quiere:
            try:
                reg["api"].change_balance(quiere)
                reg["cuenta"] = quiere
            except Exception as e:
                log.warning("no se pudo cambiar a %s: %s", quiere, e)
        return reg["api"]


def preparar():
    """Token + datos de la petición, todo de una vez."""
    revisar_token()
    d = datos()
    broker = d.get("broker") if d.get("broker") in ("exnova", "iqoption") else "exnova"
    cuenta = "real" if d.get("cuenta") == "real" else "demo"
    return d, broker, cuenta, d.get("ssid", "")


VENCIMIENTOS = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60}


@application.errorhandler(Aviso)
def _aviso(e):
    return jsonify({"error": e.mensaje}), e.codigo


# ------------------------------------------------------------
# RUTAS
# ------------------------------------------------------------
@application.get("/")
def raiz():
    return jsonify({"puente": "Chatesla VIP", "estado": "en pie",
                    "token": "puesto" if TOKEN else "FALTA"})


@application.get("/diagnostico")
def diagnostico():
    """
    Abre esto en el navegador para ver, de un vistazo, si las librerías
    de los brókeres quedaron bien instaladas:
        https://tu-puente.onrender.com/diagnostico
    No hace falta token: no enseña ningún dato privado.
    """
    salida = {"puente": "en pie", "token": "puesto" if TOKEN else "FALTA", "librerias": {}}
    try:
        import websocket
        salida["websocket_client"] = getattr(websocket, "__version__", "desconocida")
    except Exception as e:
        salida["websocket_client"] = "NO INSTALADA (%s)" % e

    for nombre, ruta in (("exnova", "exnovaapi.stable_api"),
                         ("iqoption", "iqoptionapi.stable_api")):
        try:
            __import__(ruta)
            salida["librerias"][nombre] = "ok"
        except Exception as e:
            salida["librerias"][nombre] = "%s: %s" % (type(e).__name__, e)

    # el fallo típico de Exnova: la carpeta http se queda fuera al instalar
    try:
        import exnovaapi.http  # noqa: F401
        salida["exnovaapi_http"] = "ok"
    except Exception as e:
        salida["exnovaapi_http"] = ("FALTA — el Build Command no le puso el __init__.py "
                                    "(%s)" % e)

    salida["todo_bien"] = (salida["librerias"].get("exnova") == "ok"
                           and salida.get("exnovaapi_http") == "ok"
                           and TOKEN != "")
    return jsonify(salida)


@application.post("/estado")
def estado():
    d, broker, cuenta, ssid = preparar()
    api = cliente(broker, ssid, cuenta)
    try:
        return jsonify({"ok": True, "conectado": bool(api.check_connect()),
                        "saldo": float(api.get_balance() or 0)})
    except Exception as e:
        raise Aviso("El bróker no respondió: %s" % e, 500)


def _perfil(api):
    try:
        pr = api.get_profile_ansyc() if hasattr(api, "get_profile_ansyc") else {}
        return {"id": pr.get("user_id") or pr.get("id"),
                "nombre": pr.get("name") or pr.get("nickname") or ""}
    except Exception:
        return {}


@application.post("/saldo")
def saldo():
    d, broker, cuenta, ssid = preparar()
    api = cliente(broker, ssid, cuenta)
    try:
        return jsonify({"ok": True,
                        "saldo": float(api.get_balance() or 0),
                        "cuenta": cuenta,
                        "moneda": (api.get_currency() if hasattr(api, "get_currency") else "USD"),
                        "perfil": _perfil(api)})
    except Exception as e:
        raise Aviso("No se pudo leer el saldo: %s" % e, 500)


@application.post("/historial")
def historial():
    """
    Operaciones cerradas de la cuenta, ya con la forma que usa la web:
      id, hora, par, direccion, entrada, resultado (WIN/LOSS/DRAW), lucro
    """
    d, broker, cuenta, ssid = preparar()
    api = cliente(broker, ssid, cuenta)
    crudo = []
    try:
        if hasattr(api, "get_optioninfo_v2"):
            r = api.get_optioninfo_v2(100)
            crudo = list((r or {}).get("msg", {}).get("closed_options", []))
        if not crudo and hasattr(api, "get_position_history_v2"):
            for tipo in ("digital-option", "turbo-option", "binary-option"):
                try:
                    _, arr = api.get_position_history_v2(tipo, 100, 0, 0, 0)
                    crudo += list(arr or [])
                except Exception:
                    pass
    except Exception as e:
        raise Aviso("No se pudo leer el historial: %s" % e, 500)

    ops = []
    for o in crudo:
        try:
            monto = float(o.get("amount") or o.get("invest") or 0)
            gan = o.get("win_amount")
            if gan is None:
                gan = o.get("close_profit")
            gan = float(gan or 0)
            lucro = round(gan - monto, 2) if gan else -monto
            res = "DRAW" if abs(lucro) < 0.01 else ("WIN" if lucro > 0 else "LOSS")
            cierre = o.get("exp_time") or o.get("close_time") or o.get("created_at") or 0
            cierre = float(cierre)
            if cierre > 10_000_000_000:          # viene en milisegundos
                cierre = cierre / 1000
            ops.append({
                "id": str(o.get("id") or o.get("external_id") or ""),
                "hora": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cierre)) if cierre else "",
                "par": (o.get("active") or o.get("instrument_id") or o.get("active_id") or ""),
                "direccion": (o.get("direction") or o.get("instrument_dir") or ""),
                "entrada": round(monto, 2),
                "resultado": res,
                "lucro": lucro,
            })
        except Exception:
            continue

    desde = d.get("desde")
    if desde:
        ops = [o for o in ops if o["hora"][:10] >= desde]
    ops.sort(key=lambda o: o["hora"])
    return jsonify({"ok": True, "operaciones": ops, "total": len(ops)})


@application.post("/operar")
def operar():
    """Abre una operación de verdad en el bróker."""
    d, broker, cuenta, ssid = preparar()
    par = d.get("par")
    monto = float(d.get("monto") or 0)
    if not par or monto <= 0:
        raise Aviso("Faltan el par o el monto")
    api = cliente(broker, ssid, cuenta)
    minutos = VENCIMIENTOS.get(str(d.get("vencimiento") or "M1").upper(), 1)
    lado = "put" if str(d.get("direccion") or "call").lower() == "put" else "call"
    try:
        ok, ident = api.buy(monto, par, lado, minutos)
    except Exception as e:
        raise Aviso("El bróker rechazó la orden: %s" % e, 500)
    if not ok:
        raise Aviso("El bróker rechazó la orden: %s" % ident)
    return jsonify({"ok": True, "orden": str(ident), "par": par,
                    "direccion": lado, "monto": monto,
                    "vencimiento": d.get("vencimiento") or "M1"})


@application.post("/cerrar")
def cerrar():
    """Suelta la conexión guardada (útil al cambiar de SSID)."""
    d, broker, cuenta, ssid = preparar()
    with _candado:
        reg = _conexiones.pop(_llave(broker, ssid), None)
    if reg:
        try:
            reg["api"].close()
        except Exception:
            pass
    return jsonify({"ok": True})


# arranque suelto, para probar en tu computadora o en un VPS
if __name__ == "__main__":
    application.run(host="127.0.0.1", port=8100)

