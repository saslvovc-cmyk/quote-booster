"""
QUOTE BOOSTER PRO - Cloud Web Application
Progettato per girare online 24/7 su Render o qualsiasi server cloud.
Accessibile da smartphone (Android, iPhone) senza bisogno del PC acceso.
Include Palinsesto Completo di Oggi con selezione a 1 tap (⭐ Aggiungi ai Preferiti).
"""

import os
import math
import time
import json
import sqlite3
import itertools
import threading
import datetime
import gzip
import re
import requests
from flask import Flask, jsonify, request, Response

DB_FILE = "mega_db.sqlite"
GZ_FILE = "mega_db.sqlite.gz"
FAV_FILE = "preferiti_flashscore.json"

# Decomprime il database se necessario all'avvio
if not os.path.exists(DB_FILE) and os.path.exists(GZ_FILE):
    print("Decompressione database storico in corso...")
    try:
        with gzip.open(GZ_FILE, "rb") as f_in:
            with open(DB_FILE, "wb") as f_out:
                f_out.write(f_in.read())
        print("Database decompresso con successo!")
    except Exception as e:
        print("Errore estrazione database:", e)

MIN_N_CALCIO = 40
MIN_N_TENNIS = 60
SEP1 = chr(172)
SEP2 = chr(247)
FS_HEADERS = {
    'X-Fsign': 'SW9D1eZo',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://www.flashscore.com/',
}

# =====================================================================
#  MOTORE MATEMATICO & STATISTICO
# =====================================================================
def devig(odds):
    if not odds or any(o is None or o <= 1.0 for o in odds):
        n = len(odds) if odds else 1
        return [1.0 / n] * n, 0.0
    try:
        inv = [1.0 / float(o) for o in odds]
        s = sum(inv)
        if s <= 0:
            n = len(odds)
            return [1.0 / n] * n, 0.0
        return [x / s for x in inv], (s - 1.0) * 100.0
    except Exception:
        n = len(odds) if odds else 1
        return [1.0 / n] * n, 0.0

def zscore(freq, p, n):
    if n == 0 or p <= 0 or p >= 1:
        return 0.0
    return (freq - p) / math.sqrt(p * (1 - p) / n)

def affidabilita(n):
    if n < 30: return "BASSA"
    if n < 100: return "MEDIA"
    return "ALTA"

def _meta_trend(flags):
    n = len(flags)
    out = {"h1": None, "h2": None, "l20": None}
    if n >= 10:
        m = n // 2
        out["h1"] = sum(flags[:m]) / m * 100
        out["h2"] = sum(flags[m:]) / (n - m) * 100
    if n >= 20:
        out["l20"] = sum(flags[-20:]) / 20 * 100
    return out

def carica_db(path):
    if not os.path.exists(path):
        return [], []
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    tabelle = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fb, tn = [], []

    if "football_matches" in tabelle:
        q = ("SELECT match_date, home_team, away_team, ftr, odds_b365_h, odds_b365_d, odds_b365_a "
             "FROM football_matches WHERE ftr IN ('H','D','A') "
             "AND odds_b365_h > 1 AND odds_b365_d > 1 AND odds_b365_a > 1 ORDER BY match_date")
        for d, h, a, res, o1, ox, o2 in cur.execute(q):
            try:
                (p1, px, p2), _ = devig([float(o1), float(ox), float(o2)])
            except (ValueError, TypeError, ZeroDivisionError):
                continue
            d = d or ""
            fb.append((d, h, a, res, p1, px, p2))

    if "tennis_matches" in tabelle:
        q = ("SELECT match_date, tournament, surface, winner, loser, odds_b365_w, odds_b365_l, best_of "
             "FROM tennis_matches WHERE odds_b365_w > 1 AND odds_b365_l > 1 ORDER BY match_date")
        for d, t, s, w, l, ow, ol, bo in cur.execute(q):
            try:
                ow, ol = float(ow), float(ol)
            except (ValueError, TypeError):
                continue
            if ow == ol:
                continue
            (pw, pl), _ = devig([ow, ol])
            d = d or ""
            if ow < ol:
                tn.append((d, t, s, w, l, pw, True, str(bo)))
            else:
                tn.append((d, t, s, l, w, pl, False, str(bo)))
    conn.close()
    return fb, tn

def analizza_calcio(rows, q1, qx, q2):
    if not rows or any(x is None or x <= 1.0 for x in (q1, qx, q2)):
        return None
    (p1, px, p2), aggio = devig([q1, qx, q2])
    fav = "1" if p1 >= p2 else "2"
    cand = []
    for r in rows:
        d = max(abs(r[4] - p1), abs(r[5] - px), abs(r[6] - p2))
        if d <= 0.06:
            cand.append((d, r))
    if not cand:
        return None
    cand.sort(key=lambda t: t[0])
    tol = 0.02
    if len(cand) >= MIN_N_CALCIO:
        tol = max(tol, cand[MIN_N_CALCIO - 1][0])
    else:
        tol = 0.06
    campione = sorted([r for d, r in cand if d <= tol], key=lambda r: r[0] or "")
    n = len(campione)
    cnt = {"1": sum(1 for r in campione if r[3] == "H"),
           "X": sum(1 for r in campione if r[3] == "D"),
           "2": sum(1 for r in campione if r[3] == "A")}
    freq = {k: v / n * 100 for k, v in cnt.items()}
    impl = {"1": p1 * 100, "X": px * 100, "2": p2 * 100}
    dog = "2" if fav == "1" else "1"
    z_fav = zscore(freq[fav] / 100, impl[fav] / 100, n)
    rottura = 100 - freq[fav]
    rottura_att = 100 - impl[fav]
    quota_x = (cnt["X"] / (cnt["X"] + cnt[dog]) * 100) if (cnt["X"] + cnt[dog]) else 0

    ordine = sorted(freq, key=freq.get, reverse=True)
    if freq[ordine[0]] >= 50:
        pick = ordine[0]
        pick_pct = freq[pick]
        tipo = "singolo"
    else:
        escluso = ordine[2]
        pick = {"1": "X2", "X": "12", "2": "1X"}[escluso]
        pick_pct = 100 - freq[escluso]
        tipo = "doppia"

    def hit(r):
        s = {"H": "1", "D": "X", "A": "2"}[r[3]]
        return 1 if s in pick else 0
    trend = _meta_trend([hit(r) for r in campione])

    return dict(n=n, tol=tol, P=(p1, px, p2), aggio=aggio, fair=(1 / p1, 1 / px, 1 / p2),
                fav=fav, dog=dog, cnt=cnt, freq=freq, impl=impl, z_fav=z_fav,
                rottura=rottura, rottura_att=rottura_att, x_nelle_rotture=quota_x,
                pick=pick, pick_pct=pick_pct, tipo=tipo, trend=trend, ultimi=campione[-5:])

