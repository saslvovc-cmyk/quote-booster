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

fb_team_history = {}
tn_player_history = {}

def norm_name(s):
    if not s: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())

def carica_db(path):
    global fb_team_history, tn_player_history
    if not os.path.exists(path):
        return [], []
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    tabelle = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fb, tn = [], []
    fb_team_history = {}
    tn_player_history = {}

    if "football_matches" in tabelle:
        q = ("SELECT match_date, home_team, away_team, ftr, odds_b365_h, odds_b365_d, odds_b365_a, fthg, ftag "
             "FROM football_matches WHERE ftr IN ('H','D','A') "
             "AND odds_b365_h > 1 AND odds_b365_d > 1 AND odds_b365_a > 1 ORDER BY match_date ASC")
        for d, h, a, res, o1, ox, o2, hg, ag in cur.execute(q):
            try:
                o1, ox, o2 = float(o1), float(ox), float(o2)
                hg = float(hg or 0)
                ag = float(ag or 0)
            except (ValueError, TypeError):
                continue
            d = d or ""
            fb.append((d, h, a, res, o1, ox, o2, hg, ag))

            nh = norm_name(h)
            na = norm_name(a)
            if nh not in fb_team_history: fb_team_history[nh] = []
            if na not in fb_team_history: fb_team_history[na] = []
            out_h = 'V' if res == 'H' else ('P' if res == 'D' else 'S')
            out_a = 'V' if res == 'A' else ('P' if res == 'D' else 'S')
            fb_team_history[nh].append((d, a, True, out_h, hg, ag))
            fb_team_history[na].append((d, h, False, out_a, ag, hg))

    if "tennis_matches" in tabelle:
        q = ("SELECT match_date, tournament, surface, winner, loser, odds_b365_w, odds_b365_l, best_of, wsets, lsets "
             "FROM tennis_matches WHERE odds_b365_w > 1 AND odds_b365_l > 1 ORDER BY match_date ASC")
        for d, t, s, w, l, ow, ol, bo, ws, ls in cur.execute(q):
            try:
                ow, ol = float(ow), float(ol)
                ws = float(ws or 0)
                ls = float(ls or 0)
            except (ValueError, TypeError):
                continue
            d = d or ""
            tn.append((d, t, s, w, l, ow, ol, str(bo or '3'), ws, ls))

            nw = norm_name(w)
            nl = norm_name(l)
            if nw not in tn_player_history: tn_player_history[nw] = []
            if nl not in tn_player_history: tn_player_history[nl] = []
            is_3set = (ls > 0 and (ws + ls) >= 3)
            tn_player_history[nw].append((d, l, True, is_3set))
            tn_player_history[nl].append((d, w, False, is_3set))

    conn.close()
    return fb, tn

_forma_live_cache = {}

def get_forma_live(sport_id, mid, home="", away=""):
    """
    Recupera le ULTIME PARTITE DISPUTATE reali e ufficiali dal feed live H2H di Flashscore Ninja.
    Fornisce trend V-P-S, punti forma, rendimento specifico casa/trasferta e storico H2H aggiornato a oggi.
    """
    if not mid:
        return None
    sp_id = 1 if (sport_id == 1 or sport_id == "Calcio") else 2
    key = (sp_id, str(mid))
    if key in _forma_live_cache:
        return _forma_live_cache[key]

    url = f"https://global.flashscore.ninja/5/x/feed/df_hh_{sp_id}_{mid}_it_1"
    try:
        r = requests.get(url, headers=FS_HEADERS, timeout=3.5)
        if r.status_code == 200 and len(r.text) > 80:
            sections = []
            curr_title = ""
            curr_items = []
            
            for row in r.text.split("~"):
                f = {}
                for part in row.split(SEP1):
                    kv = part.split(SEP2)
                    if len(kv) == 2:
                        f[kv[0]] = kv[1]
                if "KB" in f:
                    if curr_items:
                        sections.append((curr_title, curr_items))
                    curr_title = f["KB"]
                    curr_items = []
                elif "KP" in f:
                    score = f.get("KL", "")
                    res_code = f.get("WIS", "") # w, d, l
                    loc = f.get("KS", "") # home, away
                    ku = int(f.get("KU", 0)) if f.get("KU", "").isdigit() else 0
                    kt = int(f.get("KT", 0)) if f.get("KT", "").isdigit() else 0
                    curr_items.append({
                        "res": res_code,
                        "score": score,
                        "loc": loc,
                        "ku": ku,
                        "kt": kt,
                        "opp_h": f.get("KJ", "").replace("*", "").strip(),
                        "opp_a": f.get("KK", "").replace("*", "").strip(),
                        "tourn": f.get("KF", "")
                    })
            if curr_items:
                sections.append((curr_title, curr_items))
                
            if len(sections) >= 2:
                matches_h = sections[0][1]
                matches_a = sections[1][1]
                matches_h2h = sections[2][1] if len(sections) >= 3 else []
                
                nh = norm_name(home)
                na = norm_name(away)

                if sp_id == 1:
                    # Calcio: ultime 5 partite generali
                    rec_h = matches_h[:5]
                    rec_a = matches_a[:5]
                    
                    trend_h = [('V' if m['res'] == 'w' else ('P' if m['res'] == 'd' else 'S')) for m in rec_h]
                    trend_a = [('V' if m['res'] == 'w' else ('P' if m['res'] == 'd' else 'S')) for m in rec_a]
                    
                    pts_h = sum(3 if x == 'V' else (1 if x == 'P' else 0) for x in trend_h)
                    pts_a = sum(3 if x == 'V' else (1 if x == 'P' else 0) for x in trend_a)
                    
                    str_h = "-".join(trend_h) if trend_h else "N.D."
                    str_a = "-".join(trend_a) if trend_a else "N.D."
                    
                    # Rendimento specifico: Home in Casa, Away Fuori casa
                    home_in_casa = [m for m in matches_h if m['loc'] == 'home'][:5]
                    away_in_trasf = [m for m in matches_a if m['loc'] == 'away'][:5]
                    
                    vh_c = sum(1 for m in home_in_casa if m['res'] == 'w')
                    ph_c = sum(1 for m in home_in_casa if m['res'] == 'd')
                    sh_c = sum(1 for m in home_in_casa if m['res'] == 'l')
                    pts_h_home = vh_c * 3 + ph_c
                    
                    va_t = sum(1 for m in away_in_trasf if m['res'] == 'w')
                    pa_t = sum(1 for m in away_in_trasf if m['res'] == 'd')
                    sa_t = sum(1 for m in away_in_trasf if m['res'] == 'l')
                    pts_a_away = va_t * 3 + pa_t
                    
                    # Scontri diretti H2H
                    h2h_str = ""
                    if matches_h2h:
                        rec_h2h = matches_h2h[:5]
                        vh = 0
                        vd = 0
                        va = 0
                        for m in rec_h2h:
                            n_kj = norm_name(m['opp_h'])
                            if nh in n_kj or (len(n_kj) >= 4 and n_kj in nh):
                                if m['ku'] > m['kt']: vh += 1
                                elif m['ku'] == m['kt']: vd += 1
                                else: va += 1
                            else:
                                if m['kt'] > m['ku']: vh += 1
                                elif m['ku'] == m['kt']: vd += 1
                                else: va += 1
                        h2h_str = f"H2H {len(rec_h2h)} match: {vh}V {home}, {vd}N, {va}V {away}"
                        
                    summary = f"Forma Ultime 5: {home} ({str_h}, {pts_h}pt | Casa: {vh_c}V-{ph_c}N-{sh_c}P) • {away} ({str_a}, {pts_a}pt | Trasf: {va_t}V-{pa_t}N-{sa_t}P)"
                    if h2h_str:
                        summary += f" • {h2h_str}"
                        
                    res_dict = {
                        "trend_h": str_h, "pts_h": pts_h, "pts_h_home": pts_h_home,
                        "trend_a": str_a, "pts_a": pts_a, "pts_a_away": pts_a_away,
                        "summary": summary, "has_data": True,
                        "h2h_str": h2h_str, "live_flashscore": True
                    }
                    _forma_live_cache[key] = res_dict
                    return res_dict
                    
                else:
                    # Tennis
                    rec_1 = matches_h[:6]
                    rec_2 = matches_a[:6]
                    
                    trend_1 = [('V' if m['res'] == 'w' else 'S') for m in rec_1]
                    trend_2 = [('V' if m['res'] == 'w' else 'S') for m in rec_2]
                    
                    w1 = sum(1 for x in trend_1 if x == 'V')
                    w2 = sum(1 for x in trend_2 if x == 'V')
                    
                    # Frequenza 3° set nelle ultime partite
                    ts1 = sum(1 for m in rec_1 if (m['ku'] + m['kt']) >= 3)
                    ts2 = sum(1 for m in rec_2 if (m['ku'] + m['kt']) >= 3)
                    
                    str_1 = "-".join(trend_1) if trend_1 else "N.D."
                    str_2 = "-".join(trend_2) if trend_2 else "N.D."
                    
                    # Scontri diretti H2H Tennis
                    h2h_str = ""
                    if matches_h2h:
                        rec_h2h = matches_h2h[:5]
                        vh = 0
                        va = 0
                        for m in rec_h2h:
                            n_kj = norm_name(m['opp_h'])
                            if nh in n_kj or (len(n_kj) >= 4 and n_kj in nh):
                                if m['ku'] > m['kt']: vh += 1
                                else: va += 1
                            else:
                                if m['kt'] > m['ku']: vh += 1
                                else: va += 1
                        h2h_str = f"H2H {len(rec_h2h)} match: {vh}V {home}, {va}V {away}"

                    summary = f"Forma: {home} ({str_1}) • {away} ({str_2})"
                    if (ts1 + ts2) > 0:
                        summary += f" • {ts1 + ts2} match recenti al 3° set"
                    if h2h_str:
                        summary += f" • {h2h_str}"
                        
                    res_dict = {
                        "trend_1": str_1, "w1": w1, "tot1": len(rec_1), "three_1": ts1,
                        "trend_2": str_2, "w2": w2, "tot2": len(rec_2), "three_2": ts2,
                        "summary": summary, "has_data": True, "live_flashscore": True,
                        "h2h_str": h2h_str
                    }
                    _forma_live_cache[key] = res_dict
                    return res_dict
    except Exception:
        pass
        
    return None

