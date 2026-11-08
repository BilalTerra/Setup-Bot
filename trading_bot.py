import json
import os
import sys
import time
import urllib.parse
import urllib.request

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
TG_API = f"https://api.telegram.org/bot{TOKEN}"
OKX = "https://www.okx.com/api/v5/market"
STATE_FILE = "state.json"

# ---- Einstellungen ----
TOP_N = 20               # Anzahl Coins (nach 24h-Handelsvolumen auf OKX)
LEVEL = 0.882            # Fibonacci-Level
NAEHE_PCT = 0.3          # "kurz davor": so nah (in %) am Level wird gemeldet
PIVOT = 3                # Hoch/Tief gilt, wenn es 3 Kerzen links und rechts überragt
MIN_BEWEGUNG_PCT = 0     # 0 = keine Mindestgröße für den Swing
TIMEFRAMES = ["4H", "1H"]  # 4H zuerst: hat Vorrang vor 1H

# Stablecoins, Wrapped-Coins, Gold und Memecoins werden ausgeschlossen
IGNORIEREN = {
    "USDC", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "USDG", "USDD", "EURT",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "BETH", "PAXG", "XAUT",
    "DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "TRUMP", "PENGU", "MEME",
    "BOME", "TURBO", "NEIRO", "PNUT", "MEW", "BRETT", "FARTCOIN", "POPCAT",
    "MOG", "DOGS", "HMSTR", "CATI", "GOAT", "SPX", "MOODENG", "GIGA", "SATS",
    "BABYDOGE", "CHEEMS", "LUNC", "WEN", "TOSHI", "PEOPLE", "ACT", "MUBARAK",
    "BANANAS31", "TST", "USELESS", "PUMP", "BABY", "TRUST",
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
        inst = t["instId"]
        if not inst.endswith("-USDT"):
            continue
        basis = inst.split("-")[0].upper()
        if basis in IGNORIEREN or basis.startswith("1000"):
            continue
        paare.append((float(t.get("volCcy24h") or 0), inst))
    paare.sort(reverse=True)
    return [p[1] for p in paare[:TOP_N]]


def kerzen(inst, bar, limit=100):
    d = http_get(f"{OKX}/candles?instId={inst}&bar={bar}&limit={limit}")
    if d.get("code") != "0":
        return []
    # OKX liefert neueste zuerst -> wir drehen um (alt -> neu). Letzte Kerze = laufende.
    return [
        {"ts": int(k[0]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
        for k in reversed(d["data"])
    ]


def letzte_pivots(k):
    """Index des letzten bestätigten lokalen Hochs und Tiefs."""
    hoch_i = tief_i = None
    for i in range(len(k) - 1 - PIVOT, PIVOT - 1, -1):
        fenster = k[i - PIVOT : i + PIVOT + 1]
        if hoch_i is None and k[i]["h"] == max(x["h"] for x in fenster):
            hoch_i = i
        if tief_i is None and k[i]["l"] == min(x["l"] for x in fenster):
            tief_i = i
        if hoch_i is not None and tief_i is not None:
            break
    return hoch_i, tief_i


def laden():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"gemeldet": []}


def speichern(state):
    state["gemeldet"] = state["gemeldet"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def pruefen(inst, state):
    n = NAEHE_PCT / 100
    gemeldet_richtungen = set()  # 4H hat Vorrang: gleiche Richtung nicht nochmal mit 1H
    for tf in TIMEFRAMES:
        k = kerzen(inst, tf)
        if len(k) < 2 * PIVOT + 3:
            continue
        hi, ti = letzte_pivots(k)
        if hi is None or ti is None:
            continue
        H, L = k[hi]["h"], k[ti]["l"]
        if L <= 0 or (H - L) / L * 100 < MIN_BEWEGUNG_PCT:
            continue
        cur = k[-1]  # laufende Kerze, Wick zählt

        if hi > ti:  # Aufwärts-Swing, Rücklauf nach unten -> LONG
            richtung = "LONG"
            level = H - LEVEL * (H - L)
            nah = cur["l"] <= level * (1 + n) and cur["c"] >= level * (1 - n) and cur["c"] > L
        else:        # Abwärts-Swing, Rücklauf nach oben -> SHORT
            richtung = "SHORT"
            level = L + LEVEL * (H - L)
            nah = cur["h"] >= level * (1 - n) and cur["c"] <= level * (1 + n) and cur["c"] < H
        if not nah:
            continue

        key = f"{inst}|{tf}|{richtung}|{k[hi]['ts']}|{k[ti]['ts']}"
        if key in state["gemeldet"]:
            gemeldet_richtungen.add(richtung)
            continue
        if tf == "1H" and richtung in gemeldet_richtungen:
            continue
        # 4H schon früher für diesen Coin/diese Richtung gemeldet? Dann 1H weglassen.
        if tf == "1H" and any(
            g.startswith(f"{inst}|4H|{richtung}|") for g in state["gemeldet"][-40:]
        ):
            continue
        state["gemeldet"].append(key)
        gemeldet_richtungen.add(richtung)
        abstand = (cur["c"] - level) / level * 100
        senden(
            f"{'🟢' if richtung == 'LONG' else '🔴'} {richtung}: "
            f"{inst.replace('-USDT', '')} kurz vor Fib {LEVEL}\n"
            f"Timeframe: {tf} ({'stärker' if tf == '4H' else 'schwächer'})\n"
            f"Level: {level:.6g}\n"
            f"Kurs: {cur['c']:.6g} ({abstand:+.2f} % zum Level)\n"
            f"Swing: Hoch {H:.6g} | Tief {L:.6g}\n"
            f"Nur eine Idee, keine Empfehlung."
        )


def main():
    state = laden()
    for inst in top_coins():
        try:
            pruefen(inst, state)
        except Exception as e:
            print(f"Fehler bei {inst}: {e}", file=sys.stderr)
        time.sleep(0.15)
    speichern(state)


if __name__ == "__main__":
    main()
