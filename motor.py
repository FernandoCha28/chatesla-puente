# -*- coding: utf-8 -*-
"""
============================================================
CHATESLA VIP — MOTOR AUTOMÁTICO (bot de señales y copy trading)
============================================================
Vive dentro del puente. La web (PHP) lo llama cada minuto desde
una tarea programada (cron) del hosting con dos pedidos:

  /motor/resultados  -> "dame lo que terminó" (y confirma lo ya guardado)
  /motor/encargar    -> "estas son las señales de los próximos minutos
                         y esta es la lista de quién copia a quién"

El motor:
  - se conecta al bróker de cada usuario un poco antes de la señal,
  - manda la orden en el segundo justo,
  - si pierde y hay gale, entra otra vez en la vela siguiente,
  - guarda el resultado hasta que la web lo recoge.

Copy trading: mantiene abierta la cuenta de cada trader y, cuando
el trader abre una operación, la repite al momento en la cuenta de
cada seguidor con SU monto.

Nada de esto guarda SSID en disco: todo vive en memoria y la web
lo vuelve a mandar en cada vuelta.
============================================================
"""
import time, threading, logging, random

log = logging.getLogger("puente.motor")

_lock = threading.Lock()          # protege las listas de abajo
_trabajos = {}                    # id -> trabajo en curso o en espera
_resultados = {}                  # id -> resultado listo para la web
_mis_ordenes = set()              # órdenes que abrió el motor (nunca se copian)
_vigias = {}                      # trader id -> datos del vigía
_copias = {}                      # trader id -> config que mandó la web
_gv_lock = threading.Lock()       # la librería guarda la cuenta activa en una variable global
_vuelta = {"ultima": 0}

VENC = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60}


# ------------------------------------------------------------
# piezas que pone el puente (bridge.py) al arrancar
# ------------------------------------------------------------
H = {"cliente": None, "registro": None, "con_tope": None, "Aviso": Exception}


def instalar(cliente, registro, con_tope, Aviso):
    H.update(cliente=cliente, registro=registro, con_tope=con_tope, Aviso=Aviso)


def _libs(broker):
    """global_value y la lista de pares de la librería de ese bróker."""
    if broker == "iqoption":
        import iqoptionapi.global_value as gv
        import iqoptionapi.constants as OP
    else:
        import exnovaapi.global_value as gv
        import exnovaapi.constants as OP
    return gv, OP


def _conexion(broker, ssid, cuenta):
    """Conexión viva + su candado + los números de cuenta real y demo."""
    H["cliente"](broker, ssid, cuenta)
    reg = H["registro"](broker, ssid)
    if reg is None:
        raise H["Aviso"]("No se pudo abrir la cuenta del bróker")
    reg.setdefault("orden", threading.Lock())
    if not reg.get("cuentas"):
        api = reg["api"]
        bal = H["con_tope"](lambda: api.get_balances(), 15, "El saldo") or {}
        cuentas = {}
        for b in (bal.get("msg") or []):
            if b.get("type") == 1:
                cuentas["real"] = b.get("id")
            elif b.get("type") == 4:
                cuentas["demo"] = b.get("id")
        reg["cuentas"] = cuentas
        try:
            pr = api.get_profile_ansyc() or {}
            reg["user_id"] = pr.get("user_id") or pr.get("id")
        except Exception:
            pass
    return reg


def _saldo(reg, cuenta):
    try:
        api = reg["api"]
        bal = H["con_tope"](lambda: api.get_balances(), 10, "El saldo") or {}
        bid = reg.get("cuentas", {}).get(cuenta)
        for b in (bal.get("msg") or []):
            if b.get("id") == bid:
                return round(float(b.get("amount") or 0), 2)
    except Exception:
        pass
    return None


def _nombres(par, otc):
    par = str(par or "").upper().replace("/", "").replace(" ", "")
    base = par[:-4] if par.endswith("-OTC") else par
    finde = time.gmtime().tm_wday >= 5
    if par.endswith("-OTC"):
        return [par] + ([base] if not finde else [])
    if otc:
        return [base + "-OTC", base] if finde else [base, base + "-OTC"]
    return [base]