def calcola_forma_calcio(home, away, mid=None):
    if mid:
        live = get_forma_live(1, mid, home, away)
        if live and live.get("has_data"):
            return live
    nh = norm_name(home)
    na = norm_name(away)
    hist_h = fb_team_history.get(nh)
    if not hist_h:
        for k, v in fb_team_history.items():
            if (len(k) >= 4 and k in nh) or (len(nh) >= 4 and nh in k):
                hist_h = v
                break
    hist_a = fb_team_history.get(na)
    if not hist_a:
        for k, v in fb_team_history.items():
            if (len(k) >= 4 and k in na) or (len(na) >= 4 and na in k):
                hist_a = v
                break

    # Ultime 5 partite generali
    rec_h = hist_h[-5:] if hist_h else []
    rec_a = hist_a[-5:] if hist_a else []

    trend_h = [x[3] for x in reversed(rec_h)]
    trend_a = [x[3] for x in reversed(rec_a)]

    pts_h = sum(3 if x=='V' else (1 if x=='P' else 0) for x in trend_h)
    pts_a = sum(3 if x=='V' else (1 if x=='P' else 0) for x in trend_a)

    str_h = "-".join(trend_h) if trend_h else "N.D."
    str_a = "-".join(trend_a) if trend_a else "N.D."

    # Rendimento specifico Casa per Home e Trasferta per Away
    rec_h_home = [x for x in hist_h if x[2]][-5:] if hist_h else []
    rec_a_away = [x for x in hist_a if not x[2]][-5:] if hist_a else []

    vh_h = sum(1 for x in rec_h_home if x[3] == 'V')
    ph_h = sum(1 for x in rec_h_home if x[3] == 'P')
    sh_h = sum(1 for x in rec_h_home if x[3] == 'S')
    pts_h_home = vh_h * 3 + ph_h

    va_a = sum(1 for x in rec_a_away if x[3] == 'V')
    pa_a = sum(1 for x in rec_a_away if x[3] == 'P')
    sa_a = sum(1 for x in rec_a_away if x[3] == 'S')
    pts_a_away = va_a * 3 + pa_a

    # Gol fatti e subiti nelle ultime gare
    gf_h = sum(x[4] for x in rec_h)
    gs_h = sum(x[5] for x in rec_h)
    gf_a = sum(x[4] for x in rec_a)
    gs_a = sum(x[5] for x in rec_a)

    # Scontri Diretti H2H
    h2h_matches = []
    if hist_h:
        for m in hist_h:
            opp_n = norm_name(m[1])
            if (len(na) >= 4 and na in opp_n) or (len(opp_n) >= 4 and opp_n in na):
                h2h_matches.append(m)

    h2h_str = ""
    if h2h_matches:
        rh = h2h_matches[-4:]
        vh = sum(1 for m in rh if m[3] == 'V')
        ph = sum(1 for m in rh if m[3] == 'P')
        sh = sum(1 for m in rh if m[3] == 'S')
        h2h_str = f"H2H {len(rh)} match: {vh}V {home}, {ph}N, {sh}V {away}"

    summary = f"Forma Ultime 5: {home} ({str_h}, {pts_h}pt | Casa: {vh_h}V-{ph_h}N-{sh_h}P) • {away} ({str_a}, {pts_a}pt | Trasf: {va_a}V-{pa_a}N-{sa_a}P)"
    if h2h_str:
        summary += f" • {h2h_str}"

    return {
        "trend_h": str_h, "pts_h": pts_h, "pts_h_home": pts_h_home,
        "trend_a": str_a, "pts_a": pts_a, "pts_a_away": pts_a_away,
        "gf_h": gf_h, "gs_h": gs_h, "gf_a": gf_a, "gs_a": gs_a,
        "summary": summary, "has_data": bool(rec_h or rec_a),
        "h2h_str": h2h_str
    }
def calcola_forma_tennis(p1, p2, mid=None):
    if mid:
        live = get_forma_live(2, mid, p1, p2)
        if live and live.get("has_data"):
            return live
    n1 = norm_name(p1)
    n2 = norm_name(p2)
    hist_1 = tn_player_history.get(n1)
    if not hist_1:
        for k, v in tn_player_history.items():
            if (len(k) >= 4 and k in n1) or (len(n1) >= 4 and n1 in k):
                hist_1 = v
                break
    hist_2 = tn_player_history.get(n2)
    if not hist_2:
        for k, v in tn_player_history.items():
            if (len(k) >= 4 and k in n2) or (len(n2) >= 4 and n2 in k):
                hist_2 = v
                break

    rec_1 = hist_1[-6:] if hist_1 else []
    rec_2 = hist_2[-6:] if hist_2 else []

    trend_1 = [('V' if x[2] else 'S') for x in reversed(rec_1)]
    trend_2 = [('V' if x[2] else 'S') for x in reversed(rec_2)]

    w1 = sum(1 for x in rec_1 if x[2])
    w2 = sum(1 for x in rec_2 if x[2])

    three_1 = sum(1 for x in rec_1 if x[3])
    three_2 = sum(1 for x in rec_2 if x[3])

    str_1 = "-".join(trend_1) if trend_1 else "N.D."
    str_2 = "-".join(trend_2) if trend_2 else "N.D."

    summary = f"Forma: {p1} ({str_1}) • {p2} ({str_2})"
    if three_1 >= 2 or three_2 >= 2:
        summary += f" • {three_1 + three_2} match recenti al 3° set"

    return {
        "trend_1": str_1, "w1": w1, "tot1": len(rec_1), "three_1": three_1,
        "trend_2": str_2, "w2": w2, "tot2": len(rec_2), "three_2": three_2,
        "summary": summary, "has_data": bool(rec_1 or rec_2)
    }

def analizza_calcio(rows, q1, qx, q2, ruolo="Tutti", home="", away="", mid=None):
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

    # Collaborazione Quote Storiche + Statistiche Recenti (Ultime 5 partite, Casa/Trasferta, H2H)
    forma = calcola_forma_calcio(home, away, mid=mid) if (home and away) else None
    forma_reinforce = ""
    trappola_forma = False

    if forma and forma.get("has_data"):
        pts_h = forma.get("pts_h", 0)
        pts_a = forma.get("pts_a", 0)
        pts_h_home = forma.get("pts_h_home", 0)
        pts_a_away = forma.get("pts_a_away", 0)

        # 1. Rinforzo positivo quando quote e forma concordano
        if fav_segno == '1' and (pts_h >= 9 or pts_h_home >= 9) and (pts_a <= 6 or pts_a_away <= 4):
            forma_reinforce = f" (Rafforzato da Forma & Rendimento Casa: {home} {pts_h}pt vs {away} {pts_a}pt)"
        elif fav_segno == '2' and (pts_a >= 9 or pts_a_away >= 9) and (pts_h <= 6 or pts_h_home <= 4):
            forma_reinforce = f" (Rafforzato da Forma & Rendimento Trasferta: {away} {pts_a}pt vs {home} {pts_h}pt)"

        # 2. Allarme Trappola: Quota favorevole ma forma recente opposta!
        if fav_segno == '1' and pts_h <= 3 and pts_a >= 9:
            trappola_forma = True
        elif fav_segno == '2' and pts_a <= 3 and pts_h >= 9:
            trappola_forma = True

    if trappola_forma:
        verdetto = f"DOPPIA CHANCE {dog_double} (Allarme Trappola: Quota Favorito in forte contrasto con Crisi Recente)"
        motivo = f"Attenzione: la quota indica favorito {home if fav_segno=='1' else away}, ma la forma recente e il rendimento casa/trasferta mostrano trend opposto. Copertura prudenziale {dog_double} raccomandata."
        if forma and forma.get("has_data"):
            motivo += f" {forma['summary']}."
        pick = dog_double
        pick_pct = max(dog_double_pct, 55.0)
        tipo = "doppia"
        livello = "warn"
    elif fav_pct >= 62.0:
        verdetto = f"SEGNO {fav_segno}{forma_reinforce} (Vittoria Favorita confermata dai dati)"
        motivo = f"Il favorito storico converte nel {fav_pct:.1f}% dei casi su {tot} partite."
        if forma and forma.get("has_data"):
            motivo += f" {forma['summary']}."
        pick = fav_segno
        pick_pct = fav_pct
        tipo = "singolo"
        livello = "ok"
    elif dog_double_pct >= 48.0 and diff_x > 2.0:
        verdetto = f"DOPPIA CHANCE {dog_double} (Trappola Favorito - Frequenza Pareggi/Rottura anomala)"
        motivo = f"La favorita nominale fallisce nel {dog_double_pct:.1f}% dei casi (Pareggio reale: {pct_x:.1f}%)."
        if forma and forma.get("has_data"):
            motivo += f" {forma['summary']}."
        pick = dog_double
        pick_pct = dog_double_pct
        tipo = "doppia"
        livello = "warn"
    elif pct_x >= 30.0:
        verdetto = "SEGNO X (Alta densità di pareggio nel pattern)"
        motivo = f"Il pareggio si manifesta al {pct_x:.1f}%, molto sopra la media standard."
        if forma and forma.get("has_data"):
            motivo += f" {forma['summary']}."
        pick = "X"
        pick_pct = pct_x
        tipo = "singolo"
        livello = "warn"
    else:
        verdetto = f"DOPPIA CHANCE {'1X' if q1 <= q2 else 'X2'} (Copertura prudenziale)"
        motivo = "Equilibrio statistico: convergenza non netta sul solo esito pieno."
        if forma and forma.get("has_data"):
            motivo += f" {forma['summary']}."
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
                verdetto=verdetto, motivo=motivo, forma_summary=forma['summary'] if forma else '',
                pick=pick, pick_pct=pick_pct, tipo=tipo, livello=livello,
                ultimi=campione[-5:])
