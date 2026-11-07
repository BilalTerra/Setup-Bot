import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
TG_API = f"https://api.telegram.org/bot{TOKEN}"
OKX = "https://www.okx.com/api/v5/market"
STATE_FILE = "state.json"

# ---- Einstellungen ----
LEVEL = 0.882            # Fibonacci-Level
TOP_N = 100              # Anzahl Coins (nach 24h-Handelsvolumen)
TOLERANCE_PCT = 0.1      # wie nah (in %) der Kurs am Level sein muss
MIN_RANGE_PCT = 2.0      # Tagesrange mindestens so groß (in %), sonst kein Setup
TIMEFRAMES = ["1H", "4H"]
LIVE_KERZE = True        # True = auch die laufende Kerze prüfen (schneller), False = nur geschlossene
IGNORIEREN = {
    "USDC", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "USDG", "USDD", "EURT",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "BETH", "USDT",
}


def http_get(url, data=None):
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": "Mozilla/5.0 (trading-bot/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def senden(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    http_get(f"{TG_API}/sendMessage", data)


def top_coins():
    d = http_get(f"{OKX}/tickers?instType=SPOT")["data"]
    paare = []
    for t in d:
        if not t["instId"].endswith("-USDT"):
            continue
        basis = t["instId"].split("-")[0]
        if basis.upper() in IGNORIEREN:
            continue
        paare.append((float(t.get("volCcy24h") or 0), t["instId"]))
    paare.sort(reverse=True)
    return [p[1] for p in paare[:TOP_N]]


def kerzen(inst, bar, limit):
    d = http_get(f"{OKX}/candles?instId={inst}&bar={bar}&limit={limit}")
    if d.get("code") != "0":
        return []
    # OKX liefert neueste zuerst: [ts, o, h, l, c, ...., confirm]
    out = []
    for k in reversed(d["data"]):
        out.append({
            "ts": int(k[0]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "closed": k[8] == "1",
        })
    return out


def laden():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"gemeldet": []}


def speichern(state, heute):
    state["gemeldet"] = [k for k in state["gemeldet"] if heute in k]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def pruefen(inst, heute, tag_start_ms, state):
    stunden = kerzen(inst, "1H", 48)
    tag = [k for k in stunden if k["ts"] >= tag_start_ms]
    if len(tag) < 2:
        return
    hoch = max(tag, key=lambda k: k["h"])
    tief = min(tag, key=lambda k: k["l"])
    spanne = hoch["h"] - tief["l"]
    if tief["l"] <= 0 or spanne / tief["l"] * 100 < MIN_RANGE_PCT:
        return

    if tief["ts"] < hoch["ts"]:
        richtung = "LONG"
        level = hoch["h"] - LEVEL * spanne
        extrem_ts = hoch["ts"]
    else:
        richtung = "SHORT"
        level = tief["l"] + LEVEL * spanne
        extrem_ts = tief["ts"]

    tol = level * TOLERANCE_PCT / 100
    for tf in TIMEFRAMES:
        k = tag if tf == "1H" else kerzen(inst, tf, 10)
        geschlossen = [x for x in k if (LIVE_KERZE or x["closed"]) and x["ts"] > extrem_ts]
        if not geschlossen:
            continue
        letzte = geschlossen[-1]
        if letzte["l"] - tol <= level <= letzte["h"] + tol:
            key = f"{inst}|{richtung}|{heute}|{tf}"
            if key in state["gemeldet"]:
                continue
            state["gemeldet"].append(key)
            senden(
                f"{'🟢' if richtung == 'LONG' else '🔴'} Mögliches {richtung}-Setup\n"
                f"{inst.replace('-USDT', '')} | Timeframe {tf}\n"
                f"Fib {LEVEL} bei {level:.6g}\n"
                f"Tageshoch {hoch['h']:.6g} | Tagestief {tief['l']:.6g}\n"
                f"Letzter Kurs {letzte['c']:.6g}"
            )


def main():
    jetzt = datetime.now(timezone.utc)
    heute = jetzt.strftime("%Y-%m-%d")
    tag_start_ms = int(jetzt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    state = laden()
    for inst in top_coins():
        try:
            pruefen(inst, heute, tag_start_ms, state)
        except Exception as e:
            print(f"Fehler bei {inst}: {e}", file=sys.stderr)
        time.sleep(0.15)
    speichern(state, heute)


if __name__ == "__main__":
    main()