def _payout(reg, nombre, minutos):
    try:
        api = reg["api"]
        todo = H["con_tope"](lambda: api.get_all_profit(), 12, "El payout")
        tipo = "turbo" if minutos <= 5 else "binary"
        v = todo[nombre][tipo]
        return round(float(v) * 100, 1) if v else None
    except Exception:
        return None


def _comprar(reg, broker, cuenta, par, lado, minutos, monto, otc):
    """Manda la orden. Devuelve (True, id_orden, par_usado) o (False, motivo, par)."""
    gv, OP = _libs(broker)
    api = reg["api"]
    bid = reg.get("cuentas", {}).get(cuenta)
    if not bid:
        return False, "No encontré la cuenta %s en el bróker" % ("real" if cuenta == "real" else "demo"), par
    ultimo = "Ese par no existe en el bróker"
    for nombre in _nombres(par, otc):
        if nombre not in OP.ACTIVES:
            continue
        rid = "ch%d" % random.randint(100000, 999999)
        with reg["orden"]:
            try:
                api.api.buy_multi_option = {}
            except Exception:
                pass
            with _gv_lock:
                gv.balance_id = bid
                api.api.buyv3(float(monto), OP.ACTIVES[nombre], lado, int(minutos), rid)
            fin = time.time() + 6
            r = None
            while time.time() < fin:
                r = (api.api.buy_multi_option or {}).get(rid)
                if r and (r.get("id") or r.get("message")):
                    break
                time.sleep(0.05)
        if r and r.get("id"):
            with _lock:
                _mis_ordenes.add(r["id"])
            return True, r["id"], nombre
        ultimo = (r or {}).get("message") or "El bróker no contestó a la orden"
        log.info("orden rechazada en %s: %s", nombre, ultimo)
    return False, str(ultimo), par


def _resultado(reg, oid, minutos):
    """Espera el cierre de la orden. Devuelve (WIN/LOSS/DRAW o None, lucro)."""
    api = reg["api"]
    fin = time.time() + minutos * 60 + 75
    while time.time() < fin:
        try:
            m = api.api.socket_option_closed.get(oid)
        except Exception:
            m = None
        if m:
            msg = m.get("msg") or {}
            w = msg.get("win")
            if w == "equal":
                return "DRAW", 0.0
            if w == "loose":
                return "LOSS", round(-float(msg.get("sum") or 0), 2)
            if w == "win":
                return "WIN", round(float(msg.get("win_amount") or 0) - float(msg.get("sum") or 0), 2)
        time.sleep(0.5)
    # plan B: buscarla en el historial
    try:
        r = H["con_tope"](lambda: api.get_optioninfo_v2(30), 15, "El historial") or {}
        for o in (r.get("msg") or {}).get("closed_options", []):
            ids = o.get("id")
            if ids == oid or (isinstance(ids, list) and oid in ids):
                monto = float(o.get("amount") or 0)
                if o.get("win") == "equal":
                    return "DRAW", 0.0
                if o.get("win") == "win":
                    return "WIN", round(float(o.get("win_amount") or 0) - monto, 2)
                return "LOSS", round(-monto, 2)
    except Exception:
        pass
    return None, 0.0


def _guardar(res):
    with _lock:
        _resultados[res["id"]] = res
        _trabajos.pop(res["id"], None)