def analizza_tennis(rows, q1, q2):
    if not rows or any(x is None or x <= 1.0 for x in (q1, q2)):
        return None
    (pa, pb), aggio = devig([q1, q2])
    p_fav = max(pa, pb)
    fav = "1" if q1 <= q2 else "2"
    und = "2" if fav == "1" else "1"
    cand = []
    for r in rows:
        d = abs(r[5] - p_fav)
        if d <= 0.05:
            cand.append((d, r))
    if not cand:
        return None
    cand.sort(key=lambda t: t[0])
    tol = 0.015
    if len(cand) >= MIN_N_TENNIS:
        tol = max(tol, cand[MIN_N_TENNIS - 1][0])
    else:
        tol = 0.05
    campione = sorted([r for d, r in cand if d <= tol], key=lambda r: r[0] or "")
    n = len(campione)
    fav_c = sum(1 for r in campione if r[6])
    pct_fav = fav_c / n * 100
    pct_und = 100 - pct_fav
    impl_fav = p_fav * 100
    z_fav = zscore(pct_fav / 100, p_fav, n)

    if pct_und > 50:
        verdetto = f"SEGNO {und} (lo sfavorito prevale nel contesto)"
        livello = "bad"
    elif pct_fav >= 62 and z_fav > -1.5:
        verdetto = f"SEGNO {fav} (favorito solido)"
        livello = "ok"
    elif pct_fav >= 55:
        verdetto = f"SEGNO {fav} (moderato)"
        livello = "warn"
    else:
        verdetto = f"MATCH APERTO: {und} competitivo"
        livello = "bad"

    return dict(n=n, tol=tol, P=(pa, pb), aggio=aggio, fair=(1 / pa, 1 / pb), fav=fav, und=und,
                pct_fav=pct_fav, pct_und=pct_und, impl_fav=impl_fav, z_fav=z_fav,
                verdetto=verdetto, livello=livello, ultimi=campione[-5:])

def barra(pct, impl, width=20):
    n = int(round(max(0, min(100, pct)) / 100 * width))
    celle = ["#"] * n + ["."] * (width - n)
    k = max(0, min(width - 1, int(round(max(0, min(100, impl)) / 100 * width))))
    celle[k] = "|"
    return "".join(celle)

def report_calcio(r, q):
    fav, dog = r["fav"], r["dog"]
    pct = r["pick_pct"]
    livello = "ok" if pct >= 60 else ("warn" if pct >= 50 else "bad")
    nome = f"SEGNO {r['pick']}" if r["tipo"] == "singolo" else f"DOPPIA CHANCE {r['pick']}"
    titolo = f"{nome}  ({pct:.1f}%)"
    xr = r["x_nelle_rotture"]
    summary = [
        f"• {r['n']} precedenti, affidabilita' {affidabilita(r['n'])}",
        f"• Favorito (segno {fav}): {r['freq'][fav]:.1f}% storico, {r['impl'][fav]:.1f}% fair quota",
        f"• Rottura {r['rottura']:.1f}%: pareggio {xr:.0f}%, ribaltone {100 - xr:.0f}%",
    ]
    full = [titolo, f"{r['n']} precedenti | tolleranza +/-{r['tol'] * 100:.1f} pt | affidabilita' {affidabilita(r['n'])}", ""]
    full.append("PERCHE' QUESTO PRONOSTICO:")
    if r["tipo"] == "singolo":
        full.append(f"Nei {r['n']} match storici il segno {r['pick']} e' uscito il {pct:.1f}% (supera il 50%).")
    else:
        escluso = [s for s in ("1", "X", "2") if s not in r["pick"]][0]
        full.append(f"Nessun segno singolo supera il 50%. La doppia chance {r['pick']} copre il {pct:.1f}%, escludendo il segno {escluso} ({r['freq'][escluso]:.1f}%).")
    full += ["", "FREQUENZE STORICHE:"]
    for s in ("1", "X", "2"):
        full.append(f"{s}  [{barra(r['freq'][s], r['impl'][s])}]  {r['freq'][s]:5.1f}% ({r['cnt'][s]} match)")
    full += ["", f"ROTTURA FAVORITO (segno {fav}): {r['rottura']:.1f}% (pareggio {xr:.0f}% | vittoria {dog} {100 - xr:.0f}%)"]
    full += ["", f"Fair Odds: {r['fair'][0]:.2f} / {r['fair'][1]:.2f} / {r['fair'][2]:.2f}"]
    return livello, titolo, summary, full

def report_tennis(r, q):
    livello = r["livello"]
    titolo = r["verdetto"]
    summary = [
        f"• {r['n']} precedenti, affidabilita' {affidabilita(r['n'])}",
        f"• Favorito (G{r['fav']}): {r['pct_fav']:.1f}% storico, {r['impl_fav']:.1f}% pulita",
        f"• Ribaltone {r['pct_und']:.1f}%",
    ]
    full = [titolo, f"{r['n']} precedenti | tolleranza +/-{r['tol'] * 100:.1f} pt", ""]
    full += ["FREQUENZE STORICHE:", f"G{r['fav']} fav: {r['pct_fav']:.1f}%", f"G{r['und']} sfv: {r['pct_und']:.1f}%"]
    full += ["", f"Fair Odds: {r['fair'][0]:.2f} / {r['fair'][1]:.2f}"]
    return livello, titolo, summary, full

