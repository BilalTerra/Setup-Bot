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
DEV_ATR = 4.0            # Ein Hoch/Tief zählt erst, wenn der Kurs danach mind. 4x die normale
                         # Kerzenschwankung (ATR) zurückgelaufen ist. Kleine Zacken fallen raus.
                         # Größer = nur größere Swings, kleiner = mehr Meldungen
MAX_ALTER = 100          # Swing darf höchstens so viele Kerzen alt sein
# Seitwärts-Filter: kein Fib in einer Range
ER_MIN = 0.5             # Swing muss ein sauberer Lauf sein (1 = gerade Linie, 0 = Zickzack). 0 = aus
BOS_ATR = 0.5            # Swing muss das vorherige Hoch/Tief überbieten (Trend!), um mind. 0,5 ATR
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


def kerzen(inst, bar, limit=200):
    d = http_get(f"{OKX}/candles?instId={inst}&bar={bar}&limit={limit}")
    if d.get("code") != "0":
        return []
    # OKX liefert neueste zuerst -> wir drehen um (alt -> neu). Letzte Kerze = laufende.
    return [
        {"ts": int(k[0]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
        for k in reversed(d["data"])
    ]


def atr(k, n=14):
    trs = []
    for i in range(1, len(k)):
        trs.append(max(k[i]["h"] - k[i]["l"],
                       abs(k[i]["h"] - k[i - 1]["c"]),
                       abs(k[i]["l"] - k[i - 1]["c"])))
    trs = trs[-n - 1:-1]  # laufende Kerze nicht mitzählen
    return sum(trs) / len(trs) if trs else 0


def zickzack(k):
    """ATR-ZigZag: nur bestätigte, echte Swings. Eintrag: (index, 'H'/'L', preis)."""
    a = atr(k)
    if a <= 0:
        return []
    thr = DEV_ATR * a
    zz = []
    richtung = 0
    hi = (0, k[0]["h"])
    lo = (0, k[0]["l"])
    for i in range(1, len(k)):
        h, l = k[i]["h"], k[i]["l"]
        if richtung == 0:
            if h > hi[1]:
                hi = (i, h)
            if l < lo[1]:
                lo = (i, l)
            if hi[1] - l >= thr:
                zz.append((hi[0], "H", hi[1]))
                richtung = -1
                lo = (i, l)
            elif h - lo[1] >= thr:
                zz.append((lo[0], "L", lo[1]))
                richtung = 1
                hi = (i, h)
        elif richtung == 1:
            if h > hi[1]:
                hi = (i, h)
            elif hi[1] - l >= thr:
                zz.append((hi[0], "H", hi[1]))
                richtung = -1
                lo = (i, l)
        else:
            if l < lo[1]:
                lo = (i, l)
            elif h - lo[1] >= thr:
                zz.append((lo[0], "L", lo[1]))
                richtung = 1
                hi = (i, h)
    return zz


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
        if len(k) < 30:
            continue
        zz = zickzack(k)
        if len(zz) < 2:
            continue
        p1, p2 = zz[-2], zz[-1]          # letzter Swing: p1 -> p2
        if len(k) - 1 - p2[0] > MAX_ALTER:
            continue
        H = max(p1[2], p2[2])
        L = min(p1[2], p2[2])
        if L <= 0:
            continue

        # --- Seitwärts-Filter ---
        # 1) Trend: Das Ende des Swings muss das vorherige gleichartige Extrem überbieten
        #    (höheres Hoch bei Aufwärts-Swing, tieferes Tief bei Abwärts-Swing)
        if len(zz) < 3:
            continue
        vorher = zz[-3][2]
        a_wert = atr(k)
        if p2[1] == "H" and not (p2[2] > vorher + BOS_ATR * a_wert):
            continue
        if p2[1] == "L" and not (p2[2] < vorher - BOS_ATR * a_wert):
            continue
        # 2) Sauberer Lauf: Weg von A nach B darf nicht wild hin und her gehen
        weg = sum(abs(k[i]["c"] - k[i - 1]["c"]) for i in range(p1[0] + 1, p2[0] + 1))
        if weg <= 0 or abs(k[p2[0]]["c"] - k[p1[0]]["c"]) / weg < ER_MIN:
            continue
        cur = k[-1]  # laufende Kerze, Wick zählt

        if p2[1] == "H":  # Aufwärts-Swing, Rücklauf nach unten -> LONG
            richtung = "LONG"
            level = H - LEVEL * (H - L)
            nah = cur["l"] <= level * (1 + n) and cur["c"] >= level * (1 - n) and cur["c"] > L
        else:             # Abwärts-Swing, Rücklauf nach oben -> SHORT
            richtung = "SHORT"
            level = L + LEVEL * (H - L)
            nah = cur["h"] >= level * (1 - n) and cur["c"] <= level * (1 + n) and cur["c"] < H
        if not nah:
            continue

        key = f"{inst}|{tf}|{richtung}|{k[p1[0]]['ts']}|{k[p2[0]]['ts']}"
        if key in state["gemeldet"]:
            gemeldet_richtungen.add(richtung)
            continue
        if tf == "1H" and (
            richtung in gemeldet_richtungen
            or any(g.startswith(f"{inst}|4H|{richtung}|") for g in state["gemeldet"][-40:])
        ):
            continue
        state["gemeldet"].append(key)
        gemeldet_richtungen.add(richtung)
        abstand = (cur["c"] - level) / level * 100
        start, ende = (L, H) if p2[1] == "H" else (H, L)
        senden(
            f"{'🟢' if richtung == 'LONG' else '🔴'} {richtung}: "
            f"{inst.replace('-USDT', '')} kurz vor Fib {LEVEL}\n"
            f"Timeframe: {tf} ({'stärker' if tf == '4H' else 'schwächer'})\n"
            f"Fib gezogen von {start:.6g} nach {ende:.6g}\n"
            f"Level {LEVEL}: {level:.6g}\n"
            f"Kurs: {cur['c']:.6g} ({abstand:+.2f} % zum Level)\n"
            f"Swing endete vor {len(k) - 1 - p2[0]} Kerzen\n"
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
if __name__ == "__main__":
    main()