# ------------------------------------------------------------
# BOT DE SEÑALES
# ------------------------------------------------------------
def _correr(t):
    base = {"id": t["id"], "tipo": "bot", "uid": t.get("uid"), "pasos": [], "resultado": None,
            "lucro": 0.0, "saldo": None, "error": None, "par": t["par"]}
    try:
        minutos = VENC.get(str(t.get("venc", "M1")).upper(), 1)
        cuenta = "real" if t.get("cuenta") == "real" else "demo"
        lado = "put" if str(t.get("dir", "")).upper() == "PUT" else "call"
        reg = _conexion(t["broker"], t["ssid"], cuenta)        # se conecta con tiempo

        if t.get("payoutMin"):
            p = _payout(reg, _nombres(t["par"], t.get("otc"))[0], minutos)
            if p is not None and p < float(t["payoutMin"]):
                base.update(resultado="SALTADA", error="Payout de %s%% (tu mínimo es %s%%)" % (p, t["payoutMin"]))
                return _guardar(base)

        espera = float(t["hora"]) - float(t.get("anticipo", 0)) - time.time()
        if espera > 0:
            time.sleep(espera)
        if time.time() - float(t["hora"]) > 20:
            base.update(resultado="SALTADA", error="La señal llegó tarde al motor")
            return _guardar(base)

        total = 0.0
        for i, monto in enumerate(t.get("montos") or []):
            ok, oid, nombre = _comprar(reg, t["broker"], cuenta, t["par"], lado, minutos, monto, t.get("otc"))
            if not ok:
                base["error"] = oid
                break
            res, lucro = _resultado(reg, oid, minutos)
            total += lucro
            base["pasos"].append({"orden": str(oid), "par": nombre, "monto": monto, "gale": i,
                                  "resultado": res, "lucro": lucro})
            base["par"] = nombre
            if res != "LOSS":
                break
        if base["pasos"]:
            ult = base["pasos"][-1]["resultado"]
            base["resultado"] = ult or "SIN RESULTADO"
        else:
            base["resultado"] = "ERROR"
        base["lucro"] = round(total, 2)
        base["saldo"] = _saldo(reg, cuenta)
    except Exception as e:                                    # noqa: BLE001
        base.update(resultado="ERROR", error=getattr(e, "mensaje", None) or str(e))
    _guardar(base)


# ------------------------------------------------------------
# COPY TRADING
# ------------------------------------------------------------
def _copiar_a(seg, trader_id, op):
    rid = "c-%s-%s" % (op["id"], seg["clave"])
    base = {"id": rid, "tipo": "copy", "uid": seg.get("uid"), "trader": trader_id, "par": op["par"],
            "dir": op["dir"], "venc": "M%d" % op["minutos"], "monto": seg["monto"], "cuenta": "real" if seg.get("cuenta") == "real" else "demo",
            "entradaTrader": op.get("monto"), "resultado": None, "lucro": 0.0, "saldo": None,
            "error": None, "hora": int(time.time())}
    try:
        cuenta = "real" if seg.get("cuenta") == "real" else "demo"
        reg = _conexion(seg["broker"], seg["ssid"], cuenta)
        ok, oid, nombre = _comprar(reg, seg["broker"], cuenta, op["par"], op["dir"].lower(),
                                   op["minutos"], seg["monto"], True)
        if not ok:
            base.update(resultado="ERROR", error=oid)
        else:
            res, lucro = _resultado(reg, oid, op["minutos"])
            base.update(resultado=res or "SIN RESULTADO", lucro=lucro, par=nombre, orden=str(oid))
            base["saldo"] = _saldo(reg, cuenta)
    except Exception as e:                                    # noqa: BLE001
        base.update(resultado="ERROR", error=getattr(e, "mensaje", None) or str(e))
    _guardar(base)


def _leer_apertura(m, OP):
    """Saca par, dirección, minutos y monto del aviso de 'operación abierta'."""
    msg = m.get("msg") or {}
    aid = msg.get("active_id") or msg.get("active")
    par = None
    if isinstance(aid, str) and not aid.isdigit():
        par = aid
    else:
        try:
            aid = int(aid)
            for k, v in OP.ACTIVES.items():
                if v == aid:
                    par = k
                    break
        except Exception:
            pass
    vence = msg.get("expiration_time") or msg.get("exp_time") or msg.get("expired") or 0
    vence = float(vence or 0)
    if vence > 10_000_000_000:
        vence /= 1000
    minutos = max(1, int(round((vence - time.time()) / 60.0))) if vence else 1
    lado = str(msg.get("direction") or msg.get("dir") or "").lower()
    return {"id": msg.get("id"), "par": par, "dir": "PUT" if lado == "put" else "CALL",
            "minutos": minutos, "monto": msg.get("amount") or msg.get("sum"),
            "user_id": msg.get("user_id"), "abierta": float(msg.get("open_time") or msg.get("created") or 0)}


