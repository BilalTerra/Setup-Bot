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
# Top 20 ohne Memecoins und Stablecoins. Hier kannst du Coins ändern.
COINS = [
    "BTC", "ETH", "XRP", "BNB", "SOL", "TRX", "ADA", "LINK", "AVAX", "XLM",
    "HBAR", "BCH", "LTC", "DOT", "SUI", "TON", "UNI", "NEAR", "AAVE", "ETC",
]
LEVEL = 0.882            # Fibonacci-Level
TOLERANCE_PCT = 0.15     # wie nah (in %) die Kerze ans Level kommen muss
MIN_RANGE_PCT = 3.0      # Tagesrange mindestens so groß (in %), sonst kein Setup
TIMEFRAMES = ["4H"]      # 4H ist besser als 1H. Für 1H zusätzlich: ["4H", "1H"]
LIVE_KERZE = False       # False = nur geschlossene Kerzen (sauberer), True = schneller


def http_get(url, data=None):
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": "Mozilla/5.0 (trading-bot/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def senden(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    http_get(f"{TG_API}/sendMessage", data)


def kerzen(inst, bar, limit):
    d = http_get(f"{OKX}/candles?instId={inst}&bar={bar}&limit={limit}")
    if d.get("code") != "0":
        return []
    # OKX liefert neueste zuerst: [ts, o, h, l, c, ...., confirm]
    out = []
    for k in reversed(d["data"]):
        out.append({
            "ts": int(k[0]), "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]), "closed": k[8] == "1",
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
        richtung = "LONG"                      # Aufwärtsbewegung, Rücklauf nach unten
        level = hoch["h"] - LEVEL * spanne
        extrem_ts = hoch["ts"]
        stop = tief["l"]
        ziel1 = hoch["h"] - 0.5 * spanne
        ziel2 = hoch["h"]
    else:
        richtung = "SHORT"                     # Abwärtsbewegung, Rücklauf nach oben
        level = tief["l"] + LEVEL * spanne
        extrem_ts = tief["ts"]
        stop = hoch["h"]
        ziel1 = tief["l"] + 0.5 * spanne
        ziel2 = tief["l"]

    tol = level * TOLERANCE_PCT / 100
    for tf in TIMEFRAMES:
        k = kerzen(inst, tf, 10)
        passend = [x for x in k if (LIVE_KERZE or x["closed"]) and x["ts"] > extrem_ts]
        if not passend:
            continue
        c = passend[-1]
        # Ablehnung am Level: Kerze berührt das Level und schließt auf der richtigen Seite
        if richtung == "LONG":
            ok = c["l"] <= level + tol and c["c"] > level
        else:
            ok = c["h"] >= level - tol and c["c"] < level
        if not ok:
            continue
        key = f"{inst}|{richtung}|{heute}|{tf}"
        if key in state["gemeldet"]:
            continue
        state["gemeldet"].append(key)
        risiko = abs(c["c"] - stop)
        crv = abs(ziel2 - c["c"]) / risiko if risiko > 0 else 0
        senden(
            f"{'🟢' if richtung == 'LONG' else '🔴'} {richtung}-Setup (Fib {LEVEL}) | {tf}\n"
            f"{inst.replace('-USDT', '')}\n"
            f"Level: {level:.6g}\n"
            f"Kerze schloss bei: {c['c']:.6g}\n"
            f"Stop (Tagesextrem): {stop:.6g}\n"
            f"Ziel 1 (0,5): {ziel1:.6g} | Ziel 2: {ziel2:.6g}\n"
            f"CRV bis Ziel 2: {crv:.1f}\n"
            f"Nur eine Idee, keine Empfehlung."
        )


def main():
    jetzt = datetime.now(timezone.utc)
    heute = jetzt.strftime("%Y-%m-%d")
    tag_start_ms = int(jetzt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    state = laden()
    for coin in COINS:
        inst = f"{coin}-USDT"
        try:
            pruefen(inst, heute, tag_start_ms, state)
        except Exception as e:
            print(f"Fehler bei {inst}: {e}", file=sys.stderr)
        time.sleep(0.15)
    speichern(state, heute)


if __name__ == "__main__":
    main()
