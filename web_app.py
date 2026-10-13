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
                o1, ox, o2 = float(o1), float(ox), float(o2)
            except (ValueError, TypeError):
                continue
            d = d or ""
            fb.append((d, h, a, res, o1, ox, o2))

    if "tennis_matches" in tabelle:
        q = ("SELECT match_date, tournament, surface, winner, loser, odds_b365_w, odds_b365_l, best_of "
             "FROM tennis_matches WHERE odds_b365_w > 1 AND odds_b365_l > 1 ORDER BY match_date")
        for d, t, s, w, l, ow, ol, bo in cur.execute(q):
            try:
                ow, ol = float(ow), float(ol)
            except (ValueError, TypeError):
                continue
            d = d or ""
            tn.append((d, t, s, w, l, ow, ol, str(bo or '3')))
    conn.close()
    return fb, tn

def analizza_calcio(rows, q1, qx, q2, ruolo="Tutti"):
    if not rows or any(x is None or x <= 1.0 for x in (q1, qx, q2)):
        return None
    tol1 = max(0.06, q1 * 0.05)
    tolx = max(0.15, qx * 0.06)
    tol2 = max(0.20, q2 * 0.07)

    q1_min, q1_max = q1 - tol1, q1 + tol1
    qx_min, qx_max = qx - tolx, qx + tolx
    q2_min, q2_max = q2 - tol2, q2 + tol2

    campione = []
    for r in rows:
        # r = (match_date, home_team, away_team, ftr, odds_b365_h, odds_b365_d, odds_b365_a)
        if ruolo == "Casa Favorita" and r[4] > r[6]:
            continue
        if ruolo == "Ospite Favorita" and r[6] >= r[4]:
            continue
        if (q1_min <= r[4] <= q1_max and
            qx_min <= r[5] <= qx_max and
            q2_min <= r[6] <= q2_max):
            campione.append(r)

    if not campione:
        return None

    tot = len(campione)
    cnt_1 = sum(1 for r in campione if r[3] == 'H')
    cnt_x = sum(1 for r in campione if r[3] == 'D')
    cnt_2 = sum(1 for r in campione if r[3] == 'A')

    pct_1 = cnt_1 / tot * 100
    pct_x = cnt_x / tot * 100
    pct_2 = cnt_2 / tot * 100

    sum_inv = (1.0 / q1) + (1.0 / qx) + (1.0 / q2)
    prob_imp_1 = (1.0 / q1) / sum_inv * 100
    prob_imp_x = (1.0 / qx) / sum_inv * 100
    prob_imp_2 = (1.0 / q2) / sum_inv * 100

    diff_1 = pct_1 - prob_imp_1
    diff_x = pct_x - prob_imp_x
    diff_2 = pct_2 - prob_imp_2

    fav_segno = '1' if q1 < q2 else '2'
    fav_pct = pct_1 if fav_segno == '1' else pct_2
    dog_double = 'X2' if fav_segno == '1' else '1X'
    dog_double_pct = (pct_x + pct_2) if fav_segno == '1' else (pct_1 + pct_x)

    if fav_pct >= 62.0:
        verdetto = f"SEGNO {fav_segno} (Vittoria Favorita confermata dai dati)"
        motivo = f"Il favorito storico converte nel {fav_pct:.1f}% dei casi su {tot} partite."
        pick = fav_segno
        pick_pct = fav_pct
        tipo = "singolo"
        livello = "ok"
    elif dog_double_pct >= 48.0 and diff_x > 2.0:
        verdetto = f"DOPPIA CHANCE {dog_double} (Trappola Favorito - Frequenza Pareggi/Rottura anomala)"
        motivo = f"La favorita nominale fallisce nel {dog_double_pct:.1f}% dei casi (Pareggio reale: {pct_x:.1f}%)."
        pick = dog_double
        pick_pct = dog_double_pct
        tipo = "doppia"
        livello = "warn"
    elif pct_x >= 30.0:
        verdetto = "SEGNO X (Alta densità di pareggio nel pattern)"
        motivo = f"Il pareggio si manifesta al {pct_x:.1f}%, molto sopra la media standard."
        pick = "X"
        pick_pct = pct_x
        tipo = "singolo"
        livello = "warn"
    else:
        verdetto = f"DOPPIA CHANCE {'1X' if q1 <= q2 else 'X2'} (Copertura prudenziale)"
        motivo = "Equilibrio statistico: convergenza non netta sul solo esito pieno."
        pick = '1X' if q1 <= q2 else 'X2'
        pick_pct = dog_double_pct
        tipo = "doppia"
        livello = "dim"

    return dict(n=tot, tol1=tol1, tolx=tolx, tol2=tol2,
                cnt={"1": cnt_1, "X": cnt_x, "2": cnt_2},
                freq={"1": pct_1, "X": pct_x, "2": pct_2},
                impl={"1": prob_imp_1, "X": prob_imp_x, "2": prob_imp_2},
                diff={"1": diff_1, "X": diff_x, "2": diff_2},
                fav_segno=fav_segno, fav_pct=fav_pct, dog_double=dog_double, dog_double_pct=dog_double_pct,
                verdetto=verdetto, motivo=motivo,
                pick=pick, pick_pct=pick_pct, tipo=tipo, livello=livello,
                ultimi=campione[-5:])

