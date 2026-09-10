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

import os, ssl, time, threading, logging
from flask import Flask, request, jsonify

# ------------------------------------------------------------
# AJUSTES  (en cPanel se ponen como "variables de entorno")
# ------------------------------------------------------------
TOKEN = os.environ.get("PUENTE_TOKEN", "").strip()
VIDA_CONEXION = int(os.environ.get("PUENTE_VIDA", "600"))   # segundos
ESPERA_BROKER = int(os.environ.get("PUENTE_ESPERA", "8"))   # tope de cada espera al bróker
ESPERA_TOTAL  = int(os.environ.get("PUENTE_TOTAL", "22"))  # tope de toda la conexión

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
    La librería de Exnova espera al bróker con bucles como este:

        while self.profile.msg == None:
            pass

    Cuatro veces, sin pausa y sin límite de tiempo. Un bucle así gira a toda
    velocidad y no suelta el intérprete de Python, así que el programa entero
    se queda mudo: ni siquiera puede contestarle a la página. En un servidor
    pequeño (como el plan gratis) eso lo tumba.

    Aquí se cambian esos cuatro bucles por otros que descansan 50 milisegundos
    entre vuelta y vuelta y se rinden pasado un tiempo. Es el mismo camino de
    siempre, solo que respirando.
    """
    from exnovaapi.api import ExnovaAPI
    from exnovaapi.stable_api import Exnova
    from exnovaapi.ws.client import WebsocketClient
    import exnovaapi.global_value as gv
    import requests

    if getattr(ExnovaAPI, "_adaptado", False):
        return

    def esperar(mirar, limite):
        """Repite `mirar()` hasta que dé algo, o hasta que se acabe el tiempo."""
        fin = time.time() + limite
        while time.time() < fin:
            try:
                r = mirar()
                if r is not None:
                    return r
            except Exception:
                pass
            time.sleep(0.05)
        return None

    # 1) abrir el socket
    def start_websocket(self):
        gv.check_websocket_if_connect = None
        gv.check_websocket_if_error = False
        gv.websocket_error_reason = None

        self.websocket_client = WebsocketClient(self)
        self.websocket_thread = threading.Thread(
            target=self.websocket.run_forever,
            kwargs={"sslopt": {"check_hostname": False, "cert_reqs": ssl.CERT_NONE}})
        self.websocket_thread.daemon = True
        self.websocket_thread.start()

        def estado():
            if gv.check_websocket_if_error:
                return (False, gv.websocket_error_reason or "Error de conexión")
            if gv.check_websocket_if_connect == 0:
                return (False, "El bróker cerró la conexión")
            if gv.check_websocket_if_connect == 1:
                return (True, None)
            return None

        return esperar(estado, ESPERA_BROKER) or (False, "El bróker no abrió la conexión a tiempo")

    # 2) presentar el SSID
    def send_ssid(self, limite=None):
        self.profile.msg = None
        self.ssid(gv.SSID)                     # pylint: disable=not-callable
        r = esperar(lambda: self.profile.msg, limite or ESPERA_BROKER)
        return r is not None and r is not False

    # 3) la conexión de bajo nivel, solo por SSID (sin usuario ni contraseña)
    def connect(self):
        gv.ssl_Mutual_exclusion = False
        gv.ssl_Mutual_exclusion_write = False
        try:
            self.close()
        except Exception:
            pass

        ok, razon = self.start_websocket()
        if not ok:
            return False, razon
        if not gv.SSID:
            return False, "Falta el SSID"
        if not self.send_ssid():
            return False, ("El bróker no aceptó el SSID. Casi siempre es que caducó: "
                           "cópialo otra vez desde el bróker en Configuración.")

        requests.utils.add_dict_to_cookiejar(self.session.cookies, {"ssid": gv.SSID})

        # Ojo: leer `timesync.server_timestamp` de la forma normal también se
        # queda colgado, porque esa propiedad espera por dentro. Se mira el
        # valor guardado directamente, así la espera sí tiene tope.
        self.timesync.server_timestamp = None
        ts = self.timesync
        if esperar(lambda: getattr(ts, "_TimeSync__server_timestamp", None), ESPERA_BROKER) is None:
            return False, "El bróker no envió la hora del servidor"
        return True, None

    # 4) la conexión de alto nivel
    def conectar(self, sms_code=None):
        try:
            self.api.close()
        except Exception:
            pass
        self.api = ExnovaAPI("ws.trade.exnova.com", self.email, self.password)
        self.api.set_session(headers=self.SESSION_HEADER, cookies=self.SESSION_COOKIE)

        ok, razon = self.api.connect()
        if not ok:
            return False, razon

        try:
            self.re_subscribe_stream()
        except Exception:
            pass

        # el número de la cuenta llega por el socket; si tarda, seguimos igual
        if esperar(lambda: gv.balance_id, ESPERA_BROKER) is not None:
            try:
                self.position_change_all("subscribeMessage", gv.balance_id)
            except Exception:
                pass
        try:
            self.order_changed_all("subscribeMessage")
            self.api.setOptions(1, True)
        except Exception:
            pass
        return True, None

    ExnovaAPI.start_websocket = start_websocket
    ExnovaAPI.send_ssid = send_ssid
    ExnovaAPI.connect = connect
    Exnova.connect = conectar
    ExnovaAPI._adaptado = True
    log.info("exnovaapi adaptado: bucles con pausa y tope de %ss", ESPERA_BROKER)


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

    ok, razon = False, None
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
            if isinstance(r, tuple):
                ok, razon = bool(r[0]), r[1]
            else:
                ok = bool(r)
        except Exception as e:
            razon = str(e)
            log.warning("connect() falló: %s", e)

    if not ok:
        raise Aviso(str(razon) if razon else
                    "No se pudo entrar con ese SSID. Suele pasar cuando el SSID "
                    "caducó: vuelve a copiarlo desde el bróker en Configuración.")

    log.info("conectado a %s", broker)
    return api


def _abrir_vigilado(broker, ssid, limite=None):
    """
    Abre la conexión en un hilo aparte con tope de tiempo. Si la librería se
    queda esperando al bróker, no arrastra al puente entero: se responde con
    un aviso y el hilo se abandona.
    """
    limite = limite or ESPERA_TOTAL
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