# =====================================================================
#  CLIENT FLASHSCORE
# =====================================================================
class FlashscorePalinsestoService:
    @staticmethod
    def get_tournament_sort_key(sport_id, league_name, feed_idx):
        lg = (league_name or '').upper().strip()
        is_fb = (sport_id == 1 or sport_id == "Calcio")
        if is_fb:
            # Tier 1: I Principali Campionati di Diretta.it / Flashscore
            if ('SERIE A' in lg) and ('ITALI' in lg or 'ITALY' in lg or lg == 'SERIE A') and 'WOMEN' not in lg and 'FEMMINILE' not in lg:
                return (1, 1, feed_idx)
            if ('SERIE B' in lg) and ('ITALI' in lg or 'ITALY' in lg or lg == 'SERIE B') and 'WOMEN' not in lg:
                return (1, 2, feed_idx)
            if ('COPPA ITALIA' in lg or 'SUPERCOPPA' in lg) and ('ITALI' in lg or 'ITALY' in lg):
                return (1, 3, feed_idx)
            if 'CHAMPIONS LEAGUE' in lg and ('EUROPE' in lg or 'EUROPA' in lg or 'UEFA' in lg) and 'WOMEN' not in lg:
                return (1, 4, feed_idx)
            if 'EUROPA LEAGUE' in lg and ('EUROPE' in lg or 'EUROPA' in lg or 'UEFA' in lg) and 'WOMEN' not in lg:
                return (1, 5, feed_idx)
            if 'CONFERENCE LEAGUE' in lg and 'WOMEN' not in lg:
                return (1, 6, feed_idx)
            if ('PREMIER LEAGUE' in lg) and ('ENGLAND' in lg or 'INGHILTERRA' in lg) and 'CUP' not in lg and '2' not in lg and 'U21' not in lg and 'WOMEN' not in lg:
                return (1, 7, feed_idx)
            if ('LALIGA' in lg) and ('SPAIN' in lg or 'SPAGNA' in lg) and '2' not in lg and 'HYPERMOTION' not in lg and 'WOMEN' not in lg:
                return (1, 8, feed_idx)
            if ('BUNDESLIGA' in lg) and ('GERMANY' in lg or 'GERMANIA' in lg) and '2' not in lg and 'WOMEN' not in lg:
                return (1, 9, feed_idx)
            if ('LIGUE 1' in lg) and ('FRANCE' in lg or 'FRANCIA' in lg) and 'WOMEN' not in lg:
                return (1, 10, feed_idx)
            if ('EREDIVISIE' in lg) and ('NETHERLANDS' in lg or 'OLANDA' in lg) and 'WOMEN' not in lg:
                return (1, 11, feed_idx)
            if ('LIGA PORTUGAL' in lg or 'PRIMEIRA LIGA' in lg) and '2' not in lg and 'WOMEN' not in lg:
                return (1, 12, feed_idx)
            if 'NATIONS LEAGUE' in lg and 'WOMEN' not in lg:
                return (1, 13, feed_idx)
            if any(x in lg for x in ['EUROPEI', 'EURO QUAL', 'EURO CUP']) and 'U21' not in lg and 'WOMEN' not in lg:
                return (1, 14, feed_idx)
            if any(x in lg for x in ['MONDIALI', 'WORLD CUP']) and 'U20' not in lg and 'U17' not in lg and 'WOMEN' not in lg:
                return (1, 15, feed_idx)

            # Tier 2: Campionati di Rilievo & Coppe Nazionali
            if 'SERIE C' in lg: return (2, 1, feed_idx)
            if 'CHAMPIONSHIP' in lg and ('ENGLAND' in lg or 'INGHILTERRA' in lg): return (2, 2, feed_idx)
            if any(x in lg for x in ['FA CUP', 'EFL CUP', 'EFL TROPHY']) and ('ENGLAND' in lg or 'INGHILTERRA' in lg): return (2, 3, feed_idx)
            if any(x in lg for x in ['COPA DEL REY', 'LALIGA2', 'HYPERMOTION']): return (2, 4, feed_idx)
            if any(x in lg for x in ['2. BUNDESLIGA', 'DFB POKAL']): return (2, 5, feed_idx)
            if any(x in lg for x in ['LIGUE 2', 'COUPE DE FRANCE']): return (2, 6, feed_idx)
            if 'LIGA PORTUGAL 2' in lg: return (2, 7, feed_idx)
            if 'SUPER LIG' in lg and ('TURKEY' in lg or 'TURCHIA' in lg): return (2, 8, feed_idx)
            if 'JUPILER' in lg and ('BELGIUM' in lg or 'BELGIO' in lg): return (2, 9, feed_idx)
            if 'SAUDI' in lg and ('LEAGUE' in lg or 'DIVISION' in lg): return (2, 10, feed_idx)
            if ('SERIE A' in lg or 'COPA DO BRASIL' in lg) and ('BRAZIL' in lg or 'BRASILE' in lg): return (2, 11, feed_idx)
            if 'LIGA PROFESIONAL' in lg and ('ARGENTINA' in lg): return (2, 12, feed_idx)
            if any(x in lg for x in ['LIBERTADORES', 'SUDAMERICANA', 'AFC CHAMPIONS LEAGUE']): return (2, 13, feed_idx)
            if 'MLS' in lg or 'MAJOR LEAGUE SOCCER' in lg: return (2, 14, feed_idx)

            # Tier 3: Tutto il resto nell'ordine naturale del feed Flashscore
            return (3, feed_idx, feed_idx)
        else:
            # Tennis
            if any(x in lg for x in ['AUSTRALIAN OPEN', 'ROLAND GARROS', 'OPEN DI FRANCIA', 'FRENCH OPEN', 'WIMBLEDON', 'US OPEN']):
                return (1, 1, feed_idx)
            if 'ATP - SINGLES' in lg or 'ATP - SINGOLARE' in lg: return (1, 2, feed_idx)
            if 'WTA - SINGLES' in lg or 'WTA - SINGOLARE' in lg: return (1, 3, feed_idx)
            if 'ATP - DOUBLES' in lg or 'ATP - DOPPIO' in lg: return (1, 4, feed_idx)
            if 'WTA - DOUBLES' in lg or 'WTA - DOPPIO' in lg: return (1, 5, feed_idx)
            if 'CHALLENGER MEN - SINGLES' in lg or 'CHALLENGER UOMINI - SINGOLARE' in lg: return (2, 1, feed_idx)
            if 'CHALLENGER WOMEN - SINGLES' in lg or 'CHALLENGER DONNE - SINGOLARE' in lg: return (2, 2, feed_idx)
            if 'CHALLENGER' in lg and ('DOUBLES' in lg or 'DOPPIO' in lg): return (2, 3, feed_idx)
            if 'ITF MEN' in lg or 'ITF UOMINI' in lg: return (3, 1, feed_idx)
            if 'ITF WOMEN' in lg or 'ITF DONNE' in lg: return (3, 2, feed_idx)
            return (4, feed_idx, feed_idx)

    @staticmethod
    def fetch_all_today(sport_id=1, day_offset=0):
        odds_url = f"https://global.flashscore.ninja/5/x/feed/fo_{sport_id}_{day_offset}_2_it_1_0"
        odds_map = {}
        try:
            r_odds = requests.get(odds_url, headers=FS_HEADERS, timeout=8)
            if r_odds.status_code == 200:
                for row in r_odds.text.split("~"):
                    f = {}
                    for part in row.split(SEP1):
                        kv = part.split(SEP2)
                        if len(kv) == 2: f[kv[0]] = kv[1]
                    mid = f.get("AA")
                    if mid and "XA" in f:
                        try:
                            q1 = float(f["XA"].replace(",", "."))
                            if sport_id == 1:
                                qx = float(f.get("XB", "0").replace(",", "."))
                                q2 = float(f.get("XC", "0").replace(",", "."))
                                if q1 > 1.0 and qx > 1.0 and q2 > 1.0:
                                    odds_map[mid] = [q1, qx, q2]
                            else:
                                q2_raw = f.get("XC") or f.get("XB") or "0"
                                q2 = float(q2_raw.replace(",", "."))
                                if q1 > 1.0 and q2 > 1.0:
                                    odds_map[mid] = [q1, q2]
                        except ValueError: pass
        except Exception: pass

        sched_url = f"https://global.flashscore.ninja/5/x/feed/f_{sport_id}_{day_offset}_2_it_1_"
        matches = []
        try:
            r_sched = requests.get(sched_url, headers=FS_HEADERS, timeout=8)
            if r_sched.status_code == 200:
                curr_league = "Generale"
                curr_league_idx = 0
                curr_sort_key = (3, 9999, 9999)
                match_counter = 0
                for row in r_sched.text.split("~"):
                    f = {}
                    for part in row.split(SEP1):
                        kv = part.split(SEP2)
                        if len(kv) == 2: f[kv[0]] = kv[1]
                    if "ZA" in f:
                        curr_league = f["ZA"]
                        curr_league_idx += 1
                        curr_sort_key = FlashscorePalinsestoService.get_tournament_sort_key(sport_id, curr_league, curr_league_idx)
                    elif "AA" in f:
                        mid = f["AA"]
                        match_counter += 1
                        home = f.get("AE", "Casa").strip()
                        away = f.get("AF", "Ospite").strip()
                        ts = f.get("AD", "")
                        time_str = "--:--"
                        if ts:
                            try:
                                dt = datetime.datetime.fromtimestamp(int(ts))
                                time_str = dt.strftime("%H:%M")
                            except Exception: pass
                        status_code = f.get("AB", "1")
                        score_str = f"{f.get('AG', '-')} : {f.get('AH', '-')}"
                        q = odds_map.get(mid)
                        q_str = " / ".join(f"{x:.2f}" for x in q) if q else "Quote N.D."
                        is_top = curr_sort_key[0] == 1
                        matches.append({
                            "id": mid,
                            "sport": "Tennis" if sport_id == 2 else "Calcio",
                            "league": curr_league,
                            "league_sort_key": curr_sort_key,
                            "feed_order": match_counter,
                            "is_top": is_top,
                            "home": home,
                            "away": away,
                            "time": time_str,
                            "timestamp": int(ts) if ts.isdigit() else 0,
                            "status_code": status_code,
                            "score": score_str,
                            "odds": q,
                            "odds_str": q_str
                        })

                # Ordina: campionati principali prima, mantenendo l'ordine dei match nel torneo
                matches.sort(key=lambda m: (m.get("league_sort_key", (99, 9999, 9999)), m.get("feed_order", 0)))
        except Exception: pass
        return matches

