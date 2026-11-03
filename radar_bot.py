#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RADAR BOT  ·  Alertas de edges Kalshi x NBM por Telegram
========================================================

El sistema escanea solo todos los mercados de temperatura de Kalshi cada X minutos,
y te manda un mensaje a Telegram cuando aparece un edge igual o mayor al que vos elegiste.
Vos decidis si compras. Ademas registra cada alerta y, cuando el mercado liquida,
anota si hubiera ganado o perdido (para medir si el modelo sirve antes de pensar en un bot que opere).

Necesita radar_climatico.py en la MISMA carpeta.

Primer uso:
  1. En Telegram habla con @BotFather -> /newbot -> elegi nombre -> copia el TOKEN.
  2. python radar_bot.py   (te pide el token la primera vez y lo guarda)
  3. En Telegram abri tu bot y mandale /start  -> queda vinculado SOLO a vos.
  4. Mandale /edge 17   -> te avisa cuando haya edges de 17 centavos (17 puntos) o mas.

Comandos (en Telegram):
  /edge 17        avisar con edge neto >= 17c por contrato (17 puntos de probabilidad)
  /roi 30         ademas exigir ROI >= 30% (/roi off para sacarlo)
  /liquidos on    solo mercados liquidos (spread chico y volumen) · /liquidos off
  /tipo max       solo maximas · /tipo min · /tipo todas
  /intervalo 10   cada cuantos minutos escanea (minimo 5)
  /top            los 5 mejores edges de ahora, sin importar el umbral
  /dashboard      te manda el dashboard completo (HTML) al chat
  /stats          resultados de las alertas ya liquidadas
  /escanear       escanear ya
  /pausa          deja de avisar · /seguir vuelve a avisar
  /estado         configuracion actual
  /ayuda          esta lista

