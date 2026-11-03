#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RADAR CLIMATICO  ·  Kalshi x NBM edge scanner
=============================================

Que hace:
  1. Descubre TODAS las series de temperatura diaria de Kalshi (maximas KXHIGH* y minimas KXLOW*).
  2. Baja todos los mercados abiertos con sus precios (bid/ask de YES y NO).
  3. Baja el ultimo boletin NBS del National Blend of Models (NOAA) para cada estacion.
  4. Baja las observaciones reales de la estacion (api.weather.gov) desde la medianoche local estandar.
  5. Calcula la probabilidad del modelo para cada rango, la compara con el precio y descuenta comisiones.
  6. Genera un dashboard HTML (radar_climatico.html) y lo abre en el navegador.

Uso rapido:
  python radar_climatico.py                 # escaneo real
  python radar_climatico.py --demo          # dashboard con datos inventados para ver como se ve
  python radar_climatico.py --loop 15       # re-escanea cada 15 minutos y el dashboard se refresca solo

Solo usa la libreria estandar de Python 3.9+ (no hay que instalar nada).
No es consejo de inversion: es una herramienta de analisis. El modelo puede estar equivocado.
"""

import argparse
import json
import math
import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NOMADS = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
NWS = "https://api.weather.gov"
UA = "RadarClimatico/1.0 (uso personal)"
UTC = timezone.utc
INF = float("inf")

TEMP_SERIES_RE = re.compile(r"^KX(HIGH|LOW)[A-Z0-9]+$")
# Respaldo por si el descubrimiento automatico falla (se completa solo cuando funciona la API).
FALLBACK_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN",
    "KXHIGHLAX", "KXHIGHPHIL", "KXHIGHTOKC",
]
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ----------------------------------------------------------------------------- HTTP

def http_get(url, params=None, accept="application/json", timeout=40, retries=3):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            if e.code in (400, 401, 403, 404):
                raise
            last = e
        except (URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def get_json(url, params=None):
    return json.loads(http_get(url, params))


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------------------- KALSHI

def discover_series():
    found = set()
    for params in ({"category": "Climate and Weather"}, {}):
        try:
            data = get_json(f"{KALSHI}/series", params)
        except Exception as e:
            log(f"  aviso: no pude listar series ({e})")
            continue
        for s in data.get("series") or []:
            t = (s.get("ticker") or "").upper()
            if TEMP_SERIES_RE.match(t):
                found.add(t)
        if found:
            break
    if not found:
        log("  usando lista de respaldo de series")
        return list(FALLBACK_SERIES)
    return sorted(found)


def kalshi_markets(series):
    out, cursor = [], None
    for _ in range(20):
        p = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = get_json(f"{KALSHI}/markets", p)
        out += d.get("markets") or []
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def px(m, key):
    """Precio en dolares (0-1). Soporta campos nuevos *_dollars y viejos en centavos."""
    v = m.get(key + "_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = m.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


def num_field(m, *keys):
    for k in keys:
        v = m.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def valid_ask(p):
    return p is not None and 0.0 < p < 1.0


def market_bucket(m):
    """Devuelve (lo, hi) en grados F continuos, con el redondeo de Kalshi a entero."""
    st = (m.get("strike_type") or "").lower()
    fl, cp = m.get("floor_strike"), m.get("cap_strike")
    if st == "between" and fl is not None and cp is not None:
        return float(fl) - 0.5, float(cp) + 0.5
    if st == "greater" and fl is not None:
        return float(fl) + 0.5, INF
    if st == "greater_or_equal" and fl is not None:
        return float(fl) - 0.5, INF
    if st == "less" and cp is not None:
        return -INF, float(cp) - 0.5
    if st == "less_or_equal" and cp is not None:
        return -INF, float(cp) + 0.5
    # Respaldo: interpretar el texto ("64° to 65°", "72° or above", "63° or below")
    txt = f"{m.get('yes_sub_title') or ''} {m.get('subtitle') or ''}"
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", txt)]
    low = txt.lower()
    if len(nums) >= 2:
        return nums[0] - 0.5, nums[1] + 0.5
    if len(nums) == 1:
        if "above" in low or "más" in low or "more" in low or ">" in low:
            return nums[0] - 0.5, INF
        if "below" in low or "menos" in low or "less" in low or "<" in low:
            return -INF, nums[0] + 0.5
    return None


def event_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m:
        return None
    yy, mon, dd = int(m.group(1)), MONTHS.get(m.group(2)), int(m.group(3))
    if not mon:
        return None
    return date(2000 + yy, mon, dd)


def station_from_rules(m):
    txt = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}"
    s = re.search(r"\(CLI([A-Z]{3})\)", txt)
    if s:
        return "K" + s.group(1)
    s = re.search(r"\b(K[A-Z]{3})\b", txt)
    return s.group(1) if s else None


def city_from_title(title):
    t = title or ""
    for pat in (r"\bin (.+?) (?:on|today|tomorrow)\b", r"\ben (.+?) el\b", r"\bin (.+?)\?"):
        m = re.search(pat, t, re.I)
        if m:
            return m.group(1).strip()
    return None


# ----------------------------------------------------------------------------- NBM

NBS_HDR = re.compile(
    r"^\s*([A-Z0-9]{3,5})\s+NBM\s+V\S+\s+NBS\s+GUIDANCE\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{2})(\d{2})\s+UTC")
NUM_RE = re.compile(r"-?\d+")


_NBS_CACHE = {}  # url -> (texto, ciclo): evita bajar el mismo boletin en cada escaneo


def fetch_latest_nbs(now_utc, max_back=10):
    for back in range(1, max_back + 1):
        t = (now_utc - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        url = f"{NOMADS}/blend.{t:%Y%m%d}/{t:%H}/text/blend_nbstx.t{t:%H}z"
        if url in _NBS_CACHE:
            text, cyc = _NBS_CACHE[url]
            return text, cyc, url
        try:
            raw = http_get(url, accept="text/plain", timeout=180, retries=2)
            if len(raw) > 100000:
                _NBS_CACHE.clear()
                _NBS_CACHE[url] = (raw.decode("latin-1"), t)
                return _NBS_CACHE[url][0], t, url
        except HTTPError as e:
            if e.code == 404:
                continue
            raise
        except Exception:
            continue
    raise RuntimeError("No encontre un boletin NBS reciente en NOMADS")


def _row_values(line, label):
    idx = line.index(label) + len(label)
    return [(mm.end(), int(mm.group())) for mm in NUM_RE.finditer(line, idx)]


def parse_nbs(text, wanted=None):
    """
    Devuelve {station: {"issue": dt, "max": {date: (txn, xnd)}, "min": {date: (txn, xnd)}}}
    TXN maxima: ventana 12Z-06Z, informada en la columna de 00Z del dia siguiente.
    TXN minima: ventana 00Z-18Z, informada en la columna de 12Z del mismo dia.
    """
    res = {}
    cur = issue = fhr = txn = None
    tmp_line = tsd_line = None
    wanted = set(wanted) if wanted else None

    def fixed_cols(line):
        """Lee una fila por columnas fijas de 3 caracteres alineadas con FHR (evita pegar '99100')."""
        out = {}
        if not line or not fhr:
            return out
        for end, h in fhr:
            s = line[max(0, end - 3):end].strip()
            if s and s.lstrip("-").isdigit() and s != "-99":
                out[h] = int(s)
        return out

    def flush():
        if not (cur and fhr):
            return
        tmp = fixed_cols(tmp_line)
        tsd = fixed_cols(tsd_line)
        res[cur]["tmp"] = [(issue + timedelta(hours=h), v, tsd.get(h)) for h, v in sorted(tmp.items())]
        if not txn:
            return
        cols = {end: h for end, h in fhr}
        ends = sorted(cols)
        xnd_by_h = {}
        if cur_xnd:
            for end, v in cur_xnd:
                near = min(ends, key=lambda e: abs(e - end))
                xnd_by_h[cols[near]] = v
        for end, v in txn:
            if v == -99:
                continue
            near = min(ends, key=lambda e: abs(e - end))
            h = cols[near]
            valid = issue + timedelta(hours=h)
            x = xnd_by_h.get(h)
            x = None if x in (None, -99) else x
            if valid.hour == 0:
                res[cur]["max"][(valid - timedelta(days=1)).date()] = (v, x)
            elif valid.hour == 12:
                res[cur]["min"][valid.date()] = (v, x)

    cur_xnd = None
    for line in text.splitlines():
        m = NBS_HDR.match(line)
        if m:
            flush()
            sid = m.group(1)
            fhr = txn = cur_xnd = tmp_line = tsd_line = None
            if wanted is None or sid in wanted:
                cur = sid
                issue = datetime(int(m.group(4)), int(m.group(2)), int(m.group(3)),
                                 int(m.group(5)), int(m.group(6)), tzinfo=UTC)
                res[cur] = {"issue": issue, "max": {}, "min": {}, "tmp": []}
            else:
                cur = None
            continue
        if not cur:
            continue
        s = line.strip()
        if s.startswith("FHR"):
            fhr = _row_values(line, "FHR")
        elif s.startswith("TXN"):
            txn = _row_values(line, "TXN")
        elif s.startswith("XND"):
            cur_xnd = _row_values(line, "XND")
        elif s.startswith("TMP"):
            tmp_line = line
        elif s.startswith("TSD"):
            tsd_line = line
    flush()
    return res


# ----------------------------------------------------------------------------- NWS (observaciones)

def station_info(sid):
    try:
        p = get_json(f"{NWS}/stations/{sid}").get("properties", {})
        return {"tz": p.get("timeZone"), "name": p.get("name")}
    except Exception:
        return {"tz": None, "name": None}


def std_offset(tzname):
    if not tzname or ZoneInfo is None:
        return timedelta(hours=-6)
    z = ZoneInfo(tzname)
    jan = datetime(2026, 1, 15, 12, tzinfo=z).utcoffset()
    jul = datetime(2026, 7, 15, 12, tzinfo=z).utcoffset()
    return min(jan, jul)  # hora estandar (sin horario de verano)


def observations(sid, start_utc, end_utc):
    params = {"start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}
    d = get_json(f"{NWS}/stations/{sid}/observations", params)
    obs = []
    for f in d.get("features") or []:
        p = f.get("properties") or {}
        t = (p.get("temperature") or {}).get("value")
        ts = p.get("timestamp")
        if t is None or not ts:
            continue
        obs.append((datetime.fromisoformat(ts.replace("Z", "+00:00")), t * 9 / 5 + 32))
    obs.sort()
    return obs


# ----------------------------------------------------------------------------- MODELO

def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def cdf_max(t, mu, s, floor_obs):
    """P(M <= t) con M = max(observado, X), X ~ N(mu, s)."""
    if t == INF:
        return 1.0
    if t == -INF:
        return 0.0
    if floor_obs is not None and t < floor_obs:
        return 0.0
    return ncdf((t - mu) / s)


def cdf_min(t, mu, s, ceil_obs):
    """P(m <= t) con m = min(observado, X), X ~ N(mu, s)."""
    if t == INF:
        return 1.0
    if t == -INF:
        return 0.0
    if ceil_obs is not None and t >= ceil_obs:
        return 1.0
    return ncdf((t - mu) / s)


def fee(p, rate):
    """Comision taker estimada por contrato (formula general de Kalshi, redondeo hacia arriba)."""
    return math.ceil(rate * p * (1 - p) * 100 - 1e-9) / 100.0


def model_params(kind, ev_date, local_now, nbm_val, obs_ext, args, tmp=None, win=None, now_utc=None):
    """
    Devuelve (mu, sigma, limite_observado, fase) o None si no hay datos suficientes.
    obs_ext = dict(max, min, last, n) de lo observado desde la medianoche local estandar.
    tmp = pronostico de temperatura cada 3 h del NBM [(hora_utc, temp, sd)].
    win = (inicio_utc, fin_utc) del dia de Kalshi (medianoche a medianoche, hora estandar).

    Clave: la minima del dia NO siempre es a la madrugada. Con un frente frio puede ser
    a las 23:59. Por eso miramos el pronostico horario de TODO lo que falta del dia.
    """
    today = local_now.date()
    h = local_now.hour + local_now.minute / 60
    txn, xnd = nbm_val if nbm_val else (None, None)
    sig = max(args.min_sigma, (xnd or args.default_xnd) * args.sigma_inflate)
    have_obs = obs_ext and obs_ext.get("n", 0) > 0
    tmp = tmp or []
    rem, day, after_noon = [], [], []
    if win:
        d0, d1 = win
        start = max(d0, now_utc) if now_utc else d0
        rem = [v for t, v, _ in tmp if start <= t <= d1]
        day = [v for t, v, _ in tmp if d0 <= t <= d1]
        after_noon = [v for t, v, _ in tmp if d0 + timedelta(hours=12) <= t <= d1]

    if kind == "max":
        o = obs_ext.get("max") if have_obs else None
        if ev_date < today:
            return (o, 0.8, o, "cerrado (esperando CLI)") if o is not None else None
        if ev_date > today:
            if txn is not None:
                return txn, sig, None, "pronóstico NBM"
            return (max(day) + 1, sig, None, "pronóstico horario") if day else None
        cands = []
        if txn is not None and h < args.late_hour_max:
            cands.append(txn)
        if rem:
            cands.append(max(rem) + 0.5)  # cada 3 h se pierde el pico
        if not cands:
            return (o, 1.0, o, "observado (fin del día)") if o is not None else None
        mu = max(cands)
        if h >= args.late_hour_max and o is not None:
            return max(mu, o), 1.2, o, "tarde · obs + horario"
        return mu, sig, o, "en juego · NBM + obs"
    else:
        o = obs_ext.get("min") if have_obs else None
        if ev_date < today:
            return (o, 0.8, o, "cerrado (esperando CLI)") if o is not None else None
        cands = []
        if ev_date > today:
            if txn is not None:
                cands.append(txn)                 # minima de la madrugada
            if after_noon:
                cands.append(min(after_noon) - 1)  # posible caida a la noche
            return (min(cands), sig, None, "pronóstico NBM + horario") if cands else None
        if txn is not None and h < 12:
            cands.append(txn)
        if rem:
            cands.append(min(rem) - 1)  # cada 3 h se pierde el piso
        if not cands:
            return (o, 1.0, o, "observado (fin del día)") if o is not None else None
        return min(cands), sig, o, "en juego · NBM horario + obs"


# ----------------------------------------------------------------------------- ESCANEO

def scan(args):
    now = datetime.now(UTC)
    log("Descubriendo series de temperatura en Kalshi...")
    series = discover_series()
    log(f"  {len(series)} series: {', '.join(series)}")

    markets = []
    for s in series:
        try:
            ms = kalshi_markets(s)
            for m in ms:
                m["_series"] = s
            markets += ms
            log(f"  {s}: {len(ms)} mercados abiertos")
        except Exception as e:
            log(f"  {s}: error {e}")
    if not markets:
        raise RuntimeError("Kalshi no devolvio mercados abiertos")

    # Estacion por serie (de las reglas del mercado)
    st_by_series = {}
    for m in markets:
        if m["_series"] not in st_by_series:
            sid = station_from_rules(m)
            if sid:
                st_by_series[m["_series"]] = sid
    stations = sorted(set(st_by_series.values()))
    log(f"Estaciones: {', '.join(stations)}")

    log("Bajando boletin NBM (NBS) mas reciente de NOMADS...")
    nbs_text, cycle, nbs_url = fetch_latest_nbs(now)
    nbm = parse_nbs(nbs_text, stations)
    log(f"  ciclo {cycle:%Y-%m-%d %HZ} · {len(nbm)}/{len(stations)} estaciones con datos")

    info, obs_cache = {}, {}
    for sid in stations:
        info[sid] = station_info(sid)
        off = std_offset(info[sid]["tz"])
        info[sid]["off"] = off
        start = (now + off).replace(hour=0, minute=0, second=0, microsecond=0) - off - timedelta(days=1)
        try:
            obs_cache[sid] = observations(sid, start, now)
        except Exception as e:
            log(f"  {sid}: sin observaciones ({e})")
            obs_cache[sid] = []

    return build_rows(markets, st_by_series, nbm, info, obs_cache, now, args,
                      meta={"nbm_cycle": f"{cycle:%Y-%m-%d %H}Z", "nbm_url": nbs_url,
                            "series": series, "demo": False})


def obs_extremes(obs, start_utc, end_utc):
    vals = [(t, v) for t, v in obs if start_utc <= t < end_utc]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "max": max(v for _, v in vals), "min": min(v for _, v in vals),
            "last": vals[-1][1], "last_t": vals[-1][0].isoformat()}


def build_rows(markets, st_by_series, nbm, info, obs_cache, now, args, meta):
    rows, skipped = [], {}
    events = {}
    for m in markets:
        series = m["_series"]
        kind = "max" if series.startswith("KXHIGH") else "min"
        sid = st_by_series.get(series)
        evd = event_date(m.get("event_ticker"))
        bk = market_bucket(m)
        if not sid or not evd or not bk:
            skipped["sin estación/fecha/rango"] = skipped.get("sin estación/fecha/rango", 0) + 1
            continue
        off = info.get(sid, {}).get("off", timedelta(hours=-6))
        local_now = (now + off).replace(tzinfo=None)
        day_start = datetime(evd.year, evd.month, evd.day, tzinfo=UTC) - off
        ext = obs_extremes(obs_cache.get(sid, []), day_start, min(now, day_start + timedelta(days=1)))
        nbm_val = (nbm.get(sid) or {}).get(kind, {}).get(evd)
        mp = model_params(kind, evd, local_now, nbm_val, ext, args,
                          tmp=(nbm.get(sid) or {}).get("tmp"),
                          win=(day_start, day_start + timedelta(days=1)), now_utc=now)
        if not mp or mp[0] is None:
            skipped["sin datos de modelo"] = skipped.get("sin datos de modelo", 0) + 1
            continue
        mu, sig, lim, phase = mp
        lo, hi = bk
        F = cdf_max if kind == "max" else cdf_min
        p = max(0.0, min(1.0, F(hi, mu, sig, lim) - F(lo, mu, sig, lim)))

        ya, yb = px(m, "yes_ask"), px(m, "yes_bid")
        na = px(m, "no_ask")
        if not valid_ask(na) and yb is not None and 0 < yb < 1:
            na = 1 - yb
        ev_yes = p - ya - fee(ya, args.fee_rate) if valid_ask(ya) else None
        ev_no = (1 - p) - na - fee(na, args.fee_rate) if valid_ask(na) else None
        best_side, best_ev, cost = None, None, None
        for side, ev, c in (("YES", ev_yes, ya), ("NO", ev_no, na)):
            if ev is not None and (best_ev is None or ev > best_ev):
                best_side, best_ev, cost = side, ev, c
        mid = None
        if yb is not None and valid_ask(ya) and yb > 0:
            mid = (yb + ya) / 2
        elif valid_ask(ya):
            mid = ya
        spread = (ya - yb) if (valid_ask(ya) and yb is not None) else None

        cs = re.search(r"City:\s*([^;]+)", str(m.get("custom_strike") or ""))
        city = (cs.group(1).strip() if cs else None) or city_from_title(m.get("title")) \
            or info.get(sid, {}).get("name") or sid
        vol = num_field(m, "volume", "volume_fp", "volume_24h_fp")
        oi = num_field(m, "open_interest", "open_interest_fp")
        # Guardrail: si el modelo y el precio difieren por una barbaridad, casi siempre
        # es el modelo el que no ve algo (frente, dato faltante). Se marca para revisar.
        side_p = (p if best_side == "YES" else 1 - p) if best_side else None
        suspect = bool(best_side and cost is not None and abs(side_p - cost) > args.max_gap)
        label = m.get("yes_sub_title") or m.get("subtitle") or (
            f"{lo + 0.5:.0f}°–{hi - 0.5:.0f}°" if lo > -INF and hi < INF else
            (f"≤ {hi - 0.5:.0f}°" if lo == -INF else f"≥ {lo + 0.5:.0f}°"))
        row = {
            "series": series, "event": m.get("event_ticker"), "ticker": m.get("ticker"),
            "city": city, "station": sid, "kind": kind, "date": evd.isoformat(),
            "label": label, "lo": None if lo == -INF else lo, "hi": None if hi == INF else hi,
            "p": round(p, 4), "ya": ya, "yb": yb, "na": na, "mid": mid, "spread": spread,
            "ev_yes": ev_yes, "ev_no": ev_no, "side": best_side, "ev": best_ev,
            "roi": (best_ev / cost) if (best_ev is not None and cost) else None,
            "vol": int(vol), "oi": int(oi), "suspect": suspect,
            "mu": round(mu, 1), "sigma": round(sig, 2),
            "obs": round(lim, 1) if lim is not None else None,
            "nbm": nbm_val[0] if nbm_val else None, "phase": phase,
            "liquid": bool(spread is not None and spread <= args.max_spread and vol >= args.min_volume),
            "url": f"https://kalshi.com/markets/{series.lower()}",
        }
        rows.append(row)
        events.setdefault(row["event"], 0)
        events[row["event"]] += 1

    rows.sort(key=lambda r: (r["ev"] if r["ev"] is not None else -9), reverse=True)
    meta.update({
        "generated": now.isoformat(), "n_markets": len(markets), "n_rows": len(rows),
        "n_events": len(events), "skipped": skipped, "min_edge": args.min_edge,
        "fee_rate": args.fee_rate, "max_gap": args.max_gap, "sigma_inflate": args.sigma_inflate, "min_sigma": args.min_sigma,
        "refresh_min": args.loop or 0,
    })
    return rows, meta


# ----------------------------------------------------------------------------- DEMO

def demo_data(args):
    """Datos inventados para ver el dashboard sin conexion. NO son precios reales."""
    import random
    random.seed(7)
    now = datetime.now(UTC)
    cities = [("KXHIGHNY", "New York City", "KNYC", -5, 58), ("KXHIGHCHI", "Chicago", "KMDW", -6, 49),
              ("KXHIGHMIA", "Miami", "KMIA", -5, 84), ("KXHIGHAUS", "Austin", "KAUS", -6, 77),
              ("KXHIGHDEN", "Denver", "KDEN", -7, 55), ("KXHIGHLAX", "Los Angeles", "KLAX", -8, 71),
              ("KXHIGHTOKC", "Oklahoma City", "KOKC", -6, 61), ("KXLOWTCHI", "Chicago", "KMDW", -6, 36)]
    markets, st_by_series, nbm, info, obs_cache = [], {}, {}, {}, {}
    for series, city, sid, offh, base in cities:
        kind = "max" if series.startswith("KXHIGH") else "min"
        st_by_series[series] = sid
        off = timedelta(hours=offh)
        info[sid] = {"off": off, "name": city}
        nbm.setdefault(sid, {"issue": now, "max": {}, "min": {}})
        local_today = (now + off).date()
        for dd in (0, 1):
            evd = local_today + timedelta(days=dd)
            txn = base + random.randint(-2, 2)
            nbm[sid][kind][evd] = (txn, 2)
            market_center = txn + random.choice([-2, -1, 0, 0, 1, 2])
            tag = f"{evd:%y}{list(MONTHS)[evd.month - 1]}{evd:%d}"
            start = (market_center // 2) * 2 - 6
            for k in range(6):
                lo_b = start + 2 * k
                fl, cp = lo_b, lo_b + 1
                if k == 0:
                    st, lo, hi = "less", -INF, fl + 2 - 0.5
                elif k == 5:
                    st, lo, hi = "greater", fl - 1 + 0.5, INF
                else:
                    st, lo, hi = "between", fl - 0.5, cp + 0.5
                pm = cdf_max(hi, market_center, 2.2, None) - cdf_max(lo, market_center, 2.2, None)
                ya = min(0.99, max(0.01, round(pm + random.uniform(0.0, 0.04), 2)))
                yb = max(0.0, round(ya - random.choice([0.01, 0.01, 0.02, 0.04]), 2))
                m = {"_series": series, "event_ticker": f"{series}-{tag}", "ticker": f"{series}-{tag}-{k}",
                     "title": f"Highest temperature in {city} on {evd:%b %d}?" if kind == "max"
                     else f"Lowest temperature in {city} on {evd:%b %d}?",
                     "strike_type": st, "yes_ask": ya * 100, "yes_bid": yb * 100, "no_ask": (1 - yb) * 100,
                     "volume": random.randint(40, 9000), "open_interest": random.randint(20, 4000)}
                if st == "less":
                    m["cap_strike"] = fl + 2; m["yes_sub_title"] = f"{fl + 1}° or below"
                elif st == "greater":
                    m["floor_strike"] = fl - 1; m["yes_sub_title"] = f"{fl}° or above"
                else:
                    m["floor_strike"] = fl; m["cap_strike"] = cp; m["yes_sub_title"] = f"{fl}° to {cp}°"
                markets.append(m)
        obs_cache[sid] = []
    return build_rows(markets, st_by_series, nbm, info, obs_cache, now, args,
                      meta={"nbm_cycle": "DEMO", "nbm_url": "", "series": [c[0] for c in cities], "demo": True})


# ----------------------------------------------------------------------------- DASHBOARD

HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
__REFRESH__
<title>Radar Climático</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#060a12;--panel:#0c1320;--panel2:#101a2b;--line:#1b2940;--txt:#dbe7f7;--mute:#7d8ca6;
--cy:#22d3ee;--gr:#34f5a4;--rd:#ff5d7a;--am:#ffc857;--vi:#a78bfa;--glow:0 0 24px rgba(34,211,238,.18)}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--txt);font-family:"Space Grotesk",system-ui,sans-serif}
body{background-image:radial-gradient(circle at 15% -10%,rgba(34,211,238,.10),transparent 40%),radial-gradient(circle at 100% 0,rgba(167,139,250,.08),transparent 35%),
linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:auto,auto,32px 32px,32px 32px}
.wrap{max-width:1280px;margin:0 auto;padding:20px 16px 60px}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:12px}.logo{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
background:conic-gradient(from 200deg,var(--cy),var(--vi),var(--gr),var(--cy));box-shadow:var(--glow)}
.logo span{width:34px;height:34px;border-radius:9px;background:var(--bg);display:grid;place-items:center;font-size:18px}
h1{font-size:20px;margin:0;letter-spacing:.06em}h1 b{color:var(--cy)}.sub{color:var(--mute);font-size:12px}
.pill{border:1px solid var(--line);background:var(--panel);padding:6px 10px;border-radius:999px;font-size:12px;color:var(--mute)}
.pill i{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--gr);margin-right:6px;box-shadow:0 0 10px var(--gr);animation:p 1.6s infinite}
@keyframes p{50%{opacity:.35}}
.demo{background:rgba(255,200,87,.12);border:1px solid rgba(255,200,87,.4);color:var(--am);padding:10px 14px;border-radius:12px;margin-bottom:14px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.kpi{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.kpi .l{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.1em}.kpi .v{font-size:24px;font-weight:700;margin-top:4px}
.sec{display:flex;align-items:baseline;justify-content:space-between;margin:22px 0 10px}.sec h2{font-size:14px;letter-spacing:.14em;text-transform:uppercase;margin:0;color:var(--mute)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px;overflow:hidden;transition:.2s}
.card:hover{border-color:var(--cy);box-shadow:var(--glow);transform:translateY(-2px)}
.card .rank{position:absolute;top:10px;right:12px;font-size:11px;color:var(--mute)}
.card .city{font-weight:700;font-size:16px}.card .meta{font-size:12px;color:var(--mute);margin:2px 0 10px}
.card .bk{font-size:20px;font-weight:700}.side{display:inline-block;font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;margin-left:8px;vertical-align:middle}
.side.YES{background:rgba(52,245,164,.14);color:var(--gr);border:1px solid rgba(52,245,164,.4)}.side.NO{background:rgba(255,93,122,.12);color:var(--rd);border:1px solid rgba(255,93,122,.4)}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:12px}.grid3 div{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:7px}
.grid3 .l{font-size:10px;color:var(--mute);text-transform:uppercase}.grid3 .v{font-size:15px;font-weight:600}
.bar{height:6px;background:var(--bg);border-radius:4px;margin-top:10px;position:relative;overflow:hidden}
.bar .m{position:absolute;inset:0 auto 0 0;background:var(--mute);opacity:.5}.bar .p{position:absolute;inset:0 auto 0 0;background:linear-gradient(90deg,var(--cy),var(--gr));height:3px;top:auto}
.pos{color:var(--gr)}.neg{color:var(--rd)}.amb{color:var(--am)}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:10px}
.filters input[type=search]{flex:1;min-width:160px;background:var(--bg);border:1px solid var(--line);color:var(--txt);padding:8px 10px;border-radius:10px;font:inherit}
.seg{display:flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}.seg button{background:transparent;color:var(--mute);border:0;padding:7px 10px;font:inherit;font-size:12px;cursor:pointer}
.seg button.on{background:rgba(34,211,238,.14);color:var(--cy)}
.rng{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mute)}.rng input{accent-color:var(--cy)}
label.chk{font-size:12px;color:var(--mute);display:flex;gap:6px;align-items:center}
.tbl{overflow:auto;border:1px solid var(--line);border-radius:14px;margin-top:10px;background:var(--panel)}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--panel2);color:var(--mute);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.08em;cursor:pointer;user-select:none}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}tr:hover td{background:rgba(34,211,238,.04)}
a{color:var(--cy);text-decoration:none}
.events{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.ev{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px}.ev h3{margin:0;font-size:15px}.ev .meta{font-size:12px;color:var(--mute);margin-bottom:10px}
.brow{display:grid;grid-template-columns:86px 1fr 92px;gap:8px;align-items:center;font-size:12px;margin:5px 0}
.bars{position:relative;height:16px}.bars .mk{position:absolute;left:0;top:0;height:7px;background:#3a4a66;border-radius:3px}
.bars .md{position:absolute;left:0;bottom:0;height:7px;background:linear-gradient(90deg,var(--cy),var(--gr));border-radius:3px}
.legend{display:flex;gap:14px;font-size:11px;color:var(--mute)}.legend span:before{content:"";display:inline-block;width:10px;height:6px;border-radius:2px;margin-right:5px;vertical-align:middle}
.legend .a:before{background:#3a4a66}.legend .b:before{background:var(--cy)}
.notes{margin-top:26px;color:var(--mute);font-size:12px;line-height:1.6;border-top:1px solid var(--line);padding-top:14px}
.empty{color:var(--mute);padding:20px;text-align:center}
@media(max-width:600px){.kpi .v{font-size:20px}h1{font-size:17px}}
</style></head><body><div class="wrap">
<header><div class="brand"><div class="logo"><span>📡</span></div><div><h1>RADAR <b>CLIMÁTICO</b></h1>
<div class="sub">Kalshi × National Blend of Models · edges en mercados de temperatura</div></div></div>
<div style="display:flex;gap:8px;flex-wrap:wrap"><span class="pill mono" id="gen"></span><span class="pill mono" id="cyc"></span></div></header>
<div id="demo"></div>
<div class="kpis" id="kpis"></div>
<div class="sec"><h2>🎯 Top edges</h2><div class="legend"><span class="a">Mercado</span><span class="b">Modelo</span></div></div>
<div class="cards" id="cards"></div>
<div class="sec"><h2>🧮 Todos los mercados</h2><span class="sub" id="count"></span></div>
<div class="filters">
<input type="search" id="q" placeholder="Buscar ciudad, estación o ticker…">
<div class="seg" id="kind"><button data-v="all" class="on">Todas</button><button data-v="max">Máximas</button><button data-v="min">Mínimas</button></div>
<div class="seg" id="side"><button data-v="all" class="on">YES + NO</button><button data-v="YES">YES</button><button data-v="NO">NO</button></div>
<div class="rng">Edge mín <input type="range" id="me" min="-10" max="30" step="1"><b class="mono" id="mev"></b></div>
<label class="chk"><input type="checkbox" id="liq"> Solo líquidos</label>
</div>
<div class="tbl"><table><thead><tr>
<th data-k="city">Ciudad</th><th data-k="label">Rango</th><th data-k="date">Fecha</th><th data-k="p">Modelo</th><th data-k="mid">Mercado</th>
<th data-k="side">Lado</th><th data-k="cost">Precio</th><th data-k="ev">Edge neto</th><th data-k="roi">ROI</th><th data-k="vol">Volumen</th><th data-k="spread">Spread</th><th data-k="phase">Fase</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<div class="sec"><h2>🌡️ Explorador por evento</h2><span class="sub">distribución del modelo vs precio por rango</span></div>
<div class="events" id="events"></div>
<div class="notes" id="notes"></div>
</div>
<script>
const DATA=__DATA__;const META=__META__;
const S={kind:"all",side:"all",me:Math.round((META.min_edge||0.03)*100),liq:false,q:"",k:"ev",dir:-1};
const $=s=>document.querySelector(s);
const c=x=>x==null?"—":(x*100).toFixed(0)+"¢";
const pc=x=>x==null?"—":(x*100).toFixed(1)+"%";
const ed=x=>x==null?"—":`<span class="${x>0?"pos":"neg"}">${x>0?"+":""}${(x*100).toFixed(1)}¢</span>`;
const esc=s=>String(s??"").replace(/[&<>"]/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch]));
DATA.forEach(r=>{r.cost=r.side==="YES"?r.ya:r.side==="NO"?r.na:null});
function fdate(d){const [y,m,dd]=d.split("-");return dd+"/"+m}
function filt(){const q=S.q.toLowerCase();return DATA.filter(r=>(S.kind==="all"||r.kind===S.kind)&&(S.side==="all"||r.side===S.side)
&&(r.ev!=null&&r.ev*100>=S.me)&&(!S.liq||r.liquid)&&(!q||(r.city+" "+r.station+" "+r.ticker+" "+r.label).toLowerCase().includes(q)))}
function kpis(){const pos=DATA.filter(r=>r.ev!=null&&r.ev>=(META.min_edge||0));const best=DATA.find(r=>r.ev!=null);
const cities=new Set(DATA.map(r=>r.station));
const k=[["Mercados escaneados",META.n_markets],["Eventos",META.n_events],["Ciudades",cities.size],
["Edges ≥ "+Math.round((META.min_edge||0)*100)+"¢",`<span class="pos">${pos.length}</span>`],
["Mejor edge",best?ed(best.ev):"—"],["Líquidos con edge",pos.filter(r=>r.liquid).length]];
$("#kpis").innerHTML=k.map(([l,v])=>`<div class="kpi"><div class="l">${l}</div><div class="v mono">${v}</div></div>`).join("")}
function cards(){const top=DATA.filter(r=>r.ev!=null&&r.ev>0&&!r.suspect&&(!S.liq||r.liquid)&&(S.kind==="all"||r.kind===S.kind)).slice(0,9);
if(!top.length){$("#cards").innerHTML='<div class="empty">Sin edges positivos ahora mismo. El mercado está eficiente 😎</div>';return}
$("#cards").innerHTML=top.map((r,i)=>{const pm=r.side==="YES"?r.p:1-r.p;return `<a class="card" href="${r.url}" target="_blank" rel="noopener">
<div class="rank mono">#${i+1}</div><div class="city">${esc(r.city)} <span class="sub mono">${r.station}</span></div>
<div class="meta">${r.kind==="max"?"🔺 Máxima":"🔻 Mínima"} · ${fdate(r.date)} · ${esc(r.phase)}</div>
<div class="bk">${esc(r.label)}<span class="side ${r.side}">${r.side}</span></div>
<div class="grid3 mono"><div><div class="l">Precio</div><div class="v">${c(r.cost)}</div></div>
<div><div class="l">Modelo</div><div class="v">${pc(pm)}</div></div><div><div class="l">Edge</div><div class="v">${ed(r.ev)}</div></div></div>
<div class="bar"><div class="m" style="width:${(r.cost||0)*100}%"></div><div class="p" style="width:${pm*100}%"></div></div>
<div class="meta mono" style="margin:8px 0 0">μ ${r.mu}° · σ ${r.sigma}° · obs ${r.obs??"—"}° · vol ${r.vol.toLocaleString()}${r.liquid?"":" · ⚠️ ilíquido"}</div></a>`}).join("")}
function table(){let rows=filt();const k=S.k,d=S.dir;rows.sort((a,b)=>{let x=a[k],y=b[k];if(x==null)return 1;if(y==null)return -1;return (x>y?1:x<y?-1:0)*d});
$("#count").textContent=rows.length+" de "+DATA.length+" mercados";
$("#tb").innerHTML=rows.length?rows.map(r=>`<tr><td><a href="${r.url}" target="_blank" rel="noopener">${esc(r.city)}</a> <span class="sub mono">${r.kind==="max"?"▲":"▼"} ${r.station}</span></td>
<td><b>${esc(r.label)}</b>${r.suspect?' <span class="amb" title="Modelo y precio difieren demasiado: revisar">🧪 revisar</span>':""}</td><td class="mono">${fdate(r.date)}</td><td class="mono">${pc(r.p)}</td><td class="mono">${pc(r.mid)}</td>
<td>${r.side?`<span class="side ${r.side}">${r.side}</span>`:"—"}</td><td class="mono">${c(r.cost)}</td><td class="mono">${ed(r.ev)}</td>
<td class="mono">${r.roi==null?"—":`<span class="${r.roi>0?"pos":"neg"}">${(r.roi*100).toFixed(0)}%</span>`}</td>
<td class="mono">${r.vol.toLocaleString()}</td><td class="mono ${r.spread!=null&&r.spread>0.05?"amb":""}">${c(r.spread)}</td><td class="sub">${esc(r.phase)}</td></tr>`).join("")
:'<tr><td colspan="12" class="empty">Nada con estos filtros</td></tr>'}
function events(){const g={};DATA.forEach(r=>{(g[r.event]=g[r.event]||[]).push(r)});
const list=Object.values(g).filter(rs=>S.kind==="all"||rs[0].kind===S.kind).sort((a,b)=>Math.max(...b.map(r=>r.ev??-9))-Math.max(...a.map(r=>r.ev??-9)));
$("#events").innerHTML=list.map(rs=>{rs.sort((a,b)=>(a.lo??-999)-(b.lo??-999));const r0=rs[0];const best=Math.max(...rs.map(r=>r.ev??-9));
const sumM=rs.reduce((s,r)=>s+(r.mid||0),0);
return `<div class="ev"><h3>${esc(r0.city)} · ${r0.kind==="max"?"Máxima":"Mínima"} ${fdate(r0.date)}</h3>
<div class="meta mono">NBM ${r0.nbm??"—"}° · μ ${r0.mu}° σ ${r0.sigma}° · obs ${r0.obs??"—"}° · Σ mercado ${(sumM*100).toFixed(0)}% · mejor edge ${ed(best>-9?best:null)}</div>
${rs.map(r=>`<div class="brow"><div class="mono">${esc(r.label)}</div><div class="bars"><div class="mk" style="width:${(r.mid||0)*100}%"></div><div class="md" style="width:${r.p*100}%"></div></div>
<div class="mono" style="text-align:right">${pc(r.p)} <span class="sub">vs</span> ${c(r.mid)}</div></div>`).join("")}</div>`}).join("")}
function notes(){const sk=Object.entries(META.skipped||{}).map(([k,v])=>`${v} ${k}`).join(" · ")||"ninguno";
$("#notes").innerHTML=`<b>Cómo leerlo.</b> <b>Modelo</b> = probabilidad de que el rango gane según NBM + lo ya observado. <b>Edge neto</b> = probabilidad del modelo − precio − comisión estimada (${META.fee_rate}·P·(1−P) por contrato). <b>ROI</b> = edge / precio.<br>
<b>Modelo.</b> Máxima del día = máx(observado desde medianoche hora estándar, X), con X ~ Normal(TXN del NBM, σ). σ = máx(${META.min_sigma}°, XND × ${META.sigma_inflate}). Mínima análoga con mín(). Días cerrados usan lo observado hasta que sale el reporte oficial.<br>
<b>🧪 revisar</b> = el modelo y el precio difieren más de ${Math.round((META.max_gap||0.4)*100)} puntos: casi siempre falta un dato (frente, pronóstico). No se alerta ni entra en el Top.<br><b>Ojo.</b> La ventana de máximas del NBM es 12Z–06Z y la de mínimas 00Z–18Z: no coinciden exacto con el día de Kalshi (medianoche a medianoche, hora estándar). Las observaciones de la API son horarias y pueden no captar el pico de 1 minuto. Frentes, nubes y redondeo °C→°F mueven todo. Verificá siempre las reglas del mercado. No es consejo de inversión.<br>
<span class="mono">Series: ${(META.series||[]).join(", ")} · omitidos: ${sk}</span>`}
function all(){kpis();cards();table();events();notes()}
$("#gen").textContent="⏱ "+new Date(META.generated).toLocaleString();
$("#cyc").innerHTML='<i></i>NBM '+META.nbm_cycle;
if(META.demo)$("#demo").innerHTML='<div class="demo">⚠️ MODO DEMO — precios y pronósticos inventados solo para ver el diseño. Corré el script sin --demo para datos reales.</div>';
$("#me").value=S.me;$("#mev").textContent=S.me+"¢";
$("#q").oninput=e=>{S.q=e.target.value;table()};
$("#me").oninput=e=>{S.me=+e.target.value;$("#mev").textContent=S.me+"¢";table()};
$("#liq").onchange=e=>{S.liq=e.target.checked;all()};
document.querySelectorAll(".seg").forEach(sg=>sg.addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;
sg.querySelectorAll("button").forEach(x=>x.classList.remove("on"));b.classList.add("on");S[sg.id]=b.dataset.v;all()}));
document.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;S.dir=S.k===k?-S.dir:-1;S.k=k;table()});
all();
</script></body></html>"""