def analizza_tennis(rows, q1, q2, superficie="Tutte", formato="Tutti i formati"):
    if not rows or any(x is None or x <= 1.0 for x in (q1, q2)):
        return None
    q_fav = min(q1, q2)
    q_und = max(q1, q2)
    fav_idx = "1" if q1 <= q2 else "2"
    und_idx = "2" if q1 <= q2 else "1"

    tolf = max(0.05, q_fav * 0.05)
    tolu = max(0.12, q_und * 0.06)
    f_min, f_max = q_fav - tolf, q_fav + tolf
    u_min, u_max = q_und - tolu, q_und + tolu

    campione = []
    for r in rows:
        # r = (match_date, tournament, surface, winner, loser, odds_b365_w, odds_b365_l, best_of)
        if superficie != "Tutte" and str(r[2]).lower() != superficie.lower():
            continue
        if formato == "3 Set (ATP/WTA)" and str(r[7]) != "3":
            continue
        if formato == "5 Set (Slam)" and str(r[7]) != "5":
            continue
        ow, ol = r[5], r[6]
        if ((f_min <= ow <= f_max and u_min <= ol <= u_max) or
            (f_min <= ol <= f_max and u_min <= ow <= u_max)):
            campione.append(r)

    if not campione:
        return None

    tot = len(campione)
    cnt_fav = sum(1 for r in campione if r[5] < r[6])
    cnt_und = tot - cnt_fav

    pct_fav = cnt_fav / tot * 100
    pct_und = cnt_und / tot * 100

    sum_inv = (1.0 / q_fav) + (1.0 / q_und)
    prob_imp_fav = (1.0 / q_fav) / sum_inv * 100
    diff_fav = pct_fav - prob_imp_fav

    is_fav = [1 if r[5] < r[6] else 0 for r in campione]
    fail_streaks = [len(list(g)) for k, g in itertools.groupby(is_fav) if k == 0]
    max_fail = max(fail_streaks) if fail_streaks else 0
    avg_fail = (sum(fail_streaks) / len(fail_streaks)) if fail_streaks else 0

    if pct_fav >= 66.0:
        verdetto = f"VITTORIA SEGNO {fav_idx} (Favorito Solido nel contesto)"
        alert = "Tenuta alta: la quota bassa trova conferma nel campione analizzato."
        pick = fav_idx
        pick_pct = pct_fav
        livello = "ok"
    elif pct_und >= 45.0 or diff_fav <= -5.0:
        verdetto = f"SORPRESA / OVER SET: SEGNO {und_idx} COMPETITIVO (Allarme Trappola)"
        alert = f"ATTENZIONE: Lo sfavorito vince nel {pct_und:.1f}% dei casi. Quota {q_fav:.2f} ingannevole su questa superficie!"
        pick = und_idx
        pick_pct = pct_und
        livello = "bad"
    else:
        verdetto = "MATCH A ELEVATA VOLATILITÀ (Esito incerto / Monetina)"
        alert = f"Il favorito vince solo al {pct_fav:.1f}%. Il margine statistico non giustifica una quota bassa."
        pick = fav_idx
        pick_pct = pct_fav
        livello = "warn"

    return dict(n=tot, tolf=tolf, tolu=tolu,
                cnt_fav=cnt_fav, cnt_und=cnt_und,
                pct_fav=pct_fav, pct_und=pct_und,
                prob_imp_fav=prob_imp_fav, diff_fav=diff_fav,
                fav_idx=fav_idx, und_idx=und_idx,
                max_fail=max_fail, avg_fail=avg_fail,
                verdetto=verdetto, alert=alert,
                pick=pick, pick_pct=pick_pct, livello=livello,
                ultimi=campione[-5:])

def report_calcio(r, q):
    livello = r["livello"]
    titolo = r["verdetto"]
    summary = [
        f"• {r['n']} precedenti storici con pattern quote simile",
        f"• {r['motivo']}",
        f"• 1: {r['freq']['1']:.1f}% | X: {r['freq']['X']:.1f}% | 2: {r['freq']['2']:.1f}%",
        f"• Rottura Favorito (X o Ribaltone): {r['dog_double_pct']:.1f}%",
    ]
    full = [
        f"PRONOSTICO STATISTICO: {r['verdetto']}",
        f"Motivazione: {r['motivo']}",
        f"Campione: {r['n']} match storici analizzati",
        "",
        "DISTRIBUZIONE EFFETTIVA RISCONTRATA:",
        f"• Segno 1: {r['cnt']['1']} match ({r['freq']['1']:.1f}%) | Implicita Quota: {r['impl']['1']:.1f}% [{r['diff']['1']:+.1f}%]",
        f"• Segno X: {r['cnt']['X']} match ({r['freq']['X']:.1f}%) | Implicita Quota: {r['impl']['X']:.1f}% [{r['diff']['X']:+.1f}%]",
        f"• Segno 2: {r['cnt']['2']} match ({r['freq']['2']:.1f}%) | Implicita Quota: {r['impl']['2']:.1f}% [{r['diff']['2']:+.1f}%]",
        "",
        f"INDICATORE DI ROTTURA DEL FAVORITO: {r['dog_double_pct']:.1f}% delle volte",
        "",
        "ULTIMI 5 MATCH STORICI REGISTRATI:"
    ]
    for m in r["ultimi"]:
        full.append(f"[{m[0]}] {m[1]} vs {m[2]} -> Esito: {m[3]}")
    return livello, titolo, summary, full