No es consejo de inversion. El modelo puede estar equivocado.
"""

import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import radar_climatico as R

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "estado"
STATE_DIR.mkdir(exist_ok=True)
CONFIG = STATE_DIR / "radar_config.json"
ALERTS = STATE_DIR / "alertas.csv"
DASH = BASE / "radar_climatico.html"
UTC = timezone.utc

DEFAULT_CFG = {
    "token": None, "chat_id": None, "edge": 0.17, "roi": None, "solo_liquidos": False,
    "tipo": "todas", "pausado": False, "raros": False, "intervalo_min": 10, "offset": 0, "enviadas": {},
}
CSV_FIELDS = ["fecha_alerta", "ticker", "evento", "ciudad", "tipo", "fecha_evento", "rango",
              "lado", "precio", "modelo", "edge", "resultado", "pnl"]
REALERT_STEP = 0.05   # vuelve a avisar del mismo mercado si el edge mejora 5c o mas
MAX_ALERTS_PER_SCAN = 8
MODEL_VERSION = 5     # al cambiar el modelo, se archiva el registro viejo para no ensuciar /stats

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------------------- CONFIG

def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:
            log("aviso: radar_config.json danado, uso valores por defecto")
    return cfg


def migrate(cfg, tg=None):
    """Si cambió el modelo, archiva alertas viejas y resetea las ya enviadas."""
    if cfg.get("model_version") == MODEL_VERSION:
        return
    old = cfg.get("model_version", 1)
    if ALERTS.exists():
        ALERTS.replace(ALERTS.with_name(f"alertas_v{old}.csv"))
    cfg["enviadas"] = {}
    cfg["model_version"] = MODEL_VERSION
    if tg and cfg.get("chat_id"):
        try:
            tg.send(cfg["chat_id"], f"🔧 <b>Modelo actualizado a v{MODEL_VERSION}.</b> Lector del NBM blindado (con respaldo IEM), mínimas corregidas "
                                    "y filtro de edges sospechosos 🧪. Reinicié el registro de /stats.")
        except Exception:
            pass


def save_cfg(cfg, keep_token=True):
    data = dict(cfg)
    if not keep_token:
        data.pop("token", None)  # en la nube el token vive en un secreto, nunca en el repo
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)


# ----------------------------------------------------------------------------- TELEGRAM

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method, params=None, timeout=40):
        data = json.dumps(params or {}).encode("utf-8")
        req = Request(f"{self.base}/{method}", data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read())
        if not res.get("ok"):
            raise RuntimeError(res.get("description", "error de Telegram"))
        return res.get("result")

    def send(self, chat_id, text):
        for chunk in _chunks(text, 3900):
            self.call("sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                      "disable_web_page_preview": True})

    def send_doc(self, chat_id, path, caption=""):
        boundary = uuid.uuid4().hex
        body = bytearray()
        for k, v in (("chat_id", str(chat_id)), ("caption", caption), ("parse_mode", "HTML")):
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
                 f"filename=\"{Path(path).name}\"\r\nContent-Type: text/html\r\n\r\n").encode()
        body += Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = Request(f"{self.base}/sendDocument", data=bytes(body),
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def updates(self, offset, timeout=15):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message"]}, timeout=timeout + 15) or []


def _chunks(text, n):
    if len(text) <= n:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out


def h(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------------------- MENSAJES

def ddmm(iso):
    y, m, d = iso.split("-")
    return f"{d}/{m}"


def side_price(r):
    return r["ya"] if r["side"] == "YES" else r["na"]


def fmt_alert(r, title="🚨 EDGE"):
    price = side_price(r)
    pm = r["p"] if r["side"] == "YES" else 1 - r["p"]
    kind = "🔺 Máxima" if r["kind"] == "max" else "🔻 Mínima"
    liq = "" if r["liquid"] else "\n⚠️ Poco líquido: mirá el libro antes de entrar"
    if r.get("suspect"):
        liq += "\n🧪 Diferencia enorme con el mercado: probablemente al modelo le falta un dato"
    roi = f" · ROI {r['roi'] * 100:.0f}%" if r.get("roi") is not None else ""
    obs = f" · obs {r['obs']}°" if r.get("obs") is not None else ""
    spread = f"{r['spread'] * 100:.0f}¢" if r.get("spread") is not None else "—"
    return (f"<b>{title} +{r['ev'] * 100:.1f}¢</b> · {h(r['city'])} {kind} {ddmm(r['date'])}\n"
            f"Rango: <b>{h(r['label'])}</b> → comprar <b>{r['side']}</b> a <b>{price * 100:.0f}¢</b>\n"
            f"Modelo {pm * 100:.1f}% vs precio {price * 100:.0f}%{roi}\n"
            f"μ {r['mu']}° σ {r['sigma']}°{obs} · {h(r['phase'])}\n"
            f"Vol {r['vol']:,} · spread {spread}{liq}\n"
            f"👉 {r['url']}")


HELP = (
    "<b>📡 Radar Climático · comandos</b>\n"
    "/edge 17 — avisar con edge ≥ 17¢\n"
    "/roi 30 — además ROI ≥ 30% (/roi off)\n"
    "/liquidos on|off — solo mercados líquidos\n"
    "/tipo max|min|todas\n"
    "/intervalo 10 — minutos entre escaneos\n"
    "/top — 5 mejores edges ahora\n"
    "/dashboard — dashboard completo\n"
    "/stats — resultados de alertas liquidadas\n"
    "/escanear — escanear ya\n"
    "/raros on|off — incluir edges sospechosos (🧪)\n"
    "/pausa · /seguir · /estado"
)


def fmt_estado(cfg, state):
    last = state.get("last_scan_txt") or "todavía no"
    roi = f"{cfg['roi'] * 100:.0f}%" if cfg.get("roi") is not None else "sin filtro"
    return (f"<b>⚙️ Estado</b>\n"
            f"{'⏸ PAUSADO' if cfg['pausado'] else '▶️ Activo'}\n"
            f"Edge mínimo: <b>{cfg['edge'] * 100:.0f}¢</b> · ROI mínimo: {roi}\n"
            f"Solo líquidos: {'sí' if cfg['solo_liquidos'] else 'no'} · Tipo: {cfg['tipo']}\n"
            f"Escaneo cada {cfg['intervalo_min']} min · último: {last}\n"
            f"Mercados en el último escaneo: {len(state.get('rows') or [])}")


# ----------------------------------------------------------------------------- FILTRO DE ALERTAS

def passes(r, cfg):
    if r.get("ev") is None or r["ev"] < cfg["edge"]:
        return False
    if r.get("suspect") and not cfg.get("raros"):
        return False
    if cfg.get("roi") is not None and (r.get("roi") is None or r["roi"] < cfg["roi"]):
        return False
    if cfg.get("solo_liquidos") and not r.get("liquid"):
        return False
    if cfg.get("tipo") == "max" and r["kind"] != "max":
        return False
    if cfg.get("tipo") == "min" and r["kind"] != "min":
        return False
    return True


def pick_alerts(rows, cfg):
    """Devuelve (alertas_nuevas, es_primera_vez_por_clave). Evita repetir el mismo mercado."""
    sent = cfg.setdefault("enviadas", {})
    out = []
    for r in rows:
        if not passes(r, cfg):
            continue
        key = f"{r['ticker']}|{r['side']}"
        prev = sent.get(key)
        if prev is None:
            out.append((r, True))
        elif r["ev"] >= prev[0] + REALERT_STEP:
            out.append((r, False))
    return out


def mark_sent(alerts, cfg):
    sent = cfg.setdefault("enviadas", {})
    for r, _ in alerts:
        sent[f"{r['ticker']}|{r['side']}"] = [r["ev"], r["date"]]
    cutoff = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
    for k in [k for k, v in sent.items() if v[1] < cutoff]:
        del sent[k]


# ----------------------------------------------------------------------------- REGISTRO / RESULTADOS

def read_alerts():
    if not ALERTS.exists():
        return []
    with ALERTS.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_alerts(rows):
    with ALERTS.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def log_alerts(alerts):
    new = [r for r, first in alerts if first]
    if not new:
        return
    rows = read_alerts()
    now = datetime.now(UTC).isoformat(timespec="minutes")
    for r in new:
        price = side_price(r)
        rows.append({"fecha_alerta": now, "ticker": r["ticker"], "evento": r["event"], "ciudad": r["city"],
                     "tipo": r["kind"], "fecha_evento": r["date"], "rango": r["label"], "lado": r["side"],
                     "precio": f"{price:.2f}", "modelo": f"{r['p']:.4f}", "edge": f"{r['ev']:.4f}",
                     "resultado": "", "pnl": ""})
    write_alerts(rows)


def settle_pending(fee_rate):
    """Consulta en Kalshi los mercados de alertas pasadas y anota si ganaron."""
    rows = read_alerts()
    if not rows:
        return []
    today = (datetime.now(UTC) - timedelta(hours=6)).date().isoformat()
    newly = []
    for row in rows:
        if row["resultado"] or row["fecha_evento"] >= today:
            continue
        try:
            m = R.get_json(f"{R.KALSHI}/markets/{row['ticker']}").get("market") or {}
        except Exception:
            continue
        res = (m.get("result") or "").lower()
        if res not in ("yes", "no"):
            continue
        price = float(row["precio"])
        f = R.fee(price, fee_rate)
        win = res == row["lado"].lower()
        row["resultado"] = "GANÓ" if win else "perdió"
        row["pnl"] = f"{(1 - price - f) if win else (-price - f):.2f}"
        newly.append(row)
    if newly:
        write_alerts(rows)
    return newly


def fmt_stats():
    rows = [r for r in read_alerts() if r["resultado"]]
    pend = len([r for r in read_alerts() if not r["resultado"]])
    if not rows:
        return f"📊 Todavía no hay alertas liquidadas. Pendientes: {pend}."
    wins = sum(1 for r in rows if r["resultado"] == "GANÓ")
    pnl = sum(float(r["pnl"]) for r in rows)
    cost = sum(float(r["precio"]) for r in rows)
    avg_edge = sum(float(r["edge"]) for r in rows) / len(rows)
    return (f"<b>📊 Resultados (1 contrato por alerta)</b>\n"
            f"Liquidadas: {len(rows)} · ganadas: {wins} ({wins / len(rows) * 100:.0f}%)\n"
            f"P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USD</b> sobre {cost:.2f} USD invertidos "
            f"(ROI {pnl / cost * 100:+.0f}%)\n"
            f"Edge promedio prometido: {avg_edge * 100:.1f}¢\n"
            f"Pendientes: {pend}\n"
            f"<i>Si el P&L real queda muy por debajo del edge prometido, el modelo está sobreconfiado.</i>")


# ----------------------------------------------------------------------------- COMANDOS

def handle_command(text, cfg, state, tg, chat_id):
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else None

    def reply(msg):
        tg.send(chat_id, msg)

    def num(a):
        return float(a.replace(",", ".").replace("%", "").replace("¢", "").replace("c", ""))

    if cmd in ("/start", "/ayuda", "/help"):
        reply(HELP)
    elif cmd == "/edge":
        try:
            v = num(arg)
            cfg["edge"] = v / 100 if v >= 1 else v
            reply(f"✅ Te aviso con edge ≥ <b>{cfg['edge'] * 100:.0f}¢</b> por contrato.")
            state["force"] = True
        except Exception:
            reply("Usá así: <code>/edge 17</code>")
    elif cmd == "/roi":
        if arg in ("off", "no", "0"):
            cfg["roi"] = None
            reply("✅ Filtro de ROI desactivado.")
        else:
            try:
                cfg["roi"] = num(arg) / 100
                reply(f"✅ Además exijo ROI ≥ <b>{cfg['roi'] * 100:.0f}%</b>.")
            except Exception:
                reply("Usá así: <code>/roi 30</code> o <code>/roi off</code>")
    elif cmd == "/liquidos":
        cfg["solo_liquidos"] = arg in ("on", "si", "sí", "1")
        reply(f"✅ Solo líquidos: {'sí' if cfg['solo_liquidos'] else 'no'}.")
    elif cmd == "/tipo":
        if arg in ("max", "min", "todas"):
            cfg["tipo"] = arg
            reply(f"✅ Tipo: {arg}.")
        else:
            reply("Usá: <code>/tipo max</code>, <code>/tipo min</code> o <code>/tipo todas</code>")
    elif cmd == "/intervalo":
        try:
            cfg["intervalo_min"] = max(5, int(num(arg)))
            reply(f"✅ Escaneo cada {cfg['intervalo_min']} min.")
        except Exception:
            reply("Usá así: <code>/intervalo 10</code>")
    elif cmd == "/raros":
        cfg["raros"] = arg in ("on", "si", "sí", "1")
        reply("✅ Edges sospechosos 🧪: " + ("incluidos (ojo)" if cfg["raros"] else "filtrados") + ".")
    elif cmd == "/pausa":
        cfg["pausado"] = True
        reply("⏸ Alertas en pausa. /seguir para volver.")
    elif cmd == "/seguir":
        cfg["pausado"] = False
        state["force"] = True
        reply("▶️ Alertas activas de nuevo.")
    elif cmd == "/estado":
        reply(fmt_estado(cfg, state))
    elif cmd == "/escanear":
        state["force"] = True
        reply("🔄 Escaneando…")
    elif cmd in ("/top", "/dashboard") and state.get("once"):
        state["after_scan"].add(cmd)
        state["force"] = True
        reply("⏳ Escaneando y te lo mando en un toque…")
    elif cmd == "/top":
        top = [r for r in (state.get("rows") or []) if r.get("ev") is not None and not r.get("suspect")][:5]
        if not top:
            reply("Todavía no hay datos. Probá /escanear.")
        else:
            reply("\n\n".join(fmt_alert(r, title=f"#{i + 1}") for i, r in enumerate(top)))
    elif cmd == "/dashboard":
        if DASH.exists():
            tg.send_doc(chat_id, DASH, "📡 Dashboard completo · abrilo en el navegador")
        else:
            reply("Todavía no hay dashboard. Probá /escanear.")
    elif cmd == "/stats":
        reply(fmt_stats())
    else:
        reply("No entendí. /ayuda para ver los comandos.")


def process_updates(updates, cfg, state, tg):
    for u in updates:
        cfg["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = msg.get("text") or ""
        chat = (msg.get("chat") or {}).get("id")
        if not chat or not text.startswith("/"):
            continue
        if cfg.get("chat_id") is None and text.startswith("/start"):
            cfg["chat_id"] = chat
            log(f"Bot vinculado al chat {chat}")
            tg.send(chat, "🔗 Listo, este bot quedó vinculado solo a vos.\n\n" + fmt_estado(cfg, state))
        if chat != cfg.get("chat_id"):
            tg.send(chat, "Este bot es privado.")
            continue
        try:
            handle_command(text, cfg, state, tg, chat)
        except Exception as e:
            tg.send(chat, f"Error con ese comando: {h(e)}")


# ----------------------------------------------------------------------------- ESCANEO

def run_scan(cfg, state, tg, args, demo=False):
    rows, meta = R.demo_data(args) if demo else R.scan(args)
    R.render_dashboard(rows, meta, DASH)
    state["rows"] = rows
    state["last_scan_txt"] = datetime.now().strftime("%H:%M")
    chat = cfg.get("chat_id")
    if not chat:
        log("Escaneo hecho, pero el bot todavía no está vinculado (mandale /start en Telegram)")
        return
    if not cfg["pausado"]:
        alerts = pick_alerts(rows, cfg)
        if alerts:
            shown = alerts[:MAX_ALERTS_PER_SCAN]
            for r, first in shown:
                tg.send(chat, fmt_alert(r, title="🚨 EDGE" if first else "📈 MEJORÓ EL EDGE"))
            if len(alerts) > len(shown):
                tg.send(chat, f"…y {len(alerts) - len(shown)} más arriba de tu umbral. /top o /dashboard")
            log_alerts(alerts)
            mark_sent(alerts, cfg)
            log(f"{len(alerts)} alertas enviadas")
        else:
            log("Sin edges arriba del umbral")
    if not demo:
        for row in settle_pending(args.fee_rate):
            tg.send(chat, f"🏁 Liquidó: {h(row['ciudad'])} {h(row['rango'])} {row['lado']} → "
                          f"<b>{row['resultado']}</b> ({float(row['pnl']):+.2f} USD por contrato)")


def upload_dashboard_ftp(path):
    """Sube el dashboard a tu hosting (Nuthost u otro) si estan los secretos FTP_*."""
    host = os.environ.get("FTP_HOST")
    if not host:
        return False
    from ftplib import FTP, FTP_TLS, error_perm
    user, pwd = os.environ.get("FTP_USER", ""), os.environ.get("FTP_PASS", "")
    folder = os.environ.get("FTP_DIR", "public_html/radar").strip("/")
    ftp = None
    for cls in (FTP_TLS, FTP):
        try:
            ftp = cls(host, timeout=60)
            ftp.login(user, pwd)
            if cls is FTP_TLS:
                ftp.prot_p()
            break
        except Exception as e:
            log(f"FTP ({cls.__name__}): {e}")
            ftp = None
    if ftp is None:
        raise RuntimeError("no pude entrar por FTP: revisá FTP_HOST, FTP_USER y FTP_PASS")
    for part in folder.split("/"):
        if not part:
            continue
        try:
            ftp.cwd(part)
        except error_perm:
            ftp.mkd(part)
            ftp.cwd(part)
    with open(path, "rb") as f:
        ftp.storbinary("STOR index.html", f)
    ftp.quit()
    log(f"Dashboard subido al hosting: {folder}/index.html")
    return True


def run_once(tg, demo=False):
    """Modo nube (GitHub Actions): lee comandos pendientes, escanea, avisa y termina."""
    cfg = load_cfg()
    migrate(cfg, tg)
    args = R.build_parser().parse_args([])
    args.loop = cfg["intervalo_min"]  # el dashboard publicado se recarga solo cada X minutos
    state = {"rows": [], "force": False, "once": True, "after_scan": set()}
    try:
        ups = tg.updates(cfg["offset"], timeout=0)
        process_updates(ups, cfg, state, tg)
    except Exception as e:
        log(f"Telegram: {e}")
    due = time.time() - cfg.get("last_scan_ts", 0) >= cfg["intervalo_min"] * 60 - 90
    if state["force"] or (due and not cfg["pausado"]):
        try:
            log("Escaneando Kalshi + NBM…")
            run_scan(cfg, state, tg, args, demo=demo)
            cfg["last_scan_ts"] = time.time()
            try:
                upload_dashboard_ftp(DASH)
            except Exception as e:
                log(f"FTP: {e}")
                if cfg.get("chat_id") and time.time() - cfg.get("last_ftp_err_ts", 0) > 6 * 3600:
                    cfg["last_ftp_err_ts"] = time.time()
                    tg.send(cfg["chat_id"], f"⚠️ No pude subir el dashboard al hosting: {h(e)}")
            chat = cfg.get("chat_id")
            if chat and "/top" in state["after_scan"]:
                handle_command("/top", cfg, {**state, "once": False}, tg, chat)
            if chat and "/dashboard" in state["after_scan"]:
                tg.send_doc(chat, DASH, "📡 Dashboard completo · abrilo en el navegador")
        except Exception as e:
            log(f"ERROR en escaneo: {e}")
            if cfg.get("chat_id") and time.time() - cfg.get("last_err_ts", 0) > 3600:
                cfg["last_err_ts"] = time.time()
                try:
                    tg.send(cfg["chat_id"], f"⚠️ Falló un escaneo: {h(e)}\nSigo intentando.")
                except Exception:
                    pass
    else:
        log("Nada que hacer en esta vuelta (pausado o todavía no toca escanear)")
    save_cfg(cfg, keep_token=False)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Radar Bot: alertas Telegram")
    ap.add_argument("--demo", action="store_true", help="usar datos inventados (para probar Telegram)")
    ap.add_argument("--token", help="token del bot de Telegram (si no, lo pide o usa RADAR_TG_TOKEN)")
    ap.add_argument("--once", action="store_true", help="una sola vuelta y termina (modo nube)")
    a = ap.parse_args()

    if a.once:
        token = a.token or os.environ.get("RADAR_TG_TOKEN")
        if not token:
            log("ERROR: falta el secreto RADAR_TG_TOKEN")
            sys.exit(1)
        run_once(Telegram(token), demo=a.demo)
        return

    cfg = load_cfg()
    token = a.token or os.environ.get("RADAR_TG_TOKEN") or cfg.get("token")
    if not token:
        print("\nPega el TOKEN de tu bot de Telegram (te lo da @BotFather):")
        token = input("> ").strip()
    cfg["token"] = token
    save_cfg(cfg)

    tg = Telegram(token)
    try:
        me = tg.call("getMe")
        log(f"Conectado a Telegram como @{me.get('username')}")
    except Exception as e:
        log(f"ERROR: el token no funciona ({e}). Borra radar_config.json y volve a probar.")
        sys.exit(1)

    migrate(cfg, tg)
    args = R.build_parser().parse_args([])
    state = {"rows": [], "force": True, "last_scan": 0.0, "last_err": 0.0}
    if cfg.get("chat_id"):
        try:
            tg.send(cfg["chat_id"], "📡 <b>Radar encendido.</b>\n" + fmt_estado(cfg, state))
        except Exception:
            pass
    else:
        log("Abrí tu bot en Telegram y mandale /start para vincularlo.")

    while True:
        try:
            ups = tg.updates(cfg["offset"], timeout=15)
            process_updates(ups, cfg, state, tg)
            save_cfg(cfg)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Telegram: {e}")
            time.sleep(5)

        due = time.time() - state["last_scan"] >= cfg["intervalo_min"] * 60
        if state["force"] or (due and not cfg["pausado"]):
            state["force"] = False
            state["last_scan"] = time.time()
            try:
                log("Escaneando Kalshi + NBM…")
                run_scan(cfg, state, tg, args, demo=a.demo)
                save_cfg(cfg)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"ERROR en escaneo: {e}")
                if cfg.get("chat_id") and time.time() - state["last_err"] > 3600:
                    state["last_err"] = time.time()
                    try:
                        tg.send(cfg["chat_id"], f"⚠️ Falló un escaneo: {h(e)}\nSigo intentando.")
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