def render_dashboard(rows, meta, out_path):
    def js(obj):
        return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")
    refresh = (f'<meta http-equiv="refresh" content="{int(meta["refresh_min"] * 60)}">'
               if meta.get("refresh_min") else "")
    html = HTML.replace("__DATA__", js(rows)).replace("__META__", js(meta)).replace("__REFRESH__", refresh)
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path


def print_top(rows, n=12):
    top = [r for r in rows if r["ev"] is not None][:n]
    if not top:
        print("Sin mercados evaluables.")
        return
    print("\n  TOP EDGES")
    print("  " + "-" * 92)
    for r in top:
        print(f"  {r['city'][:18]:<18} {r['kind']:<3} {r['date']}  {r['label'][:16]:<16} "
              f"{r['side'] or '-':<3} precio {r['ya'] if r['side'] == 'YES' else r['na'] or 0:>5.2f}  "
              f"modelo {r['p']:>5.1%}  edge {r['ev'] * 100:+5.1f}c  {r['phase']}")
    print()


# ----------------------------------------------------------------------------- MAIN

def build_parser():
    ap = argparse.ArgumentParser(description="Radar Climatico: edges Kalshi x NBM")
    ap.add_argument("--out", default="radar_climatico.html")
    ap.add_argument("--demo", action="store_true", help="datos inventados para ver el dashboard")
    ap.add_argument("--loop", type=float, default=0, help="re-escanear cada N minutos")
    ap.add_argument("--no-open", action="store_true", help="no abrir el navegador")
    ap.add_argument("--min-edge", type=float, default=0.05, help="edge minimo destacado (0.05 = 5c)")
    ap.add_argument("--fee-rate", type=float, default=0.07, help="tasa de comision taker")
    ap.add_argument("--sigma-inflate", type=float, default=1.3, help="multiplica el XND del NBM")
    ap.add_argument("--min-sigma", type=float, default=2.0, help="sigma minima en grados F")
    ap.add_argument("--default-xnd", type=float, default=2.5, help="XND si el NBM no lo trae")
    ap.add_argument("--late-hour-max", type=float, default=17.0, help="hora local desde la que la maxima se toma de lo observado")
    ap.add_argument("--late-hour-min", type=float, default=10.0, help="hora local desde la que la minima se toma de lo observado")
    ap.add_argument("--max-spread", type=float, default=0.05, help="spread maximo para considerar liquido")
    ap.add_argument("--min-volume", type=int, default=200, help="volumen minimo para considerar liquido")
    ap.add_argument("--max-gap", type=float, default=0.40,
                    help="si modelo y precio difieren mas que esto, se marca como sospechoso y no se alerta")
    return ap


def main():
    args = build_parser().parse_args()

    opened = False
    while True:
        try:
            rows, meta = demo_data(args) if args.demo else scan(args)
            out = render_dashboard(rows, meta, args.out)
            print_top(rows)
            log(f"Dashboard listo: {Path(out).resolve()}")
            if not opened and not args.no_open:
                webbrowser.open(Path(out).resolve().as_uri())
                opened = True
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"ERROR: {e}")
            if not args.loop:
                sys.exit(1)
        if not args.loop:
            break
        log(f"Proximo escaneo en {args.loop:g} min (Ctrl+C para salir)")
        try:
            time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