def report_tennis(r, q):
    livello = r["livello"]
    titolo = r["verdetto"]
    summary = [
        f"• {r['n']} precedenti reali analizzati",
        f"• {r['alert']}",
        f"• Vittoria Favorito G{r['fav_idx']}: {r['pct_fav']:.1f}% (Attesa: {r['prob_imp_fav']:.1f}%)",
        f"• Sorpresa Sfavorito G{r['und_idx']}: {r['pct_und']:.1f}%",
    ]
    full = [
        f"PRONOSTICO STATISTICO: {r['verdetto']}",
        f"Nota: {r['alert']}",
        f"Campione: {r['n']} match storici analizzati",
        "",
        "DATI REALI vs QUOTE:",
        f"• Vittoria Favorito (G{r['fav_idx']}): {r['cnt_fav']} volte ({r['pct_fav']:.1f}%) [Attesa teorica: {r['prob_imp_fav']:.1f}%]",
        f"• Ribaltone Underdog (G{r['und_idx']}): {r['cnt_und']} volte ({r['pct_und']:.1f}%)",
        "",
        "CICLO DI ROTTURA (QUANDO IL FAVORITO SALTA):",
        f"• Tasso effettivo di ribaltone: {r['pct_und']:.1f}%",
        f"• Serie max di ribaltoni consecutivi: {r['max_fail']} match",
        f"• Media serie di ribaltoni: {r['avg_fail']:.2f} match",
        "",
        "ULTIMI 5 MATCH CAMPIONATI:"
    ]
    for m in r["ultimi"]:
        tag = "FAVORITO" if m[5] < m[6] else "RIBALTONE"
        full.append(f"[{m[0]}] {m[3]} b. {m[4]} ({tag})")
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

SCHEDINE_FILE = "schedine_salvate.json"
active_schedina_name = "Schedina Principale"
schedine_store = {}