def _vigia(tid):
    """Mira la cuenta del trader y copia cada operación nueva."""
    vistos = set()
    nuevas = {}
    primera = True
    while True:
        with _lock:
            cfg = _copias.get(tid)
            vivo = _vigias.get(tid)
        if not cfg or not vivo or vivo.get("parar"):
            log.info("vigía de %s apagado", tid)
            return
        try:
            reg = _conexion(cfg["broker"], cfg["ssid"], "real")
            gv, OP = _libs(cfg["broker"])
            abiertas = dict(reg["api"].api.socket_option_opened or {})
            if primera:                       # lo que ya estaba abierto al encender no se copia
                vistos.update(abiertas.keys())
                primera = False
            ahora = time.time()
            for oid, m in abiertas.items():
                if oid in vistos:
                    continue
                # se espera un instante: si la abrió el propio motor, su número
                # llega por la respuesta de la orden y así no se copia a sí mismo
                if oid not in nuevas:
                    nuevas[oid] = ahora
                if ahora - nuevas[oid] < 0.8:
                    continue
                nuevas.pop(oid, None)
                vistos.add(oid)
                with _lock:
                    propia = oid in _mis_ordenes
                if propia:
                    continue
                op = _leer_apertura(m, OP)
                if reg.get("user_id") and op.get("user_id") and str(op["user_id"]) != str(reg["user_id"]):
                    continue                  # es de otra cuenta abierta en este puente
                if not op["par"]:
                    continue
                with _lock:
                    vivo["ultima"] = {"par": op["par"], "dir": op["dir"], "hora": int(time.time())}
                    segs = list(cfg.get("seguidores") or [])
                for seg in segs:
                    threading.Thread(target=_copiar_a, args=(seg, tid, op), daemon=True).start()
            with _lock:
                vivo["ok"] = True
                vivo["error"] = None
                vivo["visto"] = int(time.time())
            time.sleep(0.4)
        except Exception as e:                                # noqa: BLE001
            with _lock:
                vivo["ok"] = False
                vivo["error"] = getattr(e, "mensaje", None) or str(e)
            time.sleep(5)


def poner_copias(traders):
    """La web manda la lista completa en cada vuelta: se arrancan o apagan vigías."""
    nuevos = {}
    for t in traders or []:
        if t.get("id") and t.get("ssid"):
            nuevos[str(t["id"])] = t
    with _lock:
        for tid in list(_vigias.keys()):
            if tid not in nuevos or nuevos[tid]["ssid"] != (_copias.get(tid) or {}).get("ssid"):
                _vigias[tid]["parar"] = True
                _vigias.pop(tid, None)
        _copias.clear()
        _copias.update(nuevos)
        arrancar = [tid for tid in nuevos if tid not in _vigias]
        for tid in arrancar:
            _vigias[tid] = {"ok": None, "error": None, "visto": 0, "ultima": None}
    for tid in arrancar:
        threading.Thread(target=_vigia, args=(tid,), daemon=True).start()


# ------------------------------------------------------------
# lo que llama la web
# ------------------------------------------------------------
def encargar(trabajos, copias):
    nuevos = 0
    for t in trabajos or []:
        tid = t.get("id")
        if not tid or not t.get("ssid") or not t.get("montos"):
            continue
        with _lock:
            if tid in _trabajos or tid in _resultados:
                continue
            _trabajos[tid] = {"hora": t.get("hora"), "uid": t.get("uid")}
        threading.Thread(target=_correr, args=(t,), daemon=True).start()
        nuevos += 1
    if copias is not None:
        poner_copias(copias)
    _vuelta["ultima"] = int(time.time())
    return {"ok": True, "nuevos": nuevos, **estado()}


def resultados(ack):
    with _lock:
        for i in ack or []:
            _resultados.pop(i, None)
        lista = list(_resultados.values())
        # limpieza: las órdenes propias viejas ya no hacen falta
        if len(_mis_ordenes) > 5000:
            _mis_ordenes.clear()
    _vuelta["ultima"] = int(time.time())
    return {"ok": True, "resultados": lista, **estado()}


def estado():
    with _lock:
        return {"enCola": len(_trabajos), "porRecoger": len(_resultados),
                "vigias": {k: {"ok": v.get("ok"), "error": v.get("error"), "visto": v.get("visto"),
                               "ultima": v.get("ultima"), "seguidores": len((_copias.get(k) or {}).get("seguidores") or [])}
                           for k, v in _vigias.items()},
                "hora": int(time.time())}
