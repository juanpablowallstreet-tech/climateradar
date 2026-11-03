#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALSHI TRADER  ·  el brazo que opera del Radar Climatico
=======================================================

Que hace en cada vuelta (solo si activaste /trading on):
  1. Revisa tus posiciones abiertas en Kalshi:
       - si el take-profit se ejecuto o el mercado liquido, la cierra y te avisa el resultado;
       - si el mercado paga por tu posicion MAS de lo que el modelo cree que vale, vende al toque.
  2. Busca entradas nuevas entre los edges del radar con filtros estrictos y compra
     (orden limite "inmediata o cancela" al precio de venta del libro).
  3. Apenas compra, deja una orden de venta limite (take-profit) esperando en el libro de Kalshi.

Topes de seguridad (se cambian por Telegram): USD por operacion, USD por dia, posiciones abiertas,
una sola posicion por evento, freno automatico si el dia viene en perdida.

Credenciales (secretos de GitHub): KALSHI_KEY_ID y KALSHI_PRIVATE_KEY (el .key/.pem completo).
No es consejo de inversion. Un modelo puede perder plata: arrancá con montos minimos.
"""

import base64
import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import radar_climatico as R

UTC = timezone.utc
API = os.environ.get("KALSHI_API", "https://api.elections.kalshi.com/trade-api/v2")

DEFAULT_TRADING = {
    "activo": False,          # hay que prenderlo a mano con /trading on
    "max_por_op": 1.0,        # USD maximo por operacion
    "max_dia": 10.0,          # USD maximo comprado por dia
    "max_posiciones": 8,      # posiciones abiertas a la vez
    "edge_min": 0.06,         # edge neto minimo (6c por contrato)
    "tp": 0.30,               # take-profit: +30% sobre el precio de entrada
    "margen_salida": 0.02,    # vende si el mercado paga 2c mas de lo que vale segun el modelo
    "min_precio": 0.10,       # no compra lotería ni casi-seguros
    "max_precio": 0.85,
    "max_spread": 0.04,
    "min_vol": 50,
    "perdida_max_dia": 5.0,   # si el dia pierde esto (USD), se pausa solo
    "vender_todo": False,
    "gasto": {},              # {fecha: usd}
    "pnl_dia": {},            # {fecha: usd}
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fee(p):
    return R.fee(p, 0.07)


def cents(p):
    return int(round(p * 100))


# ----------------------------------------------------------------------------- CLIENTE FIRMADO

class Kalshi:
    """Cliente minimo de la API de Kalshi con firma RSA-PSS (o Ed25519), segun la documentacion oficial."""

    def __init__(self, key_id, pem, base=API):
        from cryptography.hazmat.primitives import serialization
        pem = (pem or "").strip()
        if "\\n" in pem and "\n" not in pem:
            pem = pem.replace("\\n", "\n")
        self.key_id = key_id.strip()
        self.key = serialization.load_pem_private_key(pem.encode(), password=None)
        self.base = base.rstrip("/")

    def sign(self, ts, method, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        msg = f"{ts}{method}{path.split('?')[0]}".encode()
        if isinstance(self.key, Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def request(self, method, path, params=None, body=None, retries=2):
        url = self.base + path + (("?" + urlencode(params)) if params else "")
        sign_path = urlparse(self.base + path).path
        last = None
        for i in range(retries + 1):
            ts = str(int(time.time() * 1000))
            headers = {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                       "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, sign_path),
                       "Content-Type": "application/json", "Accept": "application/json"}
            data = json.dumps(body).encode() if body is not None else None
            try:
                with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (429, 500, 502, 503, 504) and i < retries:
                    last = RuntimeError(f"Kalshi {e.code}: {detail}")
                    time.sleep(1.5 * (i + 1))
                    continue
                raise RuntimeError(f"Kalshi {e.code}: {detail}")
            except (URLError, TimeoutError) as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise last

    # --- endpoints ---
    def balance(self):
        d = self.request("GET", "/portfolio/balance")
        if d.get("balance_dollars") not in (None, ""):
            return float(d["balance_dollars"])
        return (d.get("balance") or 0) / 100

    def positions(self):
        """{ticker: contratos} positivo = YES, negativo = NO."""
        out, cursor = {}, None
        for _ in range(10):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = self.request("GET", "/portfolio/positions", params)
            for p in d.get("market_positions") or []:
                n = R.num_field(p, "position_fp", "position")
                if n:
                    out[p.get("ticker")] = int(round(n))
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    def order(self, ticker, action, side, count, price_cents, tif, reduce_only=False):
        body = {"ticker": ticker, "action": action, "side": side, "count": int(count), "type": "limit",
                "time_in_force": tif, "client_order_id": str(uuid.uuid4())}
        body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
        if reduce_only:
            body["reduce_only"] = True
        return self.request("POST", "/portfolio/orders", body=body).get("order") or {}

    def get_order(self, order_id):
        return self.request("GET", f"/portfolio/orders/{order_id}").get("order") or {}

    def cancel(self, order_id):
        try:
            return self.request("DELETE", f"/portfolio/orders/{order_id}")
        except RuntimeError as e:
            if "404" in str(e):
                return {}
            raise


def client_from_env():
    key_id, pem = os.environ.get("KALSHI_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY")
    if not key_id or not pem:
        return None
    return Kalshi(key_id, pem)


# ----------------------------------------------------------------------------- LIBRO DE POSICIONES

def load_book(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"abiertas": [], "cerradas": []}


def save_book(path, book):
    book["cerradas"] = book.get("cerradas", [])[-300:]
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def filled(o):
    return int(round(R.num_field(o, "fill_count_fp", "fill_count")))


def avg_fill_price(o, side, fallback):
    n = filled(o)
    cost = R.num_field(o, "taker_fill_cost_dollars") or R.num_field(o, "taker_fill_cost") / 100
    cost += R.num_field(o, "maker_fill_cost_dollars") or R.num_field(o, "maker_fill_cost") / 100
    return round(cost / n, 4) if n and cost else fallback


# ----------------------------------------------------------------------------- REGLAS

def eligible(r, T, now):
    """Filtros de entrada. Todo tiene que dar bien para comprar."""
    if not r.get("side") or r.get("ev") is None or r.get("suspect"):
        return False
    price = r["ya"] if r["side"] == "YES" else r["na"]
    if price is None or not (T["min_precio"] <= price <= T["max_precio"]):
        return False
    if r["ev"] < T["edge_min"]:
        return False
    if r.get("spread") is None or r["spread"] > T["max_spread"] or r.get("vol", 0) < T["min_vol"]:
        return False
    if str(r.get("phase", "")).startswith(("cerrado", "observado", "tarde")):
        return False
    ct = r.get("close_time")
    if ct:
        try:
            if datetime.fromisoformat(ct.replace("Z", "+00:00")) - now < timedelta(minutes=45):
                return False
        except ValueError:
            pass
    return True


def run(rows, cfg, tg, client, chat, now=None, notify=True):
    """Una vuelta del bot. Devuelve un resumen (dict)."""
    now = now or datetime.now(UTC)
    T = cfg.setdefault("trading", {})
    for k, v in DEFAULT_TRADING.items():
        T.setdefault(k, v if not isinstance(v, dict) else {})
    book_path = cfg["_book_path"]
    book = load_book(book_path)
    day = now.strftime("%Y-%m-%d")
    T["gasto"] = {d: v for d, v in T["gasto"].items() if d >= (now - timedelta(days=3)).strftime("%Y-%m-%d")}
    T["pnl_dia"] = {d: v for d, v in T["pnl_dia"].items() if d >= (now - timedelta(days=7)).strftime("%Y-%m-%d")}
    summary = {"compras": 0, "ventas": 0, "cierres": 0, "errores": 0}

    def say(msg):
        if notify and chat:
            try:
                tg.send(chat, msg)
            except Exception:
                pass

    def close(p, how, exit_price):
        n = p["count"]
        pnl = round((exit_price - p["entry"]) * n - fee(p["entry"]) * n
                    - (fee(exit_price) * n if how in ("take-profit", "venta") else 0), 2)
        p.update({"estado": how, "salida": exit_price, "pnl": pnl, "cerrada": now.isoformat()})
        book["abiertas"].remove(p)
        book["cerradas"].append(p)
        T["pnl_dia"][day] = round(T["pnl_dia"].get(day, 0) + pnl, 2)
        summary["cierres"] += 1
        icon = "💰" if pnl > 0 else "🩸" if pnl < 0 else "➖"
        say(f"{icon} <b>Cerré</b> {p['city']} {p['label']} {p['side']} ({how})\n"
            f"Entrada {p['entry'] * 100:.0f}¢ → salida {exit_price * 100:.0f}¢ · {n} contrato(s) · "
            f"<b>{'+' if pnl >= 0 else ''}{pnl:.2f} USD</b>")

    # ---------------------------------------------------------------- 1) posiciones abiertas
    try:
        held = client.positions()
    except Exception as e:
        say(f"⚠️ No pude leer tus posiciones de Kalshi: {e}")
        summary["errores"] += 1
        save_book(book_path, book)
        return summary
    by_ticker = {r["ticker"]: r for r in rows}
    for p in list(book["abiertas"]):
        sign = 1 if p["side"] == "YES" else -1
        have = max(0, sign * held.get(p["ticker"], 0))
        if have == 0:
            # Ya no la tenemos: o se ejecuto el take-profit, o liquido el mercado
            o = {}
            if p.get("tp_id"):
                try:
                    o = client.get_order(p["tp_id"])
                except Exception:
                    o = {}
            if filled(o) > 0:
                close(p, "take-profit", p["tp"])
                continue
            try:
                res = (R.get_json(f"{R.KALSHI}/markets/{p['ticker']}").get("market") or {}).get("result", "")
            except Exception:
                res = ""
            if res in ("yes", "no"):
                close(p, "liquidó", 1.0 if res == p["side"].lower() else 0.0)
            elif p.get("sin_rastro", 0) >= 3:
                close(p, "cerrada fuera del bot", p["entry"])
            else:
                p["sin_rastro"] = p.get("sin_rastro", 0) + 1
            continue
        p["count"] = have
        r = by_ticker.get(p["ticker"])
        if not r and not T.get("vender_todo"):
            continue
        p_side = (r["p"] if p["side"] == "YES" else 1 - r["p"]) if r else 0.0
        bid = (r["yb"] if p["side"] == "YES" else r.get("nb")) if r else None
        sell_now = T.get("vender_todo") or (bid and bid - fee(bid) >= p_side + T["margen_salida"])
        if not sell_now or not bid or bid <= 0.01:
            continue
        try:
            if p.get("tp_id"):
                client.cancel(p["tp_id"])
            o = client.order(p["ticker"], "sell", p["side"].lower(), have, cents(bid), "immediate_or_cancel",
                             reduce_only=True)
            n = filled(o)
            if n >= have:
                summary["ventas"] += 1
                close(p, "venta", bid)
            else:
                # vendio parcial o nada: vuelve a dejar el take-profit por lo que queda
                p["count"] = have - n
                tp = client.order(p["ticker"], "sell", p["side"].lower(), p["count"], cents(p["tp"]),
                                  "good_till_canceled", reduce_only=True)
                p["tp_id"] = tp.get("order_id")
        except Exception as e:
            summary["errores"] += 1
            say(f"⚠️ No pude vender {p['ticker']}: {e}")
    if T.get("vender_todo"):
        T["vender_todo"] = False
        quedan = len(book["abiertas"])
        say("✅ Vendí todo lo que tenía comprador." + (f" Quedan {quedan} abiertas (sin comprador o "
            "esperando liquidación)." if quedan else "") + " Trading " + ("activo." if T["activo"] else "apagado."))

    # ---------------------------------------------------------------- freno por perdida
    if T["activo"] and T["pnl_dia"].get(day, 0) <= -abs(T["perdida_max_dia"]):
        T["activo"] = False
        say(f"🛑 <b>Freno automático:</b> hoy van {T['pnl_dia'][day]:.2f} USD. Apagué las compras. "
            f"Mandá /trading on para retomar.")

    # ---------------------------------------------------------------- 2) entradas nuevas
    if T["activo"] and not T.get("vender_todo"):
        spent = T["gasto"].get(day, 0.0)
        open_events = {p["event"] for p in book["abiertas"]}
        open_tickers = {p["ticker"] for p in book["abiertas"]} | {t for t, n in held.items() if n}
        recent = {p["ticker"] for p in book["cerradas"]
                  if p.get("cerrada", "") >= (now - timedelta(hours=3)).isoformat()}
        for r in sorted(rows, key=lambda x: x.get("ev") or -9, reverse=True):
            if len(book["abiertas"]) >= T["max_posiciones"]:
                break
            if not eligible(r, T, now) or r["event"] in open_events or r["ticker"] in open_tickers \
                    or r["ticker"] in recent:
                continue
            price = r["ya"] if r["side"] == "YES" else r["na"]
            count = int(T["max_por_op"] // (price + fee(price)))
            if count < 1:
                continue
            cost = round(count * (price + fee(price)), 2)
            if spent + cost > T["max_dia"]:
                break
            side = r["side"].lower()
            try:
                o = client.order(r["ticker"], "buy", side, count, cents(price), "immediate_or_cancel")
            except Exception as e:
                summary["errores"] += 1
                say(f"⚠️ No pude comprar {r['ticker']}: {e}")
                continue
            n = filled(o)
            if n <= 0:
                log(f"Sin llenar: {r['ticker']} {side} a {cents(price)}c")
                continue
            entry = avg_fill_price(o, side, price)
            spent = round(spent + n * (entry + fee(entry)), 2)
            T["gasto"][day] = spent
            tp_c = min(99, max(cents(entry) + 1, math.ceil(entry * 100 * (1 + T["tp"]))))
            pos = {"ticker": r["ticker"], "event": r["event"], "city": r["city"], "label": r["label"],
                   "side": r["side"], "count": n, "entry": entry, "tp": tp_c / 100, "tp_id": None,
                   "edge": round(r["ev"], 4), "modelo": round(r["p"], 4), "abierta": now.isoformat()}
            try:
                tp = client.order(r["ticker"], "sell", side, n, tp_c, "good_till_canceled", reduce_only=True)
                pos["tp_id"] = tp.get("order_id")
            except Exception as e:
                say(f"⚠️ Compré pero no pude dejar el take-profit de {r['ticker']}: {e}")
            book["abiertas"].append(pos)
            open_events.add(r["event"])
            summary["compras"] += 1
            pm = r["p"] if r["side"] == "YES" else 1 - r["p"]
            say(f"🤖 <b>Compré</b> {r['city']} {'🔺' if r['kind'] == 'max' else '🔻'} {r['label']} "
                f"<b>{r['side']}</b>\n{n} contrato(s) a {entry * 100:.0f}¢ · modelo {pm * 100:.0f}% · "
                f"edge +{r['ev'] * 100:.1f}¢\n🎯 Take-profit puesto a {tp_c}¢ · gastado hoy {spent:.2f}/"
                f"{T['max_dia']:.0f} USD")
    save_book(book_path, book)
    return summary


# ----------------------------------------------------------------------------- RESUMENES

def fmt_cartera(cfg, client=None):
    T = {**DEFAULT_TRADING, **cfg.get("trading", {})}
    book = load_book(cfg["_book_path"])
    lines = [f"<b>🤖 Trading {'ACTIVO' if T['activo'] else 'apagado'}</b> · USD {T['max_por_op']:.2f}/op · "
             f"{T['max_dia']:.0f}/día · edge ≥ {T['edge_min'] * 100:.0f}¢ · TP +{T['tp'] * 100:.0f}%"]
    if client:
        try:
            lines.append(f"Saldo Kalshi: <b>{client.balance():.2f} USD</b>")
        except Exception as e:
            lines.append(f"Saldo: no disponible ({e})")
    ab = book.get("abiertas", [])
    lines.append(f"\n<b>Abiertas ({len(ab)})</b>")
    for p in ab:
        lines.append(f"• {p['city']} {p['label']} {p['side']} · {p['count']} a {p['entry'] * 100:.0f}¢ → TP {p['tp'] * 100:.0f}¢")
    ce = book.get("cerradas", [])
    if ce:
        tot = sum(p.get("pnl", 0) for p in ce)
        wins = sum(1 for p in ce if p.get("pnl", 0) > 0)
        lines.append(f"\n<b>Cerradas: {len(ce)}</b> · ganadas {wins} · P&L <b>{'+' if tot >= 0 else ''}{tot:.2f} USD</b>")
        for p in ce[-5:]:
            lines.append(f"• {p['city']} {p['label']} {p['side']}: {p['estado']} {p.get('pnl', 0):+.2f}")
    return "\n".join(lines)