def load_schedine_data():
    global schedine_store, favorites, active_schedina_name
    schedine_store = {}
    if os.path.exists(SCHEDINE_FILE):
        try:
            with open(SCHEDINE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                schedine_store = data.get("schedine", {})
                active_schedina_name = data.get("active", "Schedina Principale")
        except Exception:
            pass

    if not schedine_store:
        # Carica da FAV_FILE se esiste
        fav_legacy = {}
        if os.path.exists(FAV_FILE):
            try:
                with open(FAV_FILE, "r", encoding="utf-8") as f:
                    fav_legacy = json.load(f)
            except Exception:
                fav_legacy = {}
        schedine_store = {
            "Schedina Principale": fav_legacy
        }
        active_schedina_name = "Schedina Principale"

    if active_schedina_name not in schedine_store:
        active_schedina_name = list(schedine_store.keys())[0] if schedine_store else "Schedina Principale"
        if active_schedina_name not in schedine_store:
            schedine_store[active_schedina_name] = {}

    favorites = schedine_store[active_schedina_name]

def save_schedine_data():
    global schedine_store, favorites, active_schedina_name
    try:
        schedine_store[active_schedina_name] = favorites
        payload = {
            "active": active_schedina_name,
            "schedine": schedine_store
        }
        with open(SCHEDINE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        # Sincronizza anche con FAV_FILE per retrocompatibilità desktop
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favorites, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def load_saved_favorites():
    load_schedine_data()

def save_favorites():
    save_schedine_data()

def calcola_quota_effettiva(m):
    q = m.get("odds")
    if not q:
        return 1.00
    chosen = m.get("custom_pick") or m.get("raw_pick") or "1"
    sp = m.get("sport", "Calcio")
    try:
        if sp == "Calcio" and len(q) == 3:
            q1, qx, q2 = float(q[0]), float(q[1]), float(q[2])
            if chosen == "1": return q1
            elif chosen == "X": return qx
            elif chosen == "2": return q2
            elif chosen == "1X": return round(1.0 / ((1.0/q1) + (1.0/qx)), 2)
            elif chosen == "X2": return round(1.0 / ((1.0/qx) + (1.0/q2)), 2)
            elif chosen == "12": return round(1.0 / ((1.0/q1) + (1.0/q2)), 2)
            return q1
        elif sp == "Tennis" and len(q) >= 2:
            q1, q2 = float(q[0]), float(q[1])
            if "2" in chosen: return q2
            return q1
    except Exception:
        pass
    return 1.00

def elabora_algoritmo(m):
    q = m.get("odds")
    if not q:
        m["pick_str"] = "Quote N.D."
        m["pick_title"] = "In attesa delle quote ufficiali"
        m["summary_lines"] = ["• Quote al momento non disponibili sul feed"]
        m["full_report"] = ["Quote non disponibili."]
        m["raw_pick"] = None
        m["effective_quota"] = 1.00
        return
    sp = m.get("sport", "Calcio")
    if sp == "Calcio" and len(q) == 3:
        res = analizza_calcio(fb_db, q[0], q[1], q[2])
        if res:
            livello, verdetto, summ, full = report_calcio(res, q)
            icon = "🎯" if livello == "ok" else ("⚠️" if livello == "warn" else "🚨")
            m["livello"] = livello
            m["raw_pick"] = res["pick"]
            m["pick_str"] = f"{icon} {res['pick']} ({res['pick_pct']:.1f}%)"
            m["pick_title"] = verdetto
            m["summary_lines"] = summ
            m["full_report"] = full
    elif sp == "Tennis" and len(q) >= 2:
        res = analizza_tennis(tn_db, q[0], q[1])
        if res:
            livello, verdetto, summ, full = report_tennis(res, q)
            icon = "🎯" if livello == "ok" else ("⚠️" if livello == "warn" else "🚨")
            m["livello"] = livello
            m["raw_pick"] = res["pick"]
            m["pick_str"] = f"{icon} {res['pick']} ({res['pick_pct']:.1f}%)"
            m["pick_title"] = verdetto
            m["summary_lines"] = summ
            m["full_report"] = full

    m["effective_quota"] = calcola_quota_effettiva(m)

# Precarica
load_saved_favorites()
for m in favorites.values():
    elabora_algoritmo(m)

# =====================================================================
#  ROTTE API E INTERFACCIA WEB (CON HEADER ZERO-CACHE)
# =====================================================================
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/")
def index():
    return render_mobile_html()

@app.route("/api/data")
def api_data():
    fav_list = list(favorites.values())
    tot_quota = 1.0
    for m in fav_list:
        tot_quota *= m.get("effective_quota", 1.0)
    return jsonify({
        "count": len(fav_list),
        "favorites": fav_list,
        "active_schedina": active_schedina_name,
        "schedine_list": list(schedine_store.keys()),
        "total_quota": round(tot_quota, 2),
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
        return jsonify({"status": "ok", "match": match_item, "count": len(favorites)})
    return jsonify({"status": "error", "message": "Incontro non trovato su Flashscore"}), 404

@app.route("/api/remove", methods=["POST"])
def api_remove():
    data = request.get_json(silent=True) or request.form
    mid = data.get("mid")
    if mid and mid in favorites:
        del favorites[mid]
        save_favorites()
        return jsonify({"status": "ok", "count": len(favorites)})
    return jsonify({"status": "error"}), 400

@app.route("/api/clear", methods=["POST"])
def api_clear():
    global favorites
    favorites.clear()
    save_favorites()
    return jsonify({"status": "ok", "count": 0, "message": "Schedina svuotata con successo!"})

@app.route("/api/update_match", methods=["POST"])
def api_update_match():
    global favorites
    data = request.get_json(silent=True) or request.form
    mid = data.get("mid")
    if not mid or mid not in favorites:
        return jsonify({"status": "error", "message": "Partita non trovata"}), 404

    odds = data.get("odds")
    if odds and isinstance(odds, list) and len(odds) in (2, 3):
        try:
            favorites[mid]["odds"] = [float(x) for x in odds]
            favorites[mid]["odds_str"] = " / ".join(f"{float(x):.2f}" for x in odds)
        except Exception:
            pass

    custom_pick = data.get("custom_pick")
    if custom_pick is not None:
        favorites[mid]["custom_pick"] = str(custom_pick).strip()

    elabora_algoritmo(favorites[mid])
    save_favorites()
    return jsonify({"status": "ok", "match": favorites[mid]})

@app.route("/api/recalculate", methods=["POST"])
def api_recalculate():
    global favorites
    for m in favorites.values():
        elabora_algoritmo(m)
    save_favorites()
    return jsonify({"status": "ok", "count": len(favorites)})

@app.route("/api/schedine/save", methods=["POST"])
def api_schedine_save():
    global active_schedina_name, schedine_store, favorites
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Nome schedina obbligatorio"}), 400
    active_schedina_name = name
    schedine_store[name] = dict(favorites)
    save_schedine_data()
    return jsonify({"status": "ok", "name": name, "schedine_list": list(schedine_store.keys())})

@app.route("/api/schedine/load", methods=["POST"])
def api_schedine_load():
    global active_schedina_name, schedine_store, favorites
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    if not name or name not in schedine_store:
        return jsonify({"status": "error", "message": "Schedina non trovata"}), 404
    active_schedina_name = name
    favorites = schedine_store[name]
    save_schedine_data()
    return jsonify({
        "status": "ok",
        "name": name,
        "count": len(favorites),
        "favorites": list(favorites.values())
    })

@app.route("/api/schedine/delete", methods=["POST"])
def api_schedine_delete():
    global active_schedina_name, schedine_store, favorites
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    if not name or name not in schedine_store:
        return jsonify({"status": "error", "message": "Schedina non trovata"}), 404
    if len(schedine_store) <= 1:
        favorites.clear()
        save_schedine_data()
        return jsonify({"status": "ok", "message": "Schedina svuotata (unica presente)"})
    del schedine_store[name]
    if active_schedina_name == name:
        active_schedina_name = list(schedine_store.keys())[0]
        favorites = schedine_store[active_schedina_name]
    save_schedine_data()
    return jsonify({
        "status": "ok",
        "active": active_schedina_name,
        "schedine_list": list(schedine_store.keys())
    })

@app.route("/download/schedina.txt")
def download_schedina():
    tot_quota = 1.0
    lines = [
        f"=== {active_schedina_name.upper()} (QUOTE BOOSTER) ===",
        f"Data creazione: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "=" * 45
    ]
    for m in favorites.values():
        eq = m.get("effective_quota", 1.0)
        tot_quota *= eq
        pick_display = m.get("custom_pick") or m.get("raw_pick") or "N.D."
        lines.append(f"• {m.get('time', '--:--')} | {m.get('league', '')}")
        lines.append(f"  {m.get('home', '')} vs {m.get('away', '')}  [{m.get('score', '-:-')}]")
        lines.append(f"  Quote Ufficiali: {m.get('odds_str', 'N.D.')}")
        lines.append(f"  Pronostico Algoritmo: {m.get('pick_title', '')}")
        lines.append(f"  🎯 ESITO IN SCHEDINA: {pick_display}  (Quota: @{eq:.2f})")
        lines.append("-" * 45)

    lines.append(f"NUMERO PARTITE: {len(favorites)}")
    lines.append(f"MOLTIPLICATORE QUOTA TOTALE: @{tot_quota:.2f}")
    lines.append(f"SIMULAZIONE PUNTATA 10€ -> VINCITA: {tot_quota * 10:.2f}€")
    text = "\n".join(lines)
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=schedina_{active_schedina_name.replace(' ', '_')}.txt"}
    )

def render_mobile_html():
    fav_cards_html = []
    tot_quota = 1.0
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
        eq = m.get("effective_quota", 1.0)
        tot_quota *= eq
        current_pick = m.get("custom_pick") or m.get("raw_pick") or ""

        # Bottoni selezione esito schedina
        if sport == "Calcio":
            pick_pills = f'''
            <div class="pick-selector">
                <span class="pick-sel-lbl">Esito Schedina (@{eq:.2f}):</span>
                <button class="pick-btn {'active' if current_pick == '1' else ''}" onclick="cambiaEsito('{mid}', '1')">1</button>
                <button class="pick-btn {'active' if current_pick == 'X' else ''}" onclick="cambiaEsito('{mid}', 'X')">X</button>
                <button class="pick-btn {'active' if current_pick == '2' else ''}" onclick="cambiaEsito('{mid}', '2')">2</button>
                <button class="pick-btn {'active' if current_pick == '1X' else ''}" onclick="cambiaEsito('{mid}', '1X')">1X</button>
                <button class="pick-btn {'active' if current_pick == 'X2' else ''}" onclick="cambiaEsito('{mid}', 'X2')">X2</button>
                <button class="pick-btn {'active' if current_pick == '12' else ''}" onclick="cambiaEsito('{mid}', '12')">12</button>
            </div>
            '''
        else:
            pick_pills = f'''
            <div class="pick-selector">
                <span class="pick-sel-lbl">Esito Schedina (@{eq:.2f}):</span>
                <button class="pick-btn {'active' if '1' in current_pick else ''}" onclick="cambiaEsito('{mid}', 'G1')">G1</button>
                <button class="pick-btn {'active' if '2' in current_pick else ''}" onclick="cambiaEsito('{mid}', 'G2')">G2</button>
            </div>
            '''

        card_html = f'''
        <div class="card" id="card_{mid}">
            <div class="card-top">
                <span class="league-badge">{sp_icon} {league}</span>
                <span class="match-time">⏱ {time_str}</span>
            </div>
            <div class="teams">{home} vs {away} {score_badge}</div>
            <div class="odds-row">
                <div class="odd-pill">
                    <div class="odd-lbl">QUOTE UFFICIALI</div>
                    <div class="odd-val">{odds_str}</div>
                </div>
                <button class="btn-edit-odds" onclick="apriModificaQuote('{mid}', '{odds_str}', '{sport}')">✏️ Modifica Quote</button>
            </div>
            <div class="pick-box">
                <div class="pick-tag">{pick_str}</div>
                <div style="font-size: 11px; font-weight:700; color: #f8fafc; margin-bottom: 4px;">{pick_title}</div>
                <div class="pick-lines">{summary}</div>
                {pick_pills}
                {details_block}
            </div>
            <div style="text-align: right; margin-top: 8px;">
                <button onclick="rimuoviPartita('{mid}')" class="btn-remove">✕ Rimuovi dalla Schedina</button>
            </div>
        </div>
        '''
        fav_cards_html.append(card_html)

    cards_content = "\n".join(fav_cards_html) if fav_cards_html else '<div class="empty-slip-box"><span>📋 Schedina Attualmente Vuota</span><p>Tocca <b>⚽ Palinsesto Oggi</b> per aggiungere le partite con 1 tap, oppure incolla un link Diretta/Flashscore qui sopra!</p></div>'
    n_fav = len(favorites)
    schedine_options = "".join(f'<option value="{name}" {"selected" if name == active_schedina_name else ""}>{name} ({len(schedine_store.get(name, {}))})</option>' for name in schedine_store)

    return f'''<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Quote Booster Pro - Gestione Schedine</title>
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
            padding-bottom: 90px;
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
        .logo {{ font-size: 15px; font-weight: 800; color: var(--acc); display: flex; align-items: center; gap: 6px; }}
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

        /* Bar Gestione Multi-Schedine */
        .schedine-bar {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 8px 10px;
            margin-bottom: 10px;
            display: flex;
            gap: 6px;
            align-items: center;
            flex-wrap: wrap;
        }}
        .schedina-select {{
            flex: 1;
            min-width: 140px;
            background: var(--card-sub);
            color: var(--txt);
            border: 1px solid var(--border);
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 700;
        }}
        .btn-mini {{
            background: var(--card-sub);
            color: var(--txt);
            border: 1px solid var(--border);
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
        }}
        .btn-mini:hover {{ border-color: var(--acc); color: var(--acc); }}

        /* Box Statistiche Quota & Vincita Schedina */
        .schedina-summary-box {{
            background: linear-gradient(135deg, rgba(30, 41, 59, 0.9), rgba(15, 23, 42, 0.9));
            border: 1px solid rgba(56, 189, 248, 0.3);
            border-radius: 12px;
            padding: 10px 12px;
            margin-bottom: 12px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.3);
        }}
        .schedina-summary-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .summary-badge {{
            display: flex;
            flex-direction: column;
        }}
        .summary-badge .lbl {{ font-size: 9px; text-transform: uppercase; color: var(--dim); font-weight: 700; }}
        .summary-badge .val {{ font-size: 16px; font-weight: 800; color: #fff; }}
        .summary-badge .val.highlight {{ color: var(--acc); }}
        .summary-badge .val.win {{ color: var(--green); }}
        .stake-wrap {{
            display: flex;
            align-items: center;
            gap: 6px;
            background: rgba(0,0,0,0.25);
            padding: 4px 8px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .stake-input {{
            width: 44px;
            background: none;
            border: none;
            color: #fff;
            font-size: 13px;
            font-weight: 800;
            text-align: center;
            outline: none;
        }}

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
            background: transparent;
            color: var(--dim);
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .tab-btn.active {{
            background: var(--acc);
            color: #0b0f19;
            box-shadow: 0 2px 8px rgba(56, 189, 248, 0.3);
        }}

        /* Barra Azioni Schedina */
        .actions-bar {{
            display: flex;
            gap: 6px;
            margin-bottom: 12px;
            flex-wrap: wrap;
        }}
        .btn {{
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 700;
            border: none;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            text-decoration: none;
            transition: opacity 0.2s;
        }}
        .btn:active {{ opacity: 0.8; }}
        .btn-green {{ background: var(--green); color: #0b0f19; }}
        .btn-sky {{ background: var(--acc); color: #0b0f19; }}
        .btn-dim {{ background: var(--card-sub); color: var(--txt); border: 1px solid var(--border); }}
        .btn-red {{ background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }}
        .btn-red:hover {{ background: var(--red); color: #fff; }}

        /* Add Input */
        .add-box {{
            background: var(--card);
            padding: 8px;
            border-radius: 12px;
            border: 1px solid var(--border);
            margin-bottom: 12px;
        }}
        .add-form {{ display: flex; gap: 6px; }}
        .input-url {{
            flex: 1;
            background: var(--card-sub);
            border: 1px solid var(--border);
            color: var(--txt);
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 12px;
            outline: none;
        }}

        /* Cards Schedina */
        .cards-list {{ display: flex; flex-direction: column; gap: 10px; }}
        .card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 12px;
            position: relative;
            box-shadow: 0 4px 12px rgba(0,0,0,0.25);
        }}
        .card-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
        }}
        .league-badge {{ font-size: 10px; color: var(--dim); font-weight: 700; text-transform: uppercase; }}
        .match-time {{ font-size: 11px; color: var(--acc); font-weight: 700; }}
        .teams {{ font-size: 14px; font-weight: 800; color: #fff; margin-bottom: 8px; }}
        .score {{ color: var(--red); font-weight: 800; margin-left: 6px; }}
        .odds-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-sub);
            padding: 6px 10px;
            border-radius: 8px;
            margin-bottom: 8px;
        }}
        .odd-pill {{ display: flex; flex-direction: column; }}
        .odd-lbl {{ font-size: 9px; color: var(--dim); font-weight: 700; }}
        .odd-val {{ font-size: 12px; font-weight: 800; color: #fff; letter-spacing: 0.5px; }}
        .btn-edit-odds {{
            background: none;
            border: 1px solid var(--border);
            color: var(--acc);
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 10px;
            font-weight: 700;
            cursor: pointer;
        }}
        .btn-edit-odds:hover {{ border-color: var(--acc); }}

        /* Pick Box */
        .pick-box {{
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(56, 189, 248, 0.25);
            border-radius: 10px;
            padding: 8px 10px;
        }}
        .pick-tag {{
            display: inline-block;
            background: var(--acc);
            color: #0b0f19;
            font-size: 11px;
            font-weight: 800;
            padding: 2px 7px;
            border-radius: 6px;
            margin-bottom: 4px;
        }}
        .pick-lines {{ font-size: 10px; color: var(--dim); line-height: 1.4; margin-bottom: 6px; }}

        /* Selettore Esito */
        .pick-selector {{
            display: flex;
            align-items: center;
            gap: 4px;
            margin-top: 6px;
            padding-top: 6px;
            border-top: 1px solid rgba(255,255,255,0.08);
            flex-wrap: wrap;
        }}
        .pick-sel-lbl {{ font-size: 10px; color: #fff; font-weight: 700; margin-right: 4px; }}
        .pick-btn {{
            background: var(--card-sub);
            border: 1px solid var(--border);
            color: var(--dim);
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 10px;
            font-weight: 800;
            cursor: pointer;
        }}
        .pick-btn.active {{
            background: var(--green);
            color: #0b0f19;
            border-color: var(--green);
        }}

        /* Dettagli Algoritmo */
        .details-btn {{
            background: none;
            border: none;
            color: var(--acc);
            font-size: 10px;
            font-weight: 700;
            cursor: pointer;
            padding: 4px 0 0 0;
            display: block;
        }}
        .details-panel {{
            display: none;
            margin-top: 6px;
            padding: 8px;
            background: #0b0f19;
            border-radius: 6px;
            font-family: monospace;
            font-size: 10px;
            white-space: pre-wrap;
            color: #cbd5e1;
            border: 1px solid var(--border);
        }}
        .btn-remove {{
            background: none;
            border: none;
            color: var(--red);
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
        }}
        .btn-remove:hover {{ text-decoration: underline; }}

        .empty-slip-box {{
            text-align: center;
            padding: 35px 20px;
            background: var(--card);
            border-radius: 14px;
            border: 1px dashed var(--border);
            color: var(--dim);
        }}
        .empty-slip-box span {{ font-size: 14px; font-weight: 800; color: #fff; display: block; margin-bottom: 6px; }}
        .empty-slip-box p {{ font-size: 11px; line-height: 1.5; }}

        /* Palinsesto */
        .pal-filter-pills {{ display: flex; gap: 6px; margin-bottom: 10px; overflow-x: auto; padding-bottom: 2px; }}
        .pill-btn {{
            background: var(--card);
            border: 1px solid var(--border);
            color: var(--dim);
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
        }}
        .pill-btn.active {{ background: var(--acc); color: #0b0f19; border-color: var(--acc); }}
        .pal-search {{
            width: 100%;
            background: var(--card);
            border: 1px solid var(--border);
            color: var(--txt);
            padding: 8px 12px;
            border-radius: 10px;
            font-size: 12px;
            outline: none;
            margin-bottom: 10px;
        }}
        .pal-league-hdr {{
            background: var(--card-sub);
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 800;
            color: var(--txt);
            margin: 10px 0 6px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .pal-league-hdr.top {{ border-left: 3px solid #f59e0b; background: rgba(245, 158, 11, 0.12); }}
        .pal-card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 8px 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
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
        <div class="logo">⚡ QUOTE BOOSTER PRO</div>
        <div class="live-tag"><span class="live-dot"></span> LIVE 24/7</div>
    </div>

    <!-- SELETTORE MULTI-SCHEDINE -->
    <div class="schedine-bar">
        <select id="selSchedina" class="schedina-select" onchange="cambiaSchedina(this.value)">
            {schedine_options}
        </select>
        <button class="btn-mini" onclick="creaNuovaSchedina()">➕ Nuova</button>
        <button class="btn-mini" onclick="salvaSchedinaConNome()">💾 Salva con nome</button>
        <button class="btn-mini" style="color:var(--red);" onclick="eliminaSchedinaCorrente()">🗑️ Elimina</button>
    </div>

    <!-- TABS -->
    <div class="tabs-nav">
        <button id="btnTabFav" class="tab-btn active" onclick="switchTab('fav')">⭐ Schedina Attiva (<span id="tabCount">{n_fav}</span>)</button>
        <button id="btnTabPal" class="tab-btn" onclick="switchTab('pal')">⚽ Palinsesto Oggi</button>
    </div>

    <!-- SEZIONE SCHEDINA ATTIVA -->
    <div id="sectionFav">
        <!-- BOX MOLTIPLICATORE & VINCITA -->
        <div class="schedina-summary-box">
            <div class="schedina-summary-row">
                <div class="summary-badge">
                    <span class="lbl">Partite in Schedina</span>
                    <span id="statCount" class="val">{n_fav}</span>
                </div>
                <div class="summary-badge">
                    <span class="lbl">Moltiplicatore Quota</span>
                    <span id="statQuota" class="val highlight">@{tot_quota:.2f}</span>
                </div>
                <div class="summary-badge">
                    <span class="lbl">Puntata Simulazione</span>
                    <div class="stake-wrap">
                        <input type="number" id="inpStake" class="stake-input" value="10" min="1" step="1" oninput="aggiornaCalcoloVincita()">
                        <span style="font-size:12px; font-weight:700;">€</span>
                    </div>
                </div>
                <div class="summary-badge">
                    <span class="lbl">Vincita Stimata</span>
                    <span id="statVincita" class="val win">€ {tot_quota * 10:.2f}</span>
                </div>
            </div>
        </div>

        <div class="actions-bar">
            <button class="btn btn-green" onclick="copiaSchedina()">📋 Copia Schedina</button>
            <a class="btn btn-sky" href="/download/schedina.txt" download>📥 Scarica .txt</a>
            <button class="btn btn-dim" onclick="ricalcolaTutto()">🔄 Ricalcola Quote</button>
            <button class="btn btn-red" onclick="svuotaSchedina()">🗑️ Svuota Schedina</button>
        </div>

        <div class="add-box">
            <form class="add-form" onsubmit="aggiungiPartita(event)">
                <input type="text" id="addInput" class="input-url" placeholder="Incolla link Diretta/Flashscore o ID partita..." required>
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
        <div style="font-size: 11px; color: var(--dim); margin-bottom: 8px;">Tocca <b>⭐ Aggiungi</b> per inserire il match e calcolare il pronostico originale:</div>
        <div id="palinsestoContainer" class="cards-list">
            <div style="text-align: center; padding: 20px; color: var(--dim);">Caricamento palinsesto in corso...</div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let palinsestoData = [];
        let favIds = {list(favorites.keys())};
        let currentSportFilter = 'ALL';
        let currentTotalQuota = {tot_quota};

        function showToast(msg) {{
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => {{ t.style.display = 'none'; }}, 2500);
        }}

        function aggiornaCalcoloVincita() {{
            const stake = parseFloat(document.getElementById('inpStake').value) || 0;
            const win = currentTotalQuota * stake;
            document.getElementById('statVincita').innerText = '€ ' + win.toFixed(2);
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
                const res = await fetch('/api/palinsesto?_t=' + Date.now());
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
                const res = await fetch('/api/add?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url: mid }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    if (!favIds.includes(mid)) favIds.push(mid);
                    if (btn) {{
                        btn.classList.add('added');
                        btn.innerText = '✓ In Schedina';
                    }}
                    showToast('✓ Aggiunta alla Schedina!');
                    aggiornaDatiSchedina();
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
                const res = await fetch('/download/schedina.txt?_t=' + Date.now());
                const text = await res.text();
                await navigator.clipboard.writeText(text);
                showToast('✓ Schedina copiata negli appunti!');
            }} catch (err) {{
                showToast('Errore durante la copia');
            }}
        }}

        async function rimuoviPartita(mid) {{
            if (!confirm('Rimuovere questa partita dalla schedina?')) return;
            try {{
                const res = await fetch('/api/remove?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ mid: mid }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    favIds = favIds.filter(id => id !== mid);
                    const el = document.getElementById('card_' + mid);
                    if (el) el.remove();
                    const btnPal = document.getElementById('btn_pal_' + mid);
                    if (btnPal) {{
                        btnPal.classList.remove('added');
                        btnPal.innerText = '⭐ Aggiungi';
                    }}
                    showToast('Partita rimossa dalla schedina');
                    aggiornaDatiSchedina();
                }}
            }} catch (err) {{
                showToast('Errore durante la rimozione');
            }}
        }}

        async function svuotaSchedina() {{
            if (!confirm('⚠️ Sei sicuro di voler CANCELLARE TUTTA la schedina corrente?')) return;
            try {{
                const res = await fetch('/api/clear?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }}
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    favIds = [];
                    document.getElementById('cardsContainer').innerHTML = '<div class="empty-slip-box"><span>📋 Schedina Attualmente Vuota</span><p>Tocca <b>⚽ Palinsesto Oggi</b> per aggiungere le partite con 1 tap, oppure incolla un link Diretta/Flashscore qui sopra!</p></div>';
                    document.querySelectorAll('.pal-btn-add').forEach(b => {{
                        b.classList.remove('added');
                        b.innerText = '⭐ Aggiungi';
                    }});
                    currentTotalQuota = 1.0;
                    document.getElementById('statCount').innerText = '0';
                    document.getElementById('tabCount').innerText = '0';
                    document.getElementById('statQuota').innerText = '@1.00';
                    aggiornaCalcoloVincita();
                    showToast('✓ Tutta la schedina è stata cancellata!');
                }}
            }} catch (err) {{
                showToast('Errore durante lo svuotamento');
            }}
        }}

        async function cambiaEsito(mid, pick) {{
            try {{
                const res = await fetch('/api/update_match?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ mid: mid, custom_pick: pick }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Esito aggiornato a ' + pick);
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (err) {{
                showToast('Errore aggiornamento esito');
            }}
        }}

        async function apriModificaQuote(mid, quoteAttuali, sport) {{
            const promptMsg = sport === 'Calcio' 
                ? 'Inserisci le nuove quote 1 / X / 2 separate da spazio (es. 1.65 3.80 5.25):' 
                : 'Inserisci le nuove quote G1 / G2 separate da spazio (es. 1.50 2.60):';
            const val = prompt(promptMsg, quoteAttuali.split(' / ').join(' '));
            if (!val) return;
            const parts = val.trim().split(/\\s+/).map(x => parseFloat(x.replace(',', '.')));
            if (parts.some(isNaN) || (sport === 'Calcio' && parts.length !== 3) || (sport === 'Tennis' && parts.length !== 2)) {{
                alert('Formato quote non valido.');
                return;
            }}
            try {{
                const res = await fetch('/api/update_match?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ mid: mid, odds: parts }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Quote aggiornate e pronostico ricalcolato!');
                    setTimeout(() => location.reload(), 500);
                }}
            }} catch (err) {{
                showToast('Errore aggiornamento quote');
            }}
        }}

        async function ricalcolaTutto() {{
            showToast('Ricalcolo quote e pronostici storici in corso...');
            try {{
                const res = await fetch('/api/recalculate?_t=' + Date.now(), {{ method: 'POST' }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Ricalcolo completato con algoritmo originale!');
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (err) {{
                showToast('Errore durante il ricalcolo');
            }}
        }}

        async function creaNuovaSchedina() {{
            const nome = prompt('Inserisci il nome per la nuova schedina (es. Schedina Raddoppio, Schedina Sabato):');
            if (!nome || !nome.trim()) return;
            try {{
                await fetch('/api/clear?_t=' + Date.now(), {{ method: 'POST' }});
                const res = await fetch('/api/schedine/save?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: nome.trim() }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Creata nuova schedina: ' + nome);
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (err) {{
                showToast('Errore creazione schedina');
            }}
        }}

        async function salvaSchedinaConNome() {{
            const nome = prompt('Salva questa schedina con il nome:');
            if (!nome || !nome.trim()) return;
            try {{
                const res = await fetch('/api/schedine/save?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: nome.trim() }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Schedina salvata come: ' + nome);
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (err) {{
                showToast('Errore salvataggio schedina');
            }}
        }}

        async function cambiaSchedina(nome) {{
            try {{
                const res = await fetch('/api/schedine/load?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: nome }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('Caricamento: ' + nome);
                    setTimeout(() => location.reload(), 300);
                }}
            }} catch (err) {{
                showToast('Errore caricamento schedina');
            }}
        }}

        async function eliminaSchedinaCorrente() {{
            const sel = document.getElementById('selSchedina');
            const nome = sel.value;
            if (!confirm('Eliminare definitivamente la schedina \"' + nome + '\"?')) return;
            try {{
                const res = await fetch('/api/schedine/delete?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: nome }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('Schedina eliminata');
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (err) {{
                showToast('Errore eliminazione schedina');
            }}
        }}

        async function aggiornaDatiSchedina() {{
            try {{
                const res = await fetch('/api/data?_t=' + Date.now());
                const data = await res.json();
                favIds = data.favorites.map(m => m.id);
                document.getElementById('statCount').innerText = data.count;
                document.getElementById('tabCount').innerText = data.count;
                document.getElementById('statQuota').innerText = '@' + (data.total_quota || 1.0).toFixed(2);
                currentTotalQuota = data.total_quota || 1.0;
                aggiornaCalcoloVincita();
            }} catch (e) {{}}
        }}

        async function aggiungiPartita(e) {{
            e.preventDefault();
            const input = document.getElementById('addInput');
            const val = input.value.trim();
            if (!val) return;
            showToast('Analisi partita in corso con algoritmo originale...');
            try {{
                const res = await fetch('/api/add?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url: val }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Partita analizzata e aggiunta alla Schedina!');
                    input.value = '';
                    setTimeout(() => location.reload(), 600);
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