# =====================================================================
#  APP FLASK & STATO CONDIVISO
# =====================================================================
app = Flask(__name__)

# Caricamento storico
print("Caricamento database storico in RAM...")
fb_db, tn_db = carica_db(DB_FILE)
print(f"Database caricato: {len(fb_db):,} match Calcio, {len(tn_db):,} match Tennis.")

favorites = {}
cached_palinsesto = []
last_palinsesto_time = 0

def get_palinsesto(force=False):
    global cached_palinsesto, last_palinsesto_time
    now = time.time()
    if not force and cached_palinsesto and (now - last_palinsesto_time < 90):
        return cached_palinsesto
    try:
        fb = FlashscorePalinsestoService.fetch_all_today(sport_id=1, day_offset=0)
        tn = FlashscorePalinsestoService.fetch_all_today(sport_id=2, day_offset=0)

        def tier(m):
            sk = m.get("league_sort_key", (3, 9999, 9999))
            return sk[0] if isinstance(sk, (tuple, list)) else 3

        # Gerarchia: Tier 1 Calcio (Serie A, Premier, LaLiga...) + Tier 1 Tennis (Grand Slam, ATP, WTA) in cima
        # poi Tier 2 Calcio (Serie C, Championship, Coppe...) + Tier 2 Tennis (Challenger)
        # poi il resto (Tier 3 / ITF / campionati minori)
        fb_t1 = [m for m in fb if tier(m) == 1]
        tn_t1 = [m for m in tn if tier(m) == 1]
        fb_t2 = [m for m in fb if tier(m) == 2]
        tn_t2 = [m for m in tn if tier(m) == 2]
        fb_rest = [m for m in fb if tier(m) >= 3]
        tn_rest = [m for m in tn if tier(m) >= 3]

        all_m = fb_t1 + tn_t1 + fb_t2 + tn_t2 + fb_rest + tn_rest
        cached_palinsesto = all_m
        last_palinsesto_time = now
    except Exception:
        pass
    return cached_palinsesto

def load_saved_favorites():
    global favorites
    if os.path.exists(FAV_FILE):
        try:
            with open(FAV_FILE, "r", encoding="utf-8") as f:
                favorites = json.load(f)
        except Exception:
            favorites = {}

    # Se vuoto, pre-popola con le prime 4 partite di oggi con quote
    if not favorites:
        pal = get_palinsesto()
        with_odds = [m for m in pal if m.get("odds")]
        for m in with_odds[:4]:
            item = dict(m)
            elabora_algoritmo(item)
            favorites[item["id"]] = item
        if favorites:
            save_favorites()

