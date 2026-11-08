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
LEVEL = 0.882            # Einstiegs-Level (Fibonacci)
NAEHE_PCT = 0.3          # "kurz davor": so nah (in %) am Level wird gemeldet
GP_LEVEL = 0.65          # Take Profit 1 = Golden Pocket (0.65 = Beginn der Zone, 0.618 = Mitte)
DEV_ATR = 4.0            # Ein Hoch/Tief zählt erst, wenn der Kurs danach mind. 4x die normale
                         # Kerzenschwankung (ATR) zurückgelaufen ist. Größer = nur größere Swings
MAX_ALTER = 100          # Swing darf höchstens so viele Kerzen alt sein
ER_MIN = 0.5             # Swing muss ein sauberer Lauf sein (1 = gerade Linie). 0 = aus
BOS_ATR = 0.5            # Swing muss das vorherige Hoch/Tief überbieten (Trend, keine Range)
MAX_TAGE = 12            # Trade in der Trefferliste nach so vielen Tagen abschließen
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


# ---------- Hilfsfunktionen ----------
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


# ---------- Speicher ----------
def laden():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    state.setdefault("gemeldet", [])
    state.setdefault("offset", 0)
    state.setdefault("trades", [])
    return state


def speichern(state):
    state["gemeldet"] = state["gemeldet"][-300:]
    state["trades"] = state["trades"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------- Trefferliste ----------
def name(t):
    return t["inst"].replace("-USDT", "")


def pruefe_trade(t, k):
    """Geht die Kerzen seit der Meldung durch. Stop = Level 1, TP1 = Golden Pocket, TP2 = Level 0.
    Berührt eine Kerze Stop und Ziel zugleich, zählt vorsichtig der Stop."""
    lang = t["richtung"] == "LONG"
    for c in k:
        if c["ts"] < t["ts"]:
            continue
        if t["status"] == "offen":
            stop_hit = c["l"] <= t["stop"] if lang else c["h"] >= t["stop"]
            tp1_hit = c["h"] >= t["tp1"] if lang else c["l"] <= t["tp1"]
            if stop_hit:
                t["status"] = "stop"
                return
            if tp1_hit:
                t["status"] = "tp1"
        if t["status"] == "tp1":
            tp2_hit = c["h"] >= t["tp2"] if lang else c["l"] <= t["tp2"]
            if tp2_hit:
                t["status"] = "tp2"
                return


def auswerten(state):
    offen = [t for t in state["trades"] if not t.get("fertig")]
    if not offen:
        return
    jetzt = int(time.time() * 1000)
    nach_coin = {}
    for t in offen:
        nach_coin.setdefault(t["inst"], []).append(t)
    for inst, trades in nach_coin.items():
        try:
            k = kerzen(inst, "1H", 300)
        except Exception as e:
            print(f"Fehler bei Auswertung {inst}: {e}", file=sys.stderr)
            continue
        for t in trades:
            alt = t["status"]
            pruefe_trade(t, k)
            neu = t["status"]
            kopf = f"{name(t)} {t['richtung']} ({t['tf']})"
            if alt == "offen" and neu == "stop":
                senden(f"❌ {kopf}: Stop (Level 1) erreicht.")
            elif alt == "offen" and neu == "tp1":
                senden(f"✅ {kopf}: Golden Pocket erreicht (TP1).")
            elif alt == "offen" and neu == "tp2":
                senden(f"🎯 {kopf}: Golden Pocket und Level 0 erreicht.")
            elif alt == "tp1" and neu == "tp2":
                senden(f"🎯 {kopf}: Level 0 erreicht (TP2).")
            if t["status"] in ("stop", "tp2"):
                t["fertig"] = True
            elif jetzt - t["ts"] > MAX_TAGE * 86400000:
                if t["status"] == "offen":
                    t["status"] = "abgelaufen"
                    senden(f"⏱ {kopf}: nach {MAX_TAGE} Tagen weder Golden Pocket noch Stop.")
                t["fertig"] = True


def statistik(state):
    teile = ["📊 Trefferliste"]
    for tf in ("4H", "1H", None):
        ts = [t for t in state["trades"] if tf is None or t["tf"] == tf]
        if not ts:
            continue
        z = lambda *st: sum(1 for t in ts if t["status"] in st)
        tp1 = z("tp1", "tp2")
        stop = z("stop")
        quote = f"{tp1 / (tp1 + stop) * 100:.0f} %" if tp1 + stop else "–"
        teile.append(
            f"\n{'Alle zusammen' if tf is None else tf + '-Chart'}: {len(ts)} Meldungen\n"
            f"✅ Golden Pocket erreicht: {tp1} (davon Level 0: {z('tp2')})\n"
            f"❌ Stop (Level 1): {stop}\n"
            f"⏳ Noch offen: {z('offen')} | ⏱ Abgelaufen: {z('abgelaufen')}\n"
            f"Trefferquote (ohne offene): {quote}"
        )
    if len(teile) == 1:
        teile.append("\nNoch keine Meldungen gespeichert.")
    return "\n".join(teile)


def befehle(state):
    try:
        d = http_get(f"{TG_API}/getUpdates?offset={state['offset'] + 1}&timeout=0")
    except Exception as e:
        print(f"getUpdates Fehler: {e}", file=sys.stderr)
        return
    for u in d.get("result", []):
        state["offset"] = u["update_id"]
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/stats") or text.startswith("/statistik"):
            senden(statistik(state))
        elif text.startswith("/start") or text.startswith("/help"):
            senden("Ich melde Fib-0,882-Setups. Mit /stats siehst du die Trefferliste.")


# ---------- Setups finden ----------
def pruefen(inst, state):
    n = NAEHE_PCT / 100
    gemeldet_richtungen = set()  # 4H hat Vorrang: gleiche Richtung nicht nochmal mit 1H
    for tf in TIMEFRAMES:
        k = kerzen(inst, tf)
        if len(k) < 30:
            continue
        zz = zickzack(k)
        if len(zz) < 3:
            continue
        p1, p2 = zz[-2], zz[-1]          # letzter Swing: p1 -> p2
        if len(k) - 1 - p2[0] > MAX_ALTER:
            continue
        H = max(p1[2], p2[2])
        L = min(p1[2], p2[2])
        if L <= 0:
            continue

        # --- Seitwärts-Filter ---
        vorher = zz[-3][2]
        a_wert = atr(k)
        if p2[1] == "H" and not (p2[2] > vorher + BOS_ATR * a_wert):
            continue
        if p2[1] == "L" and not (p2[2] < vorher - BOS_ATR * a_wert):
            continue
        weg = sum(abs(k[i]["c"] - k[i - 1]["c"]) for i in range(p1[0] + 1, p2[0] + 1))
        if weg <= 0 or abs(k[p2[0]]["c"] - k[p1[0]]["c"]) / weg < ER_MIN:
            continue

        cur = k[-1]  # laufende Kerze, Wick zählt
        R = H - L
        if p2[1] == "H":  # Aufwärts-Swing, Rücklauf nach unten -> LONG
            richtung = "LONG"
            level = H - LEVEL * R
            nah = cur["l"] <= level * (1 + n) and cur["c"] >= level * (1 - n) and cur["c"] > L
            stop, tp1, tp2 = L, H - GP_LEVEL * R, H
        else:             # Abwärts-Swing, Rücklauf nach oben -> SHORT
            richtung = "SHORT"
            level = L + LEVEL * R
            nah = cur["h"] >= level * (1 - n) and cur["c"] <= level * (1 + n) and cur["c"] < H
            stop, tp1, tp2 = H, L + GP_LEVEL * R, L
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

        stunde = int(time.time() * 1000) // 3600000 * 3600000
        state["trades"].append({
            "id": key, "inst": inst, "tf": tf, "richtung": richtung, "ts": stunde,
            "level": level, "stop": stop, "tp1": tp1, "tp2": tp2, "status": "offen",
        })
        abstand = (cur["c"] - level) / level * 100
        start, ende = (L, H) if p2[1] == "H" else (H, L)
        senden(
            f"{'🟢' if richtung == 'LONG' else '🔴'} {richtung}: "
            f"{inst.replace('-USDT', '')} kurz vor Fib {LEVEL}\n"
            f"Timeframe: {tf} ({'stärker' if tf == '4H' else 'schwächer'})\n"
            f"Fib gezogen von {start:.6g} nach {ende:.6g}\n"
            f"Level {LEVEL}: {level:.6g}\n"
            f"Kurs: {cur['c']:.6g} ({abstand:+.2f} % zum Level)\n"
            f"Stop (Level 1): {stop:.6g}\n"
            f"TP1 (Golden Pocket {GP_LEVEL}): {tp1:.6g}\n"
            f"TP2 (Level 0): {tp2:.6g}\n"
            f"Nur eine Idee, keine Empfehlung."
        )


def main():
    state = laden()
    befehle(state)
    try:
        auswerten(state)
    except Exception as e:
        print(f"Fehler bei Auswertung: {e}", file=sys.stderr)
    for inst in top_coins():
        try:
            pruefen(inst, state)
        except Exception as e:
            print(f"Fehler bei {inst}: {e}", file=sys.stderr)
        time.sleep(0.15)
    speichern(state)


if __name__ == "__main__":
    main()