def analizza_tennis(rows, q1, q2, superficie="Tutte", formato="Tutti i formati", p1="", p2="", mid=None):
    if not rows or any(x is None or x <= 1.0 for x in (q1, q2)):
        return None
    q_fav = min(q1, q2)
    q_und = max(q1, q2)
    fav_idx = "G1" if q1 <= q2 else "G2"
    und_idx = "G2" if q1 <= q2 else "G1"

    tolf = max(0.05, q_fav * 0.05)
    tolu = max(0.12, q_und * 0.06)
    f_min, f_max = q_fav - tolf, q_fav + tolf
    u_min, u_max = q_und - tolu, q_und + tolu

    campione = []
    for r in rows:
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

    # Frequenza reale con cui il database storico delle quote ha prodotto il 3° set
    cnt_3set = sum(1 for r in campione if len(r) > 9 and r[9] > 0 and (r[8] + r[9]) >= 3)
    pct_3set = round((cnt_3set / tot * 100), 1) if tot > 0 else 35.0
    over_set_quota = round(max(1.75, min(2.50, 100.0 / max(pct_3set, 35.0))), 2)

    # Reperimento forma recente ufficiale e H2H
    forma = calcola_forma_tennis(p1, p2, mid=mid) if (p1 and p2) else None
    w1 = forma.get("w1", 0) if forma else 0
    tot1 = forma.get("tot1", 0) if forma else 0
    w2 = forma.get("w2", 0) if forma else 0
    tot2 = forma.get("tot2", 0) if forma else 0
    ts1 = forma.get("three_1", 0) if forma else 0
    ts2 = forma.get("three_2", 0) if forma else 0

    w_fav = w1 if fav_idx == "G1" else w2
    tot_fav = tot1 if fav_idx == "G1" else tot2
    w_und = w2 if fav_idx == "G1" else w1
    tot_und = tot2 if fav_idx == "G1" else tot1

    fav_name = p1 if fav_idx == "G1" else p2
    und_name = p2 if fav_idx == "G1" else p1

    # 1. VALUTAZIONE QUOTE: Il match è genuinamente equilibrato?
    diff_quote = abs(q1 - q2)
    is_quote_equilibrato = (diff_quote <= 0.45) and (q_fav >= 1.60)

    # 2. VALUTAZIONE DATABASE: La quota nel DB ha realmente prodotto il 3° set?
    db_ha_prodotto_3set = (pct_3set >= 41.5)

    # 3. VALUTAZIONE FORMA: Entrambi i tennisti lottano e vanno al 3° set
    forma_tendenza_3set = (ts1 + ts2) >= 3

    # Allarme Trappola sul favorito nominale
    trappola_favorito = (pct_und >= 42.0 and diff_fav <= -4.0) or (w_fav <= 1 and w_und >= 4 and tot_fav >= 4)

    # =========================================================================
    # DECISIONE: PRONOSTICO SECCO (G1 / G2) oppure TERZO SET (OVER 2.5 SET)
    # =========================================================================
    if trappola_favorito:
        verdetto = f"SORPRESA SEGNO {und_idx} (Allarme Trappola: Sfavorito Competitivo nei Dati)"
        alert = f"Attenzione: quota {q_fav:.2f} ingannevole nel DB. Lo sfavorito {und_name} converte nel {pct_und:.1f}% dei precedenti storici con trend recente favorevole."
        if forma and forma.get("has_data"): alert += f" • {forma['summary']}."
        pick = und_idx
        pick_pct = max(pct_und, 45.0)
        livello = "warn"

    elif is_quote_equilibrato and (db_ha_prodotto_3set or forma_tendenza_3set):
        verdetto = "OVER 2.5 SET / DECISIONE AL 3° SET (Match Equilibrato da Quote DB & Forma)"
        alert = f"Incontro molto equilibrato ({q1:.2f} vs {q2:.2f}): il pattern quote nel DB produce il 3° set nel {pct_3set:.1f}% dei casi. Pronostico ideale indipendentemente da chi vincerà!"
        if forma and forma.get("has_data"): alert += f" • {forma['summary']}."
        pick = "OVER 2.5 SET"
        pick_pct = max(pct_3set, 48.0)
        livello = "ok"

    elif pct_fav >= 58.0 or q_fav <= 1.65:
        forma_reinforce = ""
        if w_fav >= 4 and tot_fav >= 4:
            forma_reinforce = f" (Rafforzato da Forma Recente: {fav_name} {w_fav}/{tot_fav} V)"
        verdetto = f"VITTORIA SEGNO {fav_idx}{forma_reinforce} (Pronostico Secco Favorito confermato dal DB)"
        alert = f"Tenuta solida: il favorito converte nel {pct_fav:.1f}% dei precedenti storici su {tot} match nel DB (attesa {prob_imp_fav:.1f}%)."
        if forma and forma.get("has_data"): alert += f" • {forma['summary']}."
        pick = fav_idx
        pick_pct = pct_fav
        livello = "ok"

    else:
        pick_idx = fav_idx
        if tot1 >= 3 and tot2 >= 3:
            if (w2 / tot2) > (w1 / tot1) + 0.25 and fav_idx == "G1":
                pick_idx = "G2"
            elif (w1 / tot1) > (w2 / tot2) + 0.25 and fav_idx == "G2":
                pick_idx = "G1"

        verdetto = f"VITTORIA SEGNO {pick_idx} (Pronostico Secco nel Contesto)"
        alert = f"Valutazione secca: favorito al {pct_fav:.1f}%, frequenza 3° set al {pct_3set:.1f}% non sufficiente per l'Over."
        if forma and forma.get("has_data"): alert += f" • {forma['summary']}."
        pick = pick_idx
        pick_pct = pct_fav if pick_idx == fav_idx else pct_und
        livello = "dim"

    return dict(n=tot, tolf=tolf, tolu=tolu,
                cnt_fav=cnt_fav, cnt_und=cnt_und,
                pct_fav=pct_fav, pct_und=pct_und,
                cnt_3set=cnt_3set, pct_3set=pct_3set, over_set_quota=over_set_quota,
                prob_imp_fav=prob_imp_fav, diff_fav=diff_fav,
                fav_idx=fav_idx, und_idx=und_idx,
                max_fail=max_fail, avg_fail=avg_fail,
                verdetto=verdetto, alert=alert, forma_summary=forma['summary'] if forma else '',
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
    if r.get("forma_summary"):
        summary.append(f"• 📈 {r['forma_summary']}")

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
        f"• Frequenza 3° Set nel pattern: {r.get('pct_3set', 40.0):.1f}%",
        f"• Vittoria Favorito {r['fav_idx']}: {r['pct_fav']:.1f}% • Sorpresa {r['und_idx']}: {r['pct_und']:.1f}%",
    ]
    if r.get("forma_summary"):
        summary.append(f"• 📈 {r['forma_summary']}")

    full = [
        f"PRONOSTICO STATISTICO: {r['verdetto']}",
        f"Nota: {r['alert']}",
        f"Campione: {r['n']} match storici analizzati",
        "",
        "DATI REALI vs QUOTE:",
        f"• Vittoria Favorito ({r['fav_idx']}): {r['cnt_fav']} volte ({r['pct_fav']:.1f}%) [Attesa teorica: {r['prob_imp_fav']:.1f}%]",
        f"• Ribaltone Underdog ({r['und_idx']}): {r['cnt_und']} volte ({r['pct_und']:.1f}%)",
        f"• Incontri terminati al 3° set: {r.get('cnt_3set', 0)} su {r['n']} ({r.get('pct_3set', 0.0):.1f}%)",
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
# Paesi Europei (in italiano e inglese per matchare qualsiasi feed Flashscore)
EUROPE_COUNTRIES = {
    'ALBANIA', 'ANDORRA', 'ARMENIA', 'AUSTRIA', 'AZERBAIJAN', 'AZERBAIGIAN', 
    'BELGIUM', 'BELGIO', 'BELARUS', 'BIELORUSSIA', 'BOSNIA AND HERZEGOVINA', 
    'BOSNIA & HERZEGOVINA', 'BOSNIA ED ERZEGOVINA', 'BOSNIA', 'BULGARIA', 
    'CROATIA', 'CROAZIA', 'CYPRUS', 'CIPRO', 'CZECH REPUBLIC', 'CECHIA', 'REPUBBLICA CECA', 
    'DENMARK', 'DANIMARCA', 'ENGLAND', 'INGHILTERRA', 'ESTONIA', 'EUROPE', 'EUROPA', 
    'FAROE ISLANDS', 'ISOLE FAROE', 'FINLAND', 'FINLANDIA', 'FRANCE', 'FRANCIA', 
    'GEORGIA', 'GERMANY', 'GERMANIA', 'GIBRALTAR', 'GIBILTERRA', 'GREECE', 'GRECIA', 
    'HUNGARY', 'UNGHERIA', 'ICELAND', 'ISLANDA', 'IRELAND', 'IRLANDA', 'NORTHERN IRELAND', 'IRLANDA DEL NORD', 
    'ISRAEL', 'ISRAELE', 'ITALY', 'ITALIA', 'KAZAKHSTAN', 'KAZAKISTAN', 'KOSOVO', 
    'LATVIA', 'LETTONIA', 'LIECHTENSTEIN', 'LITHUANIA', 'LITUANIA', 'LUXEMBOURG', 'LUSSEMBURGO', 
    'MALTA', 'MOLDOVA', 'MOLDAVIA', 'MONTENEGRO', 'NETHERLANDS', 'OLANDA', 'PAESI BASSI', 
    'NORTH MACEDONIA', 'MACEDONIA', 'MACEDONIA DEL NORD', 'NORWAY', 'NORVEGIA', 
    'POLAND', 'POLONIA', 'PORTUGAL', 'PORTOGALLO', 'ROMANIA', 'RUSSIA', 'SAN MARINO', 
    'SCOTLAND', 'SCOZIA', 'SERBIA', 'SLOVAKIA', 'SLOVACCHIA', 'SLOVENIA', 
    'SPAIN', 'SPAGNA', 'SWEDEN', 'SVEZIA', 'SWITZERLAND', 'SVIZZERA', 
    'TURKEY', 'TURCHIA', 'UKRAINE', 'UCRAINA', 'WALES', 'GALLES'
}

# Parole chiave che indicano categorie da SCARTARE SEMPRE (3a divisione, 4a divisione, dilettanti, giovanili, amatori, riserve, donne)
STRICT_DISCARD_KEYWORDS = [
    # Categorie inferiori (3a, 4a, 5a divisione o dilettanti)
    'SERIE C', 'SERIE D', 'ECCELLENZA', 'PROMOZIONE', 'LEGA PRO',
    'LEAGUE ONE', 'LEAGUE TWO', 'NATIONAL LEAGUE', 'NORTHERN PREMIER', 'SOUTHERN PREMIER', 'ISTHMIAN',
    'LOWLAND LEAGUE', 'HIGHLAND LEAGUE',
    'PRIMERA RFEF', 'SEGUNDA RFEF', 'TERCERA RFEF', 'PRIMERA FEDERACION', 'SEGUNDA FEDERACION', 'TERCERA FEDERACION',
    '3. LIGA', '3.LIGA', '4. LIGA', '4.LIGA', '5. LIGA', '5.LIGA',
    'REGIONALLIGA', 'OBERLIGA', 'LANDESLIGA',
    'NATIONAL 1', 'NATIONAL 2', 'NATIONAL 3', 'CHAMPIONNAT NATIONAL',
    'SERIE C1', 'SERIE C2',
    '3. DIVISION', '3.DIVISION', '4. DIVISION', '4.DIVISION', '5. DIVISION', '5.DIVISION',
    'DIVISION 3', 'DIVISION 4', 'DIVISION 5',
    'THIRD LEAGUE', 'FOURTH LEAGUE', 'FIFTH LEAGUE',
    '3RD LEAGUE', '4TH LEAGUE', '5TH LEAGUE',
    '3. DEILD', '4. DEILD', '5. DEILD',
    'LIGA 3', 'LIGA 4', 'LIGA 5',
    'TERCERA DIVISION', 'SEGUNDA B', '2. LIGA INTERREGIONAL', '1. LIGA CLASSIC', '1.LIGA CLASSIC', 'PROMOTION LEAGUE',
    'TERZA DIVISIONE', 'QUARTA DIVISIONE', 'QUINTA DIVISIONE',
    '3A DIVISIONE', '4A DIVISIONE', '5A DIVISIONE',
    '3ª DIVISIONE', '4ª DIVISIONE', '5ª DIVISIONE',
    'DIVISION 2',
    # Giovanili / Riserve / Amatori / Femminile
    'WOMEN', 'FEMMINILE', 'FEMENINA', 'FRAUEN', 'DAMES', 'WOMAN', 'FEMININ', 'FEMENIL',
    'PRIMAVERA', 'UNDER', 'U17', 'U18', 'U19', 'U20', 'U21', 'U22', 'U23', 'SUB-20', 'SUB-19', 'SUB-17',
    'RESERVES', 'RISERVE', 'RESERVE', 'AMATEUR', 'AMATORI', 'AMATEURE', 'REGIONAL',
    'YOUTH', 'YOUTH LEAGUE', 'DEVELOPMENT', 'JUNIORES', 'BERRETTI', 'COLLEGE'
]

class FlashscorePalinsestoService:
    @staticmethod
    def check_and_rank_football_league(league_str, feed_idx):
        """
        Riconosce se un campionato e' di 1a o 2a divisione o coppa maggiore.
        Se e' una serie minore (3a/4a div) o giovanile/dilettanti/donne -> RITORNA NONE (SCARTATO!).
        Se e' valido -> RITORNA (tier, sub_order, feed_idx) con priorita' ASSOLUTA per l'Europa!
        """
        raw = (league_str or '').strip()
        up = raw.upper()

        for kw in STRICT_DISCARD_KEYWORDS:
            if kw in up:
                return None

        if ':' in raw:
            country_part, comp_part = raw.split(':', 1)
            country = country_part.strip().upper()
            comp = comp_part.strip().upper()
        else:
            country = 'EUROPE' if any(eu in up for eu in ['UEFA', 'CHAMPIONS LEAGUE', 'EUROPA LEAGUE', 'CONFERENCE']) else 'MONDO'
            comp = up

        is_europe = (country in EUROPE_COUNTRIES) or any(eu in country for eu in ['EUROPE', 'EUROPA', 'BOSNIA'])

        # UEFA & Coppe Europee Maggiori (Solo se UEFA / Europe)
        if is_europe or 'UEFA' in comp:
            if 'CHAMPIONS LEAGUE' in comp or 'UEFA CHAMPIONS' in comp:
                return (1, 1, feed_idx)
            if 'EUROPA LEAGUE' in comp or 'UEFA EUROPA' in comp:
                return (1, 2, feed_idx)
            if 'CONFERENCE LEAGUE' in comp:
                return (1, 3, feed_idx)
            if 'SUPER CUP' in comp:
                return (1, 4, feed_idx)
            if 'NATIONS LEAGUE' in comp:
                return (1, 5, feed_idx)
            if any(x in comp for x in ['EUROPEI', 'EURO QUAL', 'EURO CUP']):
                return (1, 6, feed_idx)

        # TOP 5 CAMPIONATI EUROPEI - 1A DIVISIONE
        if country in ['ITALIA', 'ITALY'] and ('SERIE A' in comp or comp == 'SERIE A'):
            return (1, 10, feed_idx)
        if country in ['INGHILTERRA', 'ENGLAND'] and ('PREMIER LEAGUE' in comp) and '2' not in comp:
            return (1, 11, feed_idx)
        if country in ['SPAGNA', 'SPAIN'] and ('LALIGA' in comp or 'PRIMERA DIVISION' in comp) and '2' not in comp and 'HYPERMOTION' not in comp:
            return (1, 12, feed_idx)
        if country in ['GERMANIA', 'GERMANY'] and ('BUNDESLIGA' in comp) and '2' not in comp:
            return (1, 13, feed_idx)
        if country in ['FRANCIA', 'FRANCE'] and ('LIGUE 1' in comp):
            return (1, 14, feed_idx)

        # TOP 5 CAMPIONATI EUROPEI - 2A DIVISIONE
        if country in ['ITALIA', 'ITALY'] and ('SERIE B' in comp or comp == 'SERIE B'):
            return (2, 1, feed_idx)
        if country in ['INGHILTERRA', 'ENGLAND'] and ('CHAMPIONSHIP' in comp):
            return (2, 2, feed_idx)
        if country in ['SPAGNA', 'SPAIN'] and any(x in comp for x in ['LALIGA2', 'HYPERMOTION', 'SEGUNDA DIVISION']):
            return (2, 3, feed_idx)
        if country in ['GERMANIA', 'GERMANY'] and ('2. BUNDESLIGA' in comp or '2.BUNDESLIGA' in comp):
            return (2, 4, feed_idx)
        if country in ['FRANCIA', 'FRANCE'] and ('LIGUE 2' in comp):
            return (2, 5, feed_idx)

        # Identificazione generica Prima e Seconda Divisione
        is_first_div = any(x in comp for x in [
            '1.', '1ST', 'TIER 1', 'FIRST', 'PREMIER', 'SUPER LIG', 'SUPER LEAGUE', 'SUPERLIGA', 
            'EREDIVISIE', 'PRIMEIRA LIGA', 'LIGA PORTUGAL', 'JUPILER', 'EKSTRAKLASA', 'ELITESERIEN', 
            'ALLSVENSKAN', 'A LYGA', 'HNL', 'LIGA 1', 'DIVISION 1', 'DIVISION OF HONOUR', 
            'TOP DIVISION', 'PROFESSIONAL LEAGUE', 'PRO LEAGUE', 'LIGA PROFESIONAL', 'SERIE A',
            'VEIKKAUSLIIGA', 'BESTA DEILD', 'MEISTRILIIGA', 'VIRSLIGA', 'SUPERIORE', 'NATIONALDIVISIOUN',
            'PREMIERSHIP', 'PREMIER DIVISION', 'PREMIJER LIGA', 'PRVA LIGA', 'PRIMERA A', 'LIGA MX'
        ])
        
        is_second_div = any(x in comp for x in [
            '2.', '2ND', 'TIER 2', 'SECOND', 'CHAMPIONSHIP', 'EERSTE DIVISIE', 'LIGA PORTUGAL 2',
            'CHALLENGER PRO', '1. LIG', '1. DIVISION', '1.DIVISION', 'SUPERETTAN', 'OBOS-LIGAEN',
            '2. LIGA', '2.LIGA', 'VTORA LIGA', 'SEGUNDA', 'SERIE B', 'PRIMERA NACIONAL', 'PRIMERA B',
            'USL CHAMPIONSHIP', 'J2 LEAGUE', 'K LEAGUE 2', 'LIGA 2', 'YKKONEN', 'YKKOSLIIGA',
            'KATEGORIA E PARE'
        ])

        is_cup = any(x in comp for x in ['COPPA', 'CUP', 'POKAL', 'BEKER', 'TAÇA', 'TACA', 'COUPE', 'COPA', 'SUPERCOPPA', 'SUPER CUP'])

        if is_europe:
            if is_first_div:
                return (3, feed_idx, feed_idx)
            if is_second_div:
                return (4, feed_idx, feed_idx)
            if is_cup:
                return (5, feed_idx, feed_idx)
            if any(w in comp for w in ['DIVISIO', 'LIGA', 'LEAGUE']):
                return (3, feed_idx, feed_idx)
            return None

        else:
            if any(x in comp for x in ['LIBERTADORES', 'SUDAMERICANA', 'CONCACAF', 'AFC CHAMPIONS', 'CAF CHAMPIONS', 'CHAMPIONS LEAGUE']):
                return (6, 1, feed_idx)
            if is_first_div or any(x in comp for x in ['MLS', 'MAJOR LEAGUE SOCCER', 'LIGA MX', 'SAUDI', 'J1 LEAGUE', 'K LEAGUE 1', 'LIGA PROFESIONAL']):
                return (6, 10, feed_idx)
            if is_second_div:
                return (7, feed_idx, feed_idx)
            if is_cup:
                return (8, feed_idx, feed_idx)
            return None

    @staticmethod
    def get_tennis_sort_key(league_name, feed_idx):
        lg = (league_name or '').upper().strip()
        if any(x in lg for x in ['AUSTRALIAN OPEN', 'ROLAND GARROS', 'OPEN DI FRANCIA', 'FRENCH OPEN', 'WIMBLEDON', 'US OPEN']):
            return (1, 1, feed_idx)
        if 'ATP' in lg and ('1000' in lg or 'MASTERS' in lg): return (1, 2, feed_idx)
        if 'WTA' in lg and ('1000' in lg): return (1, 3, feed_idx)
        if 'ATP - SINGLES' in lg or 'ATP - SINGOLARE' in lg or 'ATP' in lg: return (1, 4, feed_idx)
        if 'WTA - SINGLES' in lg or 'WTA - SINGOLARE' in lg or 'WTA' in lg: return (1, 5, feed_idx)
        if 'CHALLENGER' in lg: return (2, 1, feed_idx)
        if 'ITF' in lg: return (3, 1, feed_idx)
        return (4, feed_idx, feed_idx)

    @staticmethod
    def get_tournament_sort_key(sport_id, league_name, feed_idx):
        if sport_id == 1 or sport_id == "Calcio":
            return FlashscorePalinsestoService.check_and_rank_football_league(league_name, feed_idx)
        else:
            return FlashscorePalinsestoService.get_tennis_sort_key(league_name, feed_idx)

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
                curr_league_valid = True
                curr_sort_key = (5, 9999, 9999)
                match_counter = 0
                for row in r_sched.text.split("~"):
                    f = {}
                    for part in row.split(SEP1):
                        kv = part.split(SEP2)
                        if len(kv) == 2: f[kv[0]] = kv[1]
                    if "ZA" in f:
                        curr_league = f["ZA"]
                        curr_league_idx += 1
                        if sport_id == 1 or sport_id == "Calcio":
                            rank = FlashscorePalinsestoService.check_and_rank_football_league(curr_league, curr_league_idx)
                            if rank is None:
                                curr_league_valid = False
                            else:
                                curr_league_valid = True
                                curr_sort_key = rank
                        else:
                            up_lg = curr_league.upper()
                            if any(d_kw in up_lg for d_kw in ['DOUBLE', 'DOPPI', 'MIXED', 'MISTO']):
                                curr_league_valid = False
                            else:
                                curr_league_valid = True
                                curr_sort_key = FlashscorePalinsestoService.get_tennis_sort_key(curr_league, curr_league_idx)
                    elif "AA" in f:
                        if not curr_league_valid:
                            continue
                        home = f.get("AE", "Casa").strip()
                        away = f.get("AF", "Ospite").strip()

                        # FILTRO TENNIS: SCARTA I DOPPI (SOLO SINGOLARI)
                        if sport_id == 2 or sport_id == "Tennis":
                            if '/' in home or '/' in away:
                                continue

                        mid = f["AA"]
                        match_counter += 1
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

                # Ordina: campionati principali ed europei prima, mantenendo l'ordine dei match nel torneo
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
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favorites, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def load_saved_favorites():
    load_schedine_data()

def save_favorites():
    save_schedine_data()

def verifica_esito(m):
    """
    Verifica automatica dell'esito della partita in schedina.
    Ritorna: (stato, badge_text, css_class)
    Stati possibili: 'WIN' (Preso), 'LOST' (Non Preso), 'LIVE' (In corso), 'PENDING' (In attesa)
    """
    status_code = str(m.get("status_code", "1"))
    score_str = str(m.get("score", "- : -")).strip()
    chosen = str(m.get("custom_pick") or m.get("raw_pick") or "").upper().strip()
    sport = m.get("sport", "Calcio")

    if not chosen:
        return "PENDING", "⏳ IN ATTESA", "pending"

    if status_code in ("1", "") and (not score_str or score_str in ("- : -", "-:-", "-")):
        return "PENDING", "⏳ IN ATTESA", "pending"

    is_live = (status_code == "2")
    is_finished = (status_code == "3")

    home_score = None
    away_score = None
    parts = re.split(r'[:\-]', score_str)
    if len(parts) >= 2:
        try:
            h_clean = re.sub(r'[^\d]', '', parts[0].strip())
            a_clean = re.sub(r'[^\d]', '', parts[1].strip())
            if h_clean.isdigit() and a_clean.isdigit():
                home_score = int(h_clean)
                away_score = int(a_clean)
        except Exception:
            pass

    if home_score is None or away_score is None:
        if is_live:
            return "LIVE", f"🟡 LIVE ({score_str})", "live"
        return "PENDING", "⏳ IN ATTESA", "pending"

    if sport == "Calcio":
        if home_score > away_score: esito_reale = "1"
        elif home_score == away_score: esito_reale = "X"
        else: esito_reale = "2"

        preso = False
        if chosen == "1" and esito_reale == "1": preso = True
        elif chosen == "X" and esito_reale == "X": preso = True
        elif chosen == "2" and esito_reale == "2": preso = True
        elif chosen == "1X" and esito_reale in ("1", "X"): preso = True
        elif chosen == "X2" and esito_reale in ("X", "2"): preso = True
        elif chosen == "12" and esito_reale in ("1", "2"): preso = True

        if is_finished:
            if preso: return "WIN", f"✅ PRESO ({home_score}-{away_score})", "win"
            else: return "LOST", f"❌ NON PRESO ({home_score}-{away_score})", "lost"
        else:
            tag = "🟡 VANTAGGIO" if preso else "🟡 SVANTAGGIO"
            return "LIVE", f"{tag} ({home_score}-{away_score})", "live"
    else:
        # Tennis
        tot_set = home_score + away_score
        if "OVER" in chosen or "SET" in chosen:
            if tot_set >= 3:
                return "WIN", f"✅ PRESO 3° SET ({home_score}-{away_score})", "win"
            elif is_finished:
                return "LOST", f"❌ FINITO IN 2 SET ({home_score}-{away_score})", "lost"
            else:
                return "LIVE", f"🟡 SET: {home_score}-{away_score}", "live"
        else:
            vince_p1 = (home_score > away_score)
            vince_p2 = (away_score > home_score)
            is_g1 = ("1" in chosen or "G1" in chosen)
            is_g2 = ("2" in chosen or "G2" in chosen)
            preso = (is_g1 and vince_p1) or (is_g2 and vince_p2)
            if is_finished:
                if preso: return "WIN", f"✅ PRESO ({home_score}-{away_score})", "win"
                else: return "LOST", f"❌ NON PRESO ({home_score}-{away_score})", "lost"
            else:
                tag = "🟡 VANTAGGIO" if preso else "🟡 SVANTAGGIO"
                return "LIVE", f"{tag} ({home_score}-{away_score})", "live"

def calcola_quota_effettiva(m):
    q = m.get("odds")
    if not q:
        return 1.00
    chosen = str(m.get("custom_pick") or m.get("raw_pick") or "1").strip().upper()
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
            if "OVER" in chosen or "SET" in chosen:
                return float(m.get("over_set_quota", 2.05))
            if "2" in chosen or "G2" in chosen: return q2
            return q1
    except Exception:
        pass
    return 1.00

def elabora_algoritmo(m):
    q = m.get("odds")
    home = m.get("home", "")
    away = m.get("away", "")
    if not q:
        m["pick_str"] = "Quote N.D."
        m["pick_title"] = "In attesa delle quote ufficiali"
        m["summary_lines"] = ["• Quote al momento non disponibili sul feed"]
        m["full_report"] = ["Quote non disponibili."]
        m["raw_pick"] = None
        m["effective_quota"] = 1.00
        m["esito_stato"] = "PENDING"
        m["esito_testo"] = "⏳ IN ATTESA"
        m["esito_css"] = "pending"
        return
    sp = m.get("sport", "Calcio")
    mid = m.get("id", "")
    if sp == "Calcio" and len(q) == 3:
        res = analizza_calcio(fb_db, q[0], q[1], q[2], home=home, away=away, mid=mid)
        if res:
            livello, verdetto, summ, full = report_calcio(res, q)
            icon = "🎯" if livello == "ok" else ("⚠️" if livello == "warn" else "🚨")
            m["livello"] = livello
            m["raw_pick"] = res["pick"]
            m["pick_str"] = f"{icon} {res['pick']} ({res['pick_pct']:.1f}%)"
            m["pick_title"] = verdetto
            m["summary_lines"] = summ
            m["full_report"] = full
            m["forma_summary"] = res.get("forma_summary", "")
    elif sp == "Tennis" and len(q) >= 2:
        res = analizza_tennis(tn_db, q[0], q[1], p1=home, p2=away, mid=mid)
        if res:
            livello, verdetto, summ, full = report_tennis(res, q)
            icon = "🎯" if livello == "ok" else ("⚠️" if livello == "warn" else "🚨")
            m["livello"] = livello
            m["raw_pick"] = res["pick"]
            m["pick_str"] = f"{icon} {res['pick']} ({res['pick_pct']:.1f}%)"
            m["pick_title"] = verdetto
            m["summary_lines"] = summ
            m["full_report"] = full
            m["over_set_quota"] = res.get("over_set_quota", 2.05)
            m["forma_summary"] = res.get("forma_summary", "")

    m["effective_quota"] = calcola_quota_effettiva(m)
    stato, testo, css_cls = verifica_esito(m)
    m["esito_stato"] = stato
    m["esito_testo"] = testo
    m["esito_css"] = css_cls

def get_palinsesto(force=False):
    global cached_palinsesto, last_palinsesto_time
    now = time.time()
    if not force and cached_palinsesto and (now - last_palinsesto_time < 90):
        return cached_palinsesto
    try:
        fb = FlashscorePalinsestoService.fetch_all_today(sport_id=1, day_offset=0)
        tn = FlashscorePalinsestoService.fetch_all_today(sport_id=2, day_offset=0)

        def tier(m):
            sk = m.get("league_sort_key", (5, 9999, 9999))
            return sk[0] if isinstance(sk, (tuple, list)) else 5

        # Ordinamento Palinsesto:
        # 1. Calcio Europeo (Tiers 1, 2, 3, 4, 5) - SEMPRE E ASSOLUTAMENTE IN CIMA ALLA LISTA!
        #    Tier 1: Top 5 Europei 1a Div (Serie A, Premier, LaLiga, Bundesliga, Ligue 1) + Coppe UEFA
        #    Tier 2: Top 5 Europei 2a Div (Serie B, Championship, LaLiga2, 2. Bundesliga, Ligue 2)
        #    Tier 3: Altri campionati Europei 1a Div (Eredivisie, Liga Portugal, Super Lig, ecc.)
        #    Tier 4: Altri campionati Europei 2a Div (Eerste Divisie, ecc.)
        #    Tier 5: Coppe Nazionali Europee
        # 2. Tennis Singolari (Tiers 1, 2, 3, 4)
        # 3. Calcio Resto del Mondo (Tiers 6, 7, 8) - IN SEGUITO
        fb_europe = [m for m in fb if tier(m) <= 5]
        fb_world = [m for m in fb if tier(m) > 5]

        all_sorted = fb_europe + tn + fb_world

        # Prefetch parallelo velocissimo di tutti i match con quote (H2H Flashscore Live)
        with_odds = [m for m in all_sorted if m.get("odds") and m.get("id")]
        from concurrent.futures import ThreadPoolExecutor
        def _pre_fetch(m):
            sp_id = 1 if m.get("sport") == "Calcio" else 2
            get_forma_live(sp_id, m["id"], m.get("home", ""), m.get("away", ""))
        with ThreadPoolExecutor(max_workers=20) as ex:
            list(ex.map(_pre_fetch, with_odds))

        # Pre-calcola algoritmo sui match con quote per visione immediata da smartphone!
        for m in with_odds:
            elabora_algoritmo(m)

        cached_palinsesto = all_sorted
        last_palinsesto_time = now
    except Exception:
        pass
    return cached_palinsesto
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
    load_schedine_data()
    for m in favorites.values():
        if not m.get("pick_str"):
            elabora_algoritmo(m)
    return render_mobile_html()

@app.route("/api/data")
def api_data():
    load_schedine_data()
    fav_list = list(favorites.values())
    tot_quota = 1.0
    has_lost = False
    all_win = len(fav_list) > 0
    has_live = False

    for m in fav_list:
        if not m.get("pick_str"):
            elabora_algoritmo(m)
        tot_quota *= m.get("effective_quota", 1.0)
        # Aggiorna verifica esito live
        stato, testo, css_cls = verifica_esito(m)
        m["esito_stato"] = stato
        m["esito_testo"] = testo
        m["esito_css"] = css_cls
        if stato == "LOST":
            has_lost = True
            all_win = False
        elif stato == "WIN":
            pass
        elif stato == "LIVE":
            has_live = True
            all_win = False
        else:
            all_win = False

    if len(fav_list) == 0:
        esito_complessivo = "EMPTY"
        esito_label = "📋 Schedina vuota"
    elif has_lost:
        esito_complessivo = "LOST"
        esito_label = "❌ SCHEDINA NON RIUSCITA"
    elif all_win:
        esito_complessivo = "WIN"
        esito_label = "🎉 SCHEDINA VINCENTE!"
    elif has_live:
        esito_complessivo = "LIVE"
        esito_label = "🟡 INCONTRI IN CORSO (LIVE)"
    else:
        esito_complessivo = "PENDING"
        esito_label = "⏳ IN ATTESA DEGLI INCONTRI"

    return jsonify({
        "count": len(fav_list),
        "favorites": fav_list,
        "active_schedina": active_schedina_name,
        "schedine_list": list(schedine_store.keys()),
        "total_quota": round(tot_quota, 2),
        "esito_complessivo": esito_complessivo,
        "esito_label": esito_label,
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

@app.route("/api/schedine/new", methods=["POST"])
def api_schedine_new():
    global active_schedina_name, schedine_store, favorites
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Nome schedina obbligatorio"}), 400
    if active_schedina_name:
        schedine_store[active_schedina_name] = dict(favorites)
    active_schedina_name = name
    schedine_store[name] = {}
    favorites = schedine_store[name]
    save_schedine_data()
    return jsonify({
        "status": "ok",
        "active": active_schedina_name,
        "schedine_list": list(schedine_store.keys()),
        "message": f"Creata schedina '{name}'"
    })

@app.route("/api/schedine/sync", methods=["POST"])
def api_schedine_sync():
    global active_schedina_name, schedine_store, favorites
    data = request.get_json(silent=True) or {}
    client_schedine = data.get("schedine")
    client_active = data.get("active")
    if client_schedine and isinstance(client_schedine, dict):
        for s_name, s_matches in client_schedine.items():
            if s_name not in schedine_store:
                schedine_store[s_name] = s_matches
            elif not schedine_store[s_name] and s_matches:
                schedine_store[s_name] = s_matches
    if client_active and client_active in schedine_store:
        active_schedina_name = client_active
    favorites = schedine_store.get(active_schedina_name, {})
    save_schedine_data()
    return jsonify({
        "status": "ok",
        "active": active_schedina_name,
        "schedine": schedine_store,
        "schedine_list": list(schedine_store.keys()),
        "count": len(favorites)
    })

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
    return jsonify({"status": "ok", "active": name, "schedine_list": list(schedine_store.keys())})

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
        "active": name,
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
        schedine_store["Schedina Principale"] = {}
        active_schedina_name = "Schedina Principale"
        favorites = schedine_store[active_schedina_name]
        save_schedine_data()
        return jsonify({"status": "ok", "active": active_schedina_name, "schedine_list": list(schedine_store.keys())})
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
    has_lost = False
    all_win = len(favorites) > 0
    has_live = False

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
        current_pick = str(m.get("custom_pick") or m.get("raw_pick") or "").upper().strip()

        # Verifica esito match
        stato, testo_esito, css_esito = verifica_esito(m)
        m["esito_stato"] = stato
        m["esito_testo"] = testo_esito
        m["esito_css"] = css_esito
        if stato == "LOST":
            has_lost = True
            all_win = False
        elif stato == "WIN":
            pass
        elif stato == "LIVE":
            has_live = True
            all_win = False
        else:
            all_win = False

        status_badge_html = f'<span class="status-pill {css_esito}">{testo_esito}</span>'

        # Bottoni selezione esito schedina
        if sport == "Calcio":
            pick_pills = f'''
            <div class="pick-selector">
                <span class="pick-sel-lbl">Esito Scelto (@{eq:.2f}):</span>
                <button class="pick-btn {'active' if current_pick == '1' else ''}" onclick="cambiaEsito('{mid}', '1')">1</button>
                <button class="pick-btn {'active' if current_pick == 'X' else ''}" onclick="cambiaEsito('{mid}', 'X')">X</button>
                <button class="pick-btn {'active' if current_pick == '2' else ''}" onclick="cambiaEsito('{mid}', '2')">2</button>
                <button class="pick-btn {'active' if current_pick == '1X' else ''}" onclick="cambiaEsito('{mid}', '1X')">1X</button>
                <button class="pick-btn {'active' if current_pick == 'X2' else ''}" onclick="cambiaEsito('{mid}', 'X2')">X2</button>
                <button class="pick-btn {'active' if current_pick == '12' else ''}" onclick="cambiaEsito('{mid}', '12')">12</button>
            </div>
            '''
        else:
            over_q = m.get("over_set_quota", 2.05)
            pick_pills = f'''
            <div class="pick-selector">
                <span class="pick-sel-lbl">Esito Scelto (@{eq:.2f}):</span>
                <button class="pick-btn {'active' if ('1' in current_pick or 'G1' in current_pick) and 'OVER' not in current_pick else ''}" onclick="cambiaEsito('{mid}', 'G1')">G1</button>
                <button class="pick-btn {'active' if ('2' in current_pick or 'G2' in current_pick) and 'OVER' not in current_pick else ''}" onclick="cambiaEsito('{mid}', 'G2')">G2</button>
                <button class="pick-btn {'active' if 'OVER' in current_pick or 'SET' in current_pick else ''}" onclick="cambiaEsito('{mid}', 'OVER 2.5 SET')">OVER 2.5 SET (@{over_q:.2f})</button>
            </div>
            '''

        card_html = f'''
        <div class="card card-{css_esito}" id="card_{mid}">
            <div class="card-top">
                <span class="league-badge">{sp_icon} {league}</span>
                <div style="display:flex; align-items:center; gap:6px;">
                    {status_badge_html}
                    <span class="match-time">⏱ {time_str}</span>
                </div>
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

    cards_content = "\n".join(fav_cards_html) if fav_cards_html else '<div class="empty-slip-box"><span>📋 Schedina Attualmente Vuota</span><p>Tocca <b>⚽ Palinsesto Oggi</b> per esplorare i pronostici elaborati in tempo reale e aggiungere partite con 1 tap!</p></div>'
    n_fav = len(favorites)
    schedine_options = "".join(f'<option value="{name}" {"selected" if name == active_schedina_name else ""}>{name} ({len(schedine_store.get(name, {}))})</option>' for name in schedine_store)

    # Banner verifica complessiva
    if n_fav == 0:
        outcome_banner_html = ''
    elif has_lost:
        outcome_banner_html = '<div class="outcome-banner lost">❌ SCHEDINA NON RIUSCITA • Uno o più pronostici non centrati</div>'
    elif all_win:
        outcome_banner_html = '<div class="outcome-banner win">🎉 SCHEDINA VINCENTE! • Tutti gli esiti sono stati centrati!</div>'
    elif has_live:
        outcome_banner_html = '<div class="outcome-banner live">🟡 VERIFICA IN CORSO • Partite in svolgimento LIVE</div>'
    else:
        outcome_banner_html = '<div class="outcome-banner pending">⏳ VERIFICA ATTIVA • In attesa della conclusione degli eventi</div>'

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

        /* Banner Verifica Esito Schedina */
        .outcome-banner {{
            padding: 10px 14px;
            border-radius: 10px;
            font-size: 12px;
            font-weight: 800;
            text-align: center;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }}
        .outcome-banner.win {{ background: rgba(34, 197, 94, 0.2); border: 1px solid var(--green); color: #4ade80; }}
        .outcome-banner.lost {{ background: rgba(239, 68, 68, 0.2); border: 1px solid var(--red); color: #f87171; }}
        .outcome-banner.live {{ background: rgba(245, 158, 11, 0.2); border: 1px solid var(--amber); color: #fbbf24; }}
        .outcome-banner.pending {{ background: rgba(56, 189, 248, 0.15); border: 1px solid var(--acc); color: #7dd3fc; }}

        /* Status Badge Singola Partita */
        .status-pill {{
            font-size: 10px;
            font-weight: 800;
            padding: 2px 7px;
            border-radius: 12px;
            display: inline-block;
            white-space: nowrap;
        }}
        .status-pill.win {{ background: rgba(34, 197, 94, 0.25); color: #4ade80; border: 1px solid var(--green); }}
        .status-pill.lost {{ background: rgba(239, 68, 68, 0.25); color: #f87171; border: 1px solid var(--red); }}
        .status-pill.live {{ background: rgba(245, 158, 11, 0.25); color: #fbbf24; border: 1px solid var(--amber); }}
        .status-pill.pending {{ background: rgba(148, 163, 184, 0.2); color: var(--dim); border: 1px solid var(--border); }}

        /* Card color accent per esiti */
        .card.card-win {{ border-left: 3px solid var(--green); }}
        .card.card-lost {{ border-left: 3px solid var(--red); }}
        .card.card-live {{ border-left: 3px solid var(--amber); }}

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

        /* Palinsesto Mobile */
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
            border-radius: 12px;
            padding: 10px;
            margin-bottom: 8px;
            box-shadow: 0 3px 8px rgba(0,0,0,0.2);
        }}
        .pal-top-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }}
        .pal-teams {{ font-size: 13px; font-weight: 800; color: #fff; margin: 3px 0; }}
        .pal-odds-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-sub);
            padding: 4px 8px;
            border-radius: 6px;
            margin: 6px 0;
        }}
        .pal-odds-val {{ font-size: 11px; color: var(--acc); font-weight: 700; }}
        /* Box pronostico immediato nel palinsesto */
        .pal-pred-box {{
            background: rgba(56, 189, 248, 0.1);
            border: 1px solid rgba(56, 189, 248, 0.3);
            border-radius: 8px;
            padding: 6px 8px;
            margin-top: 6px;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .pal-pred-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .pal-pred-badge {{
            font-size: 10px;
            font-weight: 800;
            background: var(--acc);
            color: #0b0f19;
            padding: 2px 6px;
            border-radius: 4px;
        }}
        .pal-pred-title {{
            font-size: 10px;
            font-weight: 700;
            color: #e2e8f0;
        }}
        .pal-forma-txt {{
            font-size: 9px;
            color: #94a3b8;
            margin-top: 2px;
        }}
        .pal-btn-add {{
            background: rgba(34, 197, 94, 0.2);
            border: 1px solid var(--green);
            color: var(--green);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 11px;
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
        <div class="live-tag"><span class="live-dot"></span> LIVE VERIFICA 24/7</div>
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
        <!-- BANNER VERIFICA ESITO COMPLESSIVO -->
        <div id="outcomeBannerContainer">
            {outcome_banner_html}
        </div>

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
            <button class="pill-btn" id="pillFb" onclick="setSportFilter('Calcio')">⚽ Calcio (1ª e 2ª Div)</button>
            <button class="pill-btn" id="pillTn" onclick="setSportFilter('Tennis')">🎾 Tennis (Solo Singoli)</button>
            <button class="pill-btn" id="pillTop" onclick="setSportFilter('TOP')">🔥 Top Europei</button>
        </div>
        <input type="text" id="palSearchInput" class="pal-search" placeholder="🔍 Cerca squadra, giocatore o campionato..." oninput="applicaFiltriPalinsesto()">
        <div style="font-size: 11px; color: var(--dim); margin-bottom: 8px;">
            ⚡ <b>Pronostico Algoritmo visibile subito</b> per ogni partita: tocca <b>⭐ Aggiungi</b> per inserire direttamente in schedina!
        </div>
        <div id="palinsestoContainer" class="cards-list">
            <div style="text-align: center; padding: 20px; color: var(--dim);">Caricamento palinsesto con pronostici in corso...</div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let palinsestoData = [];
        let favIds = {list(favorites.keys())};
        let currentSportFilter = 'ALL';
        let currentTotalQuota = {tot_quota};
        const STORAGE_KEY_SCHEDINE = 'qb_schedine_pro_v2';
        const STORAGE_KEY_ACTIVE = 'qb_active_schedina_v2';

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
                        ${{isTop ? '<span style="font-size:9px; background:#f59e0b; color:#000; padding:2px 6px; border-radius:4px; font-weight:800;">TOP EUROPEI</span>' : ''}}
                    </div>`;
                }}
                const isFav = favIds.includes(m.id);
                const hasPred = !!m.pick_str;
                html += `
                <div class="pal-card">
                    <div class="pal-top-row">
                        <span style="font-size:10px; color:var(--dim);">${{m.time}} • ${{m.status_code === '2' ? '<span style=\"color:var(--red); font-weight:800;\">LIVE ' + m.score + '</span>' : 'Programmata'}}</span>
                        <button class="pal-btn-add ${{isFav ? 'added' : ''}}" id="btn_pal_${{m.id}}" onclick="aggiungiDaPalinsesto('${{m.id}}')">
                            ${{isFav ? '✓ In Schedina' : '⭐ Aggiungi'}}
                        </button>
                    </div>
                    <div class="pal-teams">${{m.home}} vs ${{m.away}}</div>
                    <div class="pal-odds-bar">
                        <span style="font-size:10px; color:var(--dim);">Quote Ufficiali:</span>
                        <span class="pal-odds-val">${{m.odds_str || 'N.D.'}}</span>
                    </div>
                    ${{hasPred ? `
                    <div class="pal-pred-box">
                        <div class="pal-pred-head">
                            <span class="pal-pred-badge">${{m.pick_str}}</span>
                            <span class="pal-pred-title">${{m.pick_title || ''}}</span>
                        </div>
                        ${{m.forma_summary ? `<div class="pal-forma-txt">📈 Forma: ${{m.forma_summary}}</div>` : ''}}
                    </div>
                    ` : ''}}
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
                    salvaBackupLocale();
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
                    salvaBackupLocale();
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
                    document.getElementById('cardsContainer').innerHTML = '<div class="empty-slip-box"><span>📋 Schedina Attualmente Vuota</span><p>Tocca <b>⚽ Palinsesto Oggi</b> per esplorare i pronostici elaborati in tempo reale e aggiungere partite con 1 tap!</p></div>';
                    document.querySelectorAll('.pal-btn-add').forEach(b => {{
                        b.classList.remove('added');
                        b.innerText = '⭐ Aggiungi';
                    }});
                    const outcomeBanner = document.getElementById('outcomeBannerContainer');
                    if (outcomeBanner) outcomeBanner.innerHTML = '';
                    currentTotalQuota = 1.0;
                    document.getElementById('statCount').innerText = '0';
                    document.getElementById('tabCount').innerText = '0';
                    document.getElementById('statQuota').innerText = '@1.00';
                    aggiornaCalcoloVincita();
                    salvaBackupLocale();
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
                    salvaBackupLocale();
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
                    salvaBackupLocale();
                    setTimeout(() => location.reload(), 500);
                }}
            }} catch (err) {{
                showToast('Errore aggiornamento quote');
            }}
        }}

        async function ricalcolaTutto() {{
            showToast('Ricalcolo quote e verifica esiti in corso...');
            try {{
                const res = await fetch('/api/recalculate?_t=' + Date.now(), {{ method: 'POST' }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    showToast('✓ Ricalcolo ed esiti aggiornati!');
                    salvaBackupLocale();
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
                const res = await fetch('/api/schedine/new?_t=' + Date.now(), {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: nome.trim() }})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    localStorage.setItem(STORAGE_KEY_ACTIVE, nome.trim());
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
                    localStorage.setItem(STORAGE_KEY_ACTIVE, nome.trim());
                    salvaBackupLocale();
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
                    localStorage.setItem(STORAGE_KEY_ACTIVE, nome);
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
                    try {{
                        let localSchedine = JSON.parse(localStorage.getItem(STORAGE_KEY_SCHEDINE) || '{{}}');
                        delete localSchedine[nome];
                        localStorage.setItem(STORAGE_KEY_SCHEDINE, JSON.stringify(localSchedine));
                        localStorage.setItem(STORAGE_KEY_ACTIVE, data.active);
                    }} catch (e) {{}}
                    showToast('Schedina eliminata definitivamente');
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (err) {{
                showToast('Errore eliminazione schedina');
            }}
        }}

        async function salvaBackupLocale() {{
            try {{
                const res = await fetch('/api/data?_t=' + Date.now());
                const data = await res.json();
                let localSchedine = JSON.parse(localStorage.getItem(STORAGE_KEY_SCHEDINE) || '{{}}');
                localSchedine[data.active_schedina] = {{}};
                (data.favorites || []).forEach(m => {{
                    localSchedine[data.active_schedina][m.id] = m;
                }});
                localStorage.setItem(STORAGE_KEY_SCHEDINE, JSON.stringify(localSchedine));
                localStorage.setItem(STORAGE_KEY_ACTIVE, data.active_schedina);
            }} catch (e) {{}}
        }}

        async function sincronizzaConLocalStorage() {{
            try {{
                const localStr = localStorage.getItem(STORAGE_KEY_SCHEDINE);
                const localActive = localStorage.getItem(STORAGE_KEY_ACTIVE);
                if (localStr) {{
                    const localData = JSON.parse(localStr);
                    await fetch('/api/schedine/sync?_t=' + Date.now(), {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ schedine: localData, active: localActive }})
                    }});
                }}
            }} catch (e) {{}}
        }}

        async function aggiornaDatiSchedina() {{
            try {{
                const res = await fetch('/api/data?_t=' + Date.now());
                const data = await res.json();
                favIds = (data.favorites || []).map(m => m.id);
                document.getElementById('statCount').innerText = data.count;
                document.getElementById('tabCount').innerText = data.count;
                document.getElementById('statQuota').innerText = '@' + (data.total_quota || 1.0).toFixed(2);
                currentTotalQuota = data.total_quota || 1.0;
                aggiornaCalcoloVincita();

                // Aggiorna banner esito
                const bannerContainer = document.getElementById('outcomeBannerContainer');
                if (bannerContainer) {{
                    if (data.count === 0) bannerContainer.innerHTML = '';
                    else if (data.esito_complessivo === 'WIN') {{
                        bannerContainer.innerHTML = '<div class="outcome-banner win">🎉 SCHEDINA VINCENTE! • Tutti gli esiti sono stati centrati!</div>';
                    }} else if (data.esito_complessivo === 'LOST') {{
                        bannerContainer.innerHTML = '<div class="outcome-banner lost">❌ SCHEDINA NON RIUSCITA • Uno o più pronostici non centrati</div>';
                    }} else if (data.esito_complessivo === 'LIVE') {{
                        bannerContainer.innerHTML = '<div class="outcome-banner live">🟡 VERIFICA IN CORSO • Partite in svolgimento LIVE</div>';
                    }} else {{
                        bannerContainer.innerHTML = '<div class="outcome-banner pending">⏳ VERIFICA ATTIVA • In attesa della conclusione degli eventi</div>';
                    }}
                }}
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
                    salvaBackupLocale();
                    setTimeout(() => location.reload(), 600);
                }} else {{
                    showToast('Errore: ' + (data.message || 'Non trovata'));
                }}
            }} catch (err) {{
                showToast('Errore di connessione');
            }}
        }}

        // Sincronizza local storage all'avvio in background
        window.addEventListener('DOMContentLoaded', () => {{
            sincronizzaConLocalStorage();
        }});
    </script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