def save_favorites():
    try:
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favorites, f, indent=2, ensure_ascii=False)
    except Exception: pass

def elabora_algoritmo(m):
    q = m.get("odds")
    if not q:
        m["pick_str"] = "Quote N.D."
        m["pick_title"] = "In attesa delle quote ufficiali"
        m["summary_lines"] = ["• Quote al momento non disponibili sul feed"]
        m["full_report"] = ["Quote non disponibili."]
        return
    sp = m.get("sport", "Calcio")
    if sp == "Calcio" and len(q) == 3:
        res = analizza_calcio(fb_db, q[0], q[1], q[2])
        if res:
            livello, titolo, summ, full = report_calcio(res, q)
            icon = "🎯" if livello == "ok" else ("⚠️" if livello == "warn" else "🚨")
            m["pick_str"] = f"{icon} {res['pick']} ({res['pick_pct']:.1f}%)"
            m["pick_title"] = titolo
            m["summary_lines"] = summ
            m["full_report"] = full
    elif sp == "Tennis" and len(q) >= 2:
        res = analizza_tennis(tn_db, q[0], q[1])
        if res:
            livello, titolo, summ, full = report_tennis(res, q)
            m["pick_str"] = f"🎯 {titolo}"
            m["pick_title"] = titolo
            m["summary_lines"] = summ
            m["full_report"] = full

# Precarica
load_saved_favorites()
for m in favorites.values():
    elabora_algoritmo(m)

# =====================================================================
#  ROTTE API E INTERFACCIA WEB
# =====================================================================
@app.route("/")
def index():
    return render_mobile_html()

@app.route("/api/data")
def api_data():
    fav_list = list(favorites.values())
    return jsonify({
        "count": len(fav_list),
        "favorites": fav_list,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S")
    })

@app.route("/api/palinsesto")
def api_palinsesto():
    pal = get_palinsesto()
    return jsonify({
        "count": len(pal),
        "matches": pal
    })

@app.route("/api/add", methods=["POST"])
def api_add():
    data = request.get_json(silent=True) or request.form
    val = (data.get("url") or data.get("mid") or "").strip()
    if not val:
        return jsonify({"status": "error", "message": "Link vuoto"}), 400

    mid = None
    m_q = re.search(r'[?&]mid=([a-zA-Z0-9]{8})', val)
    if m_q: mid = m_q.group(1)
    elif re.search(r'/(?:partita|match)/[a-zA-Z0-9\-]+/([a-zA-Z0-9]{8})', val):
        mid = re.search(r'/(?:partita|match)/[a-zA-Z0-9\-]+/([a-zA-Z0-9]{8})', val).group(1)
    elif re.search(r'/(?:partita|match)/([a-zA-Z0-9]{8})', val):
        mid = re.search(r'/(?:partita|match)/([a-zA-Z0-9]{8})', val).group(1)
    elif re.match(r'^[a-zA-Z0-9]{8}$', val): mid = val
    else:
        tokens = re.findall(r'[a-zA-Z0-9]{8}', val)
        if tokens: mid = tokens[0]

    if not mid:
        return jsonify({"status": "error", "message": "ID partita non trovato"}), 400

    # Cerca prima nel palinsesto cached
    pal = get_palinsesto()
    match_item = None
    for item in pal:
        if item["id"] == mid:
            match_item = dict(item)
            break

    if not match_item:
        for sp in [1, 2]:
            for d in [0, 1, -1, 2, 3, 4, 5, 6, 7]:
                res = FlashscorePalinsestoService.fetch_all_today(sport_id=sp, day_offset=d)
                for rm in res:
                    if rm["id"] == mid:
                        match_item = dict(rm)
                        break
                if match_item: break
            if match_item: break

    if not match_item:
        for base_url in [f"https://www.diretta.it/partita/{mid}/", f"https://www.flashscore.it/partita/{mid}/"]:
            try:
                r_p = requests.get(base_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
                if r_p.status_code == 200:
                    tm = re.search(r'<title>(.*?)</title>', r_p.text)
                    if tm:
                        title_txt = tm.group(1)
                        is_tennis = "tennis" in title_txt.lower()
                        sp_type = "Tennis" if is_tennis else "Calcio"
                        parts = title_txt.split(":")[-1].split("-")[0].strip()
                        teams = re.split(r'\s+v\s+|\s+vs\s+|\s+-\s+', parts)
                        home = teams[0].strip() if len(teams) > 0 else "Casa"
                        away = teams[1].strip() if len(teams) > 1 else "Ospite"
                        match_item = {
                            "id": mid,
                            "sport": sp_type,
                            "league": "Incontro Flashscore",
                            "home": home,
                            "away": away,
                            "time": "--:--",
                            "timestamp": 0,
                            "status_code": "1",
                            "score": "- : -",
                            "odds": None,
                            "odds_str": "Quote N.D."
                        }
                        break
            except Exception: pass

    if match_item:
        elabora_algoritmo(match_item)
        favorites[mid] = match_item
        save_favorites()
        return jsonify({"status": "ok", "match": match_item})
    return jsonify({"status": "error", "message": "Incontro non trovato su Flashscore"}), 404

@app.route("/api/remove", methods=["POST"])
def api_remove():
    data = request.get_json(silent=True) or request.form
    mid = data.get("mid")
    if mid and mid in favorites:
        del favorites[mid]
        save_favorites()
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"}), 400

@app.route("/download/schedina.txt")
def download_schedina():
    lines = ["=== SCHEDINA PREFERITI (QUOTE BOOSTER) ==="]
    for m in favorites.values():
        lines.append(f"• {m.get('time', '--:--')} | {m.get('league', '')}")
        lines.append(f"  {m.get('home', '')} vs {m.get('away', '')}  [{m.get('score', '-:-')}]")
        lines.append(f"  Quote: {m.get('odds_str', 'N.D.')}")
        if m.get('pick_str'):
            lines.append(f"  Pronostico: {m.get('pick_str')} - {m.get('pick_title', '')}")
        lines.append("-" * 35)
    text = "\n".join(lines)
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=schedina_preferiti.txt"}
    )

def render_mobile_html():
    fav_cards_html = []
    for m in favorites.values():
        mid = m.get("id")
        home = m.get("home", "Casa")
        away = m.get("away", "Ospite")
        league = m.get("league", "Generale")
        sport = m.get("sport", "Calcio")
        time_str = m.get("time", "--:--")
        score = m.get("score", "- : -")
        odds_str = m.get("odds_str", "Quote N.D.")
        pick_str = m.get("pick_str", "In attesa")
        pick_title = m.get("pick_title", "")
        summary = "<br>".join(m.get("summary_lines", []))
        sp_icon = "🎾" if sport == "Tennis" else "⚽"
        score_badge = f'<span class="score">{score}</span>' if score and score != "- : -" else ""
        full_rep_text = "\n".join(str(x) for x in m.get("full_report", []))
        details_block = f'<button class="details-btn" onclick="toggleDetails(this)">Visualizza Analisi Completa ▼</button><div class="details-panel">{full_rep_text}</div>' if full_rep_text else ''

        card_html = f'''
        <div class="card" id="card_{mid}">
            <div class="card-top">
                <span class="league-badge">{sp_icon} {league}</span>
                <span class="match-time">⏱ {time_str}</span>
            </div>
            <div class="teams">{home} vs {away} {score_badge}</div>
            <div class="odds-row">
                <div class="odd-pill"><div class="odd-lbl">QUOTE REALI</div><div class="odd-val">{odds_str}</div></div>
            </div>
            <div class="pick-box">
                <div class="pick-tag">{pick_str}</div>
                <div style="font-size: 11px; font-weight:700; color: #f8fafc; margin-bottom: 4px;">{pick_title}</div>
                <div class="pick-lines">{summary}</div>
                {details_block}
            </div>
            <div style="text-align: right; margin-top: 6px;">
                <button onclick="rimuoviPartita('{mid}')" style="background:none; border:none; color:#ef4444; font-size:11px; font-weight:700; cursor:pointer;">✕ Rimuovi dai Preferiti</button>
            </div>
        </div>
        '''
        fav_cards_html.append(card_html)

    cards_content = "\n".join(fav_cards_html) if fav_cards_html else '<div style="text-align:center; padding: 40px 20px; color: var(--dim);">Nessuna partita preferita attualmente salvata.<br><br>👉 Vai nella scheda <b>⚽ Palinsesto Oggi</b> per scegliere le partite con 1 tap!</div>'
    n_fav = len(favorites)

    return f'''<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Quote Booster Cloud</title>
    <meta name="theme-color" content="#0b0f19">
    <style>
        :root {{
            --bg: #0b0f19;
            --card: #161f30;
            --card-sub: #1e293b;
            --border: #25334d;
            --txt: #f8fafc;
            --dim: #94a3b8;
            --acc: #38bdf8;
            --green: #22c55e;
            --amber: #f59e0b;
            --red: #ef4444;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--txt);
            padding: 10px;
            padding-bottom: 80px;
            line-height: 1.4;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 14px;
            background: var(--card);
            border-radius: 14px;
            border: 1px solid var(--border);
            margin-bottom: 10px;
        }}
        .logo {{ font-size: 15px; font-weight: 800; color: var(--acc); }}
        .live-tag {{
            font-size: 11px;
            font-weight: 700;
            color: var(--green);
            background: rgba(34, 197, 94, 0.15);
            padding: 3px 8px;
            border-radius: 20px;
            display: flex;
            align-items: center;
            gap: 4px;
        }}
        .live-dot {{ width: 6px; height: 6px; background: var(--green); border-radius: 50%; display: inline-block; }}

        /* Tabs Navigazione */
        .tabs-nav {{
            display: flex;
            gap: 6px;
            margin-bottom: 12px;
            background: var(--card);
            padding: 4px;
            border-radius: 12px;
            border: 1px solid var(--border);
        }}
        .tab-btn {{
            flex: 1;
            padding: 9px 6px;
            border: none;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
            background: transparent;
            color: var(--dim);
            transition: all 0.2s;
            text-align: center;
        }}
        .tab-btn.active {{
            background: var(--acc);
            color: #0b0f19;
            box-shadow: 0 2px 8px rgba(56, 189, 248, 0.3);
        }}

        .actions-bar {{ display: flex; gap: 8px; margin-bottom: 12px; }}
        .btn {{
            flex: 1;
            padding: 10px 6px;
            border-radius: 10px;
            border: none;
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
        }}
        .btn-green {{ background: var(--green); color: #0b0f19; }}
        .btn-sky {{ background: var(--acc); color: #0b0f19; }}
        .btn-dim {{ background: var(--card-sub); color: var(--txt); border: 1px solid var(--border); }}
        .btn:active {{ opacity: 0.8; transform: scale(0.98); }}

        .add-box {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px;
            margin-bottom: 12px;
        }}
        .add-form {{ display: flex; gap: 6px; }}
        .input-url {{
            flex: 1;
            background: var(--card-sub);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--txt);
            padding: 8px 10px;
            font-size: 12px;
            outline: none;
        }}

        .cards-list {{ display: flex; flex-direction: column; gap: 10px; }}
        .card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 12px;
        }}
        .card-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 11px;
            color: var(--dim);
            margin-bottom: 6px;
        }}
        .league-badge {{
            background: rgba(56, 189, 248, 0.12);
            color: var(--acc);
            padding: 2px 6px;
            border-radius: 6px;
            font-weight: 600;
        }}
        .match-time {{ font-weight: 700; color: var(--amber); }}
        .teams {{ font-size: 14px; font-weight: 700; color: var(--txt); margin-bottom: 8px; }}
        .score {{ color: var(--green); margin-left: 6px; }}
        .odds-row {{ display: flex; gap: 6px; margin-bottom: 10px; }}
        .odd-pill {{
            flex: 1;
            background: var(--card-sub);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 8px;
            padding: 6px 8px;
            text-align: center;
            font-size: 11px;
        }}
        .odd-lbl {{ color: var(--dim); font-size: 10px; font-weight: 600; }}
        .odd-val {{ font-weight: 700; color: var(--txt); margin-top: 2px; }}
        .pick-box {{
            background: linear-gradient(135deg, rgba(34,197,94,0.1) 0%, rgba(56,189,248,0.1) 100%);
            border: 1px solid rgba(34,197,94,0.3);
            border-radius: 10px;
            padding: 10px;
        }}
        .pick-tag {{ font-size: 13px; font-weight: 800; color: var(--green); margin-bottom: 4px; }}
        .pick-lines {{ font-size: 11px; color: var(--dim); line-height: 1.4; }}
        .details-btn {{
            background: none;
            border: none;
            color: var(--acc);
            font-size: 11px;
            font-weight: 600;
            margin-top: 6px;
            cursor: pointer;
            text-decoration: underline;
        }}
        .details-panel {{
            display: none;
            margin-top: 8px;
            padding-top: 8px;
            border-top: 1px solid rgba(255,255,255,0.08);
            font-size: 11px;
            color: #cbd5e1;
            white-space: pre-line;
            font-family: monospace;
        }}

        /* Palinsesto Items */
        .pal-filter-pills {{
            display: flex;
            gap: 6px;
            margin-bottom: 10px;
            overflow-x: auto;
            padding-bottom: 2px;
        }}
        .pill-btn {{
            background: var(--card-sub);
            border: 1px solid var(--border);
            color: var(--dim);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s ease;
        }}
        .pill-btn.active {{
            background: rgba(14, 165, 233, 0.2);
            border-color: var(--acc);
            color: var(--acc);
        }}
        .pal-league-hdr {{
            background: rgba(30, 41, 59, 0.85);
            border-left: 3px solid var(--acc);
            color: #f1f5f9;
            padding: 7px 12px;
            font-size: 11px;
            font-weight: 800;
            border-radius: 6px;
            margin-top: 12px;
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            letter-spacing: 0.3px;
        }}
        .pal-league-hdr.top {{
            border-left-color: #f59e0b;
            background: rgba(245, 158, 11, 0.14);
            color: #fbbf24;
        }}
        .pal-search {{
            width: 100%;
            background: var(--card-sub);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--txt);
            padding: 9px 12px;
            font-size: 13px;
            outline: none;
            margin-bottom: 10px;
        }}
        .pal-card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }}
        .pal-info {{ flex: 1; }}
        .pal-teams {{ font-size: 13px; font-weight: 700; color: var(--txt); margin: 3px 0; }}
        .pal-odds {{ font-size: 11px; color: var(--acc); font-weight: 600; }}
        .pal-btn-add {{
            background: rgba(34, 197, 94, 0.2);
            border: 1px solid var(--green);
            color: var(--green);
            padding: 7px 12px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
        }}
        .pal-btn-add.added {{
            background: var(--green);
            color: #0b0f19;
            border-color: var(--green);
        }}

        .toast {{
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(15, 23, 42, 0.95);
            border: 1px solid var(--acc);
            color: #fff;
            padding: 10px 18px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 700;
            box-shadow: 0 4px 16px rgba(0,0,0,0.5);
            display: none;
            z-index: 9999;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">⚡ QUOTE BOOSTER CLOUD</div>
        <div class="live-tag"><span class="live-dot"></span> ATTIVO 24/7</div>
    </div>

    <!-- TABS -->
    <div class="tabs-nav">
        <button id="btnTabFav" class="tab-btn active" onclick="switchTab('fav')">⭐ I Miei Preferiti ({n_fav})</button>
        <button id="btnTabPal" class="tab-btn" onclick="switchTab('pal')">⚽ Palinsesto Oggi</button>
    </div>

    <!-- SEZIONE PREFERITI -->
    <div id="sectionFav">
        <div class="actions-bar">
            <button class="btn btn-green" onclick="copiaSchedina()">📋 Copia Schedina</button>
            <a class="btn btn-sky" href="/download/schedina.txt" download>📥 Scarica .txt</a>
            <button class="btn btn-dim" onclick="location.reload()">🔄 Ricarica</button>
        </div>

        <div class="add-box">
            <form class="add-form" onsubmit="aggiungiPartita(event)">
                <input type="text" id="addInput" class="input-url" placeholder="Incolla link Diretta/Flashscore o ID..." required>
                <button type="submit" class="btn btn-sky" style="flex: 0 0 80px;">Aggiungi</button>
            </form>
        </div>

        <div class="cards-list" id="cardsContainer">
            {cards_content}
        </div>
    </div>

    <!-- SEZIONE PALINSESTO -->
    <div id="sectionPal" style="display: none;">
        <div class="pal-filter-pills">
            <button class="pill-btn active" id="pillAll" onclick="setSportFilter('ALL')">⭐ Tutti</button>
            <button class="pill-btn" id="pillFb" onclick="setSportFilter('Calcio')">⚽ Calcio</button>
            <button class="pill-btn" id="pillTn" onclick="setSportFilter('Tennis')">🎾 Tennis</button>
            <button class="pill-btn" id="pillTop" onclick="setSportFilter('TOP')">🔥 Principali</button>
        </div>
        <input type="text" id="palSearchInput" class="pal-search" placeholder="🔍 Cerca squadra o torneo di oggi..." oninput="applicaFiltriPalinsesto()">
        <div style="font-size: 11px; color: var(--dim); margin-bottom: 8px;">Tocca <b>⭐ Aggiungi</b> per calcolare il pronostico all'istante:</div>
        <div id="palinsestoContainer" class="cards-list">
            <div style="text-align: center; padding: 20px; color: var(--dim);">Caricamento palinsesto in corso...</div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let palinsestoData = [];
        let favIds = {list(favorites.keys())};
        let currentSportFilter = 'ALL';

        function showToast(msg) {{
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => {{ t.style.display = 'none'; }}, 2500);
        }}

        function switchTab(tab) {{
            if (tab === 'fav') {{
                document.getElementById('sectionFav').style.display = 'block';
                document.getElementById('sectionPal').style.display = 'none';
                document.getElementById('btnTabFav').classList.add('active');
                document.getElementById('btnTabPal').classList.remove('active');
            }} else {{
                document.getElementById('sectionFav').style.display = 'none';
                document.getElementById('sectionPal').style.display = 'block';
                document.getElementById('btnTabFav').classList.remove('active');
                document.getElementById('btnTabPal').classList.add('active');
                if (palinsestoData.length === 0) caricaPalinsesto();
            }}
        }}

        function setSportFilter(filter) {{
            currentSportFilter = filter;
            document.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
            if (filter === 'ALL') document.getElementById('pillAll').classList.add('active');
            else if (filter === 'Calcio') document.getElementById('pillFb').classList.add('active');
            else if (filter === 'Tennis') document.getElementById('pillTn').classList.add('active');
            else if (filter === 'TOP') document.getElementById('pillTop').classList.add('active');
            applicaFiltriPalinsesto();
        }}

        function applicaFiltriPalinsesto() {{
            const query = document.getElementById('palSearchInput').value.toLowerCase().trim();
            let filtered = palinsestoData;
            if (currentSportFilter === 'Calcio') {{
                filtered = filtered.filter(m => m.sport === 'Calcio');
            }} else if (currentSportFilter === 'Tennis') {{
                filtered = filtered.filter(m => m.sport === 'Tennis');
            }} else if (currentSportFilter === 'TOP') {{
                filtered = filtered.filter(m => m.is_top || (m.league_sort_key && m.league_sort_key[0] === 1));
            }}
            if (query) {{
                filtered = filtered.filter(m => 
                    (m.home && m.home.toLowerCase().includes(query)) ||
                    (m.away && m.away.toLowerCase().includes(query)) ||
                    (m.league && m.league.toLowerCase().includes(query))
                );
            }}
            renderPalinsesto(filtered);
        }}

        async function caricaPalinsesto() {{
            const container = document.getElementById('palinsestoContainer');
            try {{
                const res = await fetch('/api/palinsesto');
                const data = await res.json();
                palinsestoData = data.matches || [];
                applicaFiltriPalinsesto();
            }} catch (err) {{
                container.innerHTML = '<div style="color:var(--red); text-align:center;">Errore caricamento partite.</div>';
            }}
        }}

        function renderPalinsesto(matches) {{
            const container = document.getElementById('palinsestoContainer');
            if (matches.length === 0) {{
                container.innerHTML = '<div style="text-align:center; padding:20px; color:var(--dim);">Nessuna partita trovata con questo filtro.</div>';
                return;
            }}
            let html = '';
            let currLeague = '';
            matches.slice(0, 300).forEach(m => {{
                if (m.league !== currLeague) {{
                    currLeague = m.league;
                    const isTop = m.is_top || (m.league_sort_key && m.league_sort_key[0] === 1);
                    const spIcon = m.sport === 'Tennis' ? '🎾' : '🏆';
                    html += `
                    <div class="pal-league-hdr ${{isTop ? 'top' : ''}}">
                        <span>${{spIcon}} ${{isTop ? '🔥 ' : ''}}${{m.league}}</span>
                        ${{isTop ? '<span style="font-size:9px; background:#f59e0b; color:#000; padding:2px 6px; border-radius:4px; font-weight:800;">TOP</span>' : ''}}
                    </div>`;
                }}
                const isFav = favIds.includes(m.id);
                html += `
                <div class="pal-card">
                    <div class="pal-info">
                        <div style="font-size:10px; color:var(--dim);">${{m.time}} • ${{m.status_code === '2' ? '<span style=\"color:var(--red); font-weight:800;\">LIVE ' + m.score + '</span>' : 'Programmata'}}</div>
                        <div class="pal-teams">${{m.home}} vs ${{m.away}}</div>
                        <div class="pal-odds">Quote: ${{m.odds_str || 'N.D.'}}</div>
                    </div>
                    <button class="pal-btn-add ${{isFav ? 'added' : ''}}" id="btn_pal_${{m.id}}" onclick="aggiungiDaPalinsesto('${{m.id}}')">
                        ${{isFav ? '✓ In Schedina' : '⭐ Aggiungi'}}
                    </button>
                </div>
                `;
            }});
            if (matches.length > 300) {{
                html += `<div style="text-align:center; padding:15px; font-size:11px; color:var(--dim);">Mostrate prime 300 partite su ${{matches.length}}. Usa la barra di ricerca o i filtri per restringere.</div>`;
            }}
            container.innerHTML = html;
        }}

        async function aggiungiDaPalinsesto(mid) {{
            const btn = document.getElementById('btn_pal_' + mid);
            if (btn) btn.innerText = 'Elaborazione...';
            try {{
                const res = await fetch('/api/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url: mid }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    favIds.push(mid);
                    if (btn) {{
                        btn.classList.add('added');
                        btn.innerText = '✓ In Schedina';
                    }}
                    showToast('✓ Aggiunta e analizzata nei Preferiti!');
                }} else {{
                    if (btn) btn.innerText = '⭐ Aggiungi';
                    showToast('Errore: ' + data.message);
                }}
            }} catch (err) {{
                if (btn) btn.innerText = '⭐ Aggiungi';
                showToast('Errore di connessione');
            }}
        }}

        function toggleDetails(btn) {{
            const panel = btn.nextElementSibling;
            if (panel.style.display === 'block') {{
                panel.style.display = 'none';
                btn.innerText = 'Visualizza Analisi Completa ▼';
            }} else {{
                panel.style.display = 'block';
                btn.innerText = 'Nascondi Analisi ▲';
            }}
        }}

        async function copiaSchedina() {{
            try {{
                const res = await fetch('/download/schedina.txt');
                const text = await res.text();
                await navigator.clipboard.writeText(text);
                showToast('✓ Schedina copiata negli appunti!');
            }} catch (err) {{
                showToast('Errore durante la copia');
            }}
        }}

        async function rimuoviPartita(mid) {{
            if (!confirm('Rimuovere questa partita dai preferiti?')) return;
            try {{
                const res = await fetch('/api/remove', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ mid: mid }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    favIds = favIds.filter(id => id !== mid);
                    const el = document.getElementById('card_' + mid);
                    if (el) el.remove();
                    showToast('Partita rimossa');
                }}
            }} catch (err) {{
                showToast('Errore durante la rimozione');
            }}
        }}

        async function aggiungiPartita(e) {{
            e.preventDefault();
            const input = document.getElementById('addInput');
            const val = input.value.trim();
            if (!val) return;
            showToast('Analisi partita in corso...');
            try {{
                const res = await fetch('/api/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url: val }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Partita aggiunta con pronostico!');
                    input.value = '';
                    setTimeout(() => location.reload(), 800);
                }} else {{
                    showToast('Errore: ' + (data.message || 'Non trovata'));
                }}
            }} catch (err) {{
                showToast('Errore di connessione');
            }}
        }}
    </script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
