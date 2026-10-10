#!/usr/bin/env python3
"""
Bouncy Energy – marknadsdata från ENTSO-E.

Hämtar day-ahead-priser och faktisk produktion (vind, sol) per elområde,
sparar en lokal cache (data/raw/*.csv.gz) och räknar ut:

  * capture price / capture rate per månad och teknik (vind, sol)
  * rullande 12 månader (produktionsviktat)
  * dygnsspread per månad (medel och max)
  * medelpris per timme på dygnet och månad (duck curve, dag- och nattpriser)
  * fiktiv solpark i Halmstad (PVGIS) med och utan batteri: capture price och capture rate
  * arbitragedjup: hur intäkt per MW och dygnsspread påverkas när fler batterier kommer in (modell)
  * prispåverkan: hur mycket priset rör sig per GW restlast (skattas ur last, vind, sol och pris)
  * timmar med negativt pris
  * timvyer för de senaste 14 dagarna

Resultatet skrivs som JSON till docs/data/ och läses av docs/index.html.

Användning:
    export ENTSOE_API_KEY="din-token"
    python update_data.py                 # hämta nytt + räkna om
    python update_data.py --no-fetch      # räkna om från cachen
    python update_data.py --demo          # syntetisk data, ingen token behövs
    python update_data.py --zones SE3,SE4 # begränsa till vissa områden
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "docs" / "data"

TZ = "Europe/Stockholm"          # dygn och månader räknas i svensk tid
START_DEFAULT = "2024-01-01"     # så långt bakåt cachen byggs vid första körningen
MIN_COVERAGE = 0.80              # minsta andel timmar med produktionsdata för att visa en månad
MIN_DAY_HOURS = 22               # minsta antal timmar för att ett dygn ska räknas i spreaden
RECENT_DAYS = 14

ZONES = {
    "SE1": {"code": "SE_1", "name": "SE1 Luleå"},
    "SE2": {"code": "SE_2", "name": "SE2 Sundsvall"},
    "SE3": {"code": "SE_3", "name": "SE3 Stockholm"},
    "SE4": {"code": "SE_4", "name": "SE4 Malmö"},
    "DE": {"code": "DE_LU", "name": "Tyskland (DE-LU)"},
    "DK1": {"code": "DK_1", "name": "Danmark väst (DK1)"},
    "DK2": {"code": "DK_2", "name": "Danmark öst (DK2)"},
    "FI": {"code": "FI", "name": "Finland"},
    # Kontrollområden: hämtas bara för vind, sol och förbrukning och används när prispåverkan skattas
    "NO1": {"code": "NO_1", "name": "NO1 Oslo", "ctrl": True},
    "NO2": {"code": "NO_2", "name": "NO2 Kristiansand", "ctrl": True},
    "NO3": {"code": "NO_3", "name": "NO3 Trondheim", "ctrl": True},
    "NO4": {"code": "NO_4", "name": "NO4 Tromsø", "ctrl": True},
    "NO5": {"code": "NO_5", "name": "NO5 Bergen", "ctrl": True},
}

log = logging.getLogger("bouncy")


# --------------------------------------------------------------------------- #
# Hämtning från ENTSO-E
# --------------------------------------------------------------------------- #
def month_chunks(start: pd.Timestamp, end: pd.Timestamp, step: int = 1):
    """Delar upp [start, end) i block om `step` kalendermånader (ENTSO-E tillåter max ett år per anrop)."""
    cur = start
    while cur < end:
        nxt = min(cur + pd.offsets.MonthBegin(step), end)
        yield cur, nxt
        cur = nxt


def pick_generation(df: pd.DataFrame) -> pd.DataFrame:
    """Plockar ut vind (land + hav) och sol ur ett A75-svar från entsoe-py."""
    if isinstance(df, pd.Series):
        df = df.to_frame()
    if isinstance(df.columns, pd.MultiIndex):
        # Kolumner som ("Wind Onshore", "Actual Aggregated") / (..., "Actual Consumption")
        keep = df.columns.get_level_values(1) == "Actual Aggregated"
        df = df.loc[:, keep]
        df.columns = df.columns.get_level_values(0)
    wind_cols = [c for c in df.columns if str(c).startswith("Wind")]
    solar_cols = [c for c in df.columns if str(c).startswith("Solar")]
    out = pd.DataFrame(index=df.index)
    out["wind"] = df[wind_cols].sum(axis=1, min_count=1) if wind_cols else np.nan
    out["solar"] = df[solar_cols].sum(axis=1, min_count=1) if solar_cols else np.nan
    return out


def fetch_zone(client, no_match_error, code: str, start: pd.Timestamp, end: pd.Timestamp,
               gen_only: bool = False) -> pd.DataFrame | None:
    """Hämtar pris + produktion för ett område och returnerar timdata (UTC)."""

    def attempt(fn):
        for i in range(4):
            try:
                return fn()
            except no_match_error:
                return None
            except Exception as exc:  # nätverk, rate limit, 5xx ...
                wait = 5 * (i + 1)
                log.warning("  fel (%s) – försöker igen om %ss", type(exc).__name__, wait)
                time.sleep(wait)
        return None

    parts = []
    for s, e in month_chunks(start, end):
        price = None
        if not gen_only:
            price = attempt(lambda: client.query_day_ahead_prices(code, start=s, end=e))
            time.sleep(0.3)
        gen = attempt(lambda: client.query_generation(code, start=s, end=e))
        time.sleep(0.3)

        frame = pd.DataFrame()
        if price is not None and len(price):
            frame["price"] = price.resample("h").mean()
        if gen is not None and len(gen):
            g = pick_generation(gen).resample("h").mean()
            frame = g if frame.empty else frame.join(g, how="outer")
        if not frame.empty:
            parts.append(frame)
        log.info("  %s → %s: %s rader", s.date(), e.date(), 0 if frame.empty else len(frame))

    if not parts:
        return None
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index = df.index.tz_convert("UTC")
    for col in ("price", "wind", "solar"):
        if col not in df:
            df[col] = np.nan
    return df[["price", "wind", "solar"]]


def fetch_load(client, no_match_error, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series | None:
    """Faktisk förbrukning (MW) per timme, UTC."""
    parts = []
    for s, e in month_chunks(start, end, 2):
        df = None
        for i in range(4):
            try:
                df = client.query_load(code, start=s, end=e)
                break
            except no_match_error:
                break
            except Exception as exc:
                wait = 5 * (i + 1)
                log.warning("  lastfel (%s) – försöker igen om %ss", type(exc).__name__, wait)
                time.sleep(wait)
        time.sleep(0.3)
        if df is None or len(df) == 0:
            continue
        if isinstance(df, pd.DataFrame):
            col = next((c for c in df.columns if "Actual" in str(c)), df.columns[0])
            df = df[col]
        parts.append(df.resample("h").mean())
    if not parts:
        return None
    out = pd.concat(parts).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out.index = out.index.tz_convert("UTC")
    return out.rename("load")


def add_load(client, no_match_error, code: str, df: pd.DataFrame, start_default: str) -> pd.DataFrame:
    """Lägger till kolumnen `load` i cachen. Hämtar hela historiken första gången, därefter bara nya dagar."""
    hist_start = max(pd.Timestamp(start_default, tz=TZ),                               # skattningen behöver bara ca 2 år
                     pd.Timestamp.now(tz=TZ).normalize() - pd.DateOffset(months=SLOPE_MONTHS + 1))
    have = df["load"].dropna() if "load" in df else pd.Series(dtype=float)
    if have.empty or have.index.min() > hist_start.tz_convert("UTC") + pd.Timedelta(days=20):
        start = hist_start
    else:
        start = (have.index.max() - pd.Timedelta(days=7)).tz_convert(TZ).normalize()
    end = pd.Timestamp.now(tz=TZ).normalize() + pd.Timedelta(days=1)
    log.info("  last: hämtar %s → %s", start.date(), end.date())
    new = fetch_load(client, no_match_error, code, start, end)
    if new is None:
        return df
    out = df.copy()
    out["load"] = new.combine_first(out["load"]) if "load" in out else new
    return out


def load_cache(key: str) -> pd.DataFrame | None:
    path = RAW_DIR / f"{key}.csv.gz"
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def save_cache(key: str, df: pd.DataFrame) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.round(3).to_csv(RAW_DIR / f"{key}.csv.gz", compression="gzip")


def update_cache(client, no_match_error, key: str, code: str, start_default: str,
                 gen_only: bool = False) -> pd.DataFrame | None:
    cached = load_cache(key)
    now_local = pd.Timestamp.now(tz=TZ)
    end = now_local.normalize() + pd.Timedelta(days=2)   # day-ahead för i morgon finns efter ca 13:00
    if cached is not None and len(cached):
        # Hämta om de senaste 7 dagarna – produktionsdata revideras i efterhand
        start = (cached.index.max() - pd.Timedelta(days=7)).tz_convert(TZ).normalize()
    else:
        start = pd.Timestamp(start_default, tz=TZ)
    log.info("%s: hämtar %s → %s", key, start.date(), end.date())
    new = fetch_zone(client, no_match_error, code, start, end, gen_only)
    if new is None:
        log.warning("%s: ingen ny data", key)
        merged = cached
    else:
        merged = new if cached is None else new.combine_first(cached).sort_index()
    if merged is not None:
        try:
            merged = add_load(client, no_match_error, code, merged, start_default)
        except Exception as exc:                       # saknad last ska inte stoppa resten
            log.warning("%s: last kunde inte hämtas (%s)", key, exc)
        save_cache(key, merged)
    return merged


# --------------------------------------------------------------------------- #
# Beräkningar
# --------------------------------------------------------------------------- #
def compute_zone(df: pd.DataFrame, include_partial: bool = False) -> tuple[dict, dict] | None:
    loc = df.copy()
    loc.index = loc.index.tz_convert(TZ).tz_localize(None)   # lokal väggtid
    loc = loc[loc["price"].notna()]
    if loc.empty:
        return None

    cur_month = pd.Timestamp.now(tz=TZ).tz_localize(None).to_period("M")

    # ---- månadsvis ------------------------------------------------------- #
    rows = {}
    for m, d in loc.groupby(loc.index.to_period("M")):
        if m >= cur_month and not include_partial:
            continue
        expected = m.days_in_month * 24
        if len(d) < 0.9 * expected and m < cur_month:
            continue                                   # för många saknade timmar
        row = {
            "n": len(d),
            "sum_p": d["price"].sum(),
            "base": d["price"].mean(),
            "neg_hours": int((d["price"] < 0).sum()),
        }
        for tech in ("wind", "solar"):
            ok = d[tech].notna()
            coverage = ok.mean()
            g = d.loc[ok, tech]
            if coverage >= MIN_COVERAGE and g.sum() > 0:
                row[f"{tech}_sum_g"] = g.sum()
                row[f"{tech}_sum_pg"] = (d.loc[ok, "price"] * g).sum()
        rows[str(m)] = row
    if not rows:
        return None

    first, last = pd.Period(min(rows), "M"), pd.Period(max(rows), "M")
    months = [str(p) for p in pd.period_range(first, last, freq="M")]
    mdf = pd.DataFrame.from_dict(rows, orient="index").reindex(months)
    for col in ("wind_sum_g", "wind_sum_pg", "solar_sum_g", "solar_sum_pg"):
        if col not in mdf:
            mdf[col] = np.nan

    # ---- rullande 12 mån ------------------------------------------------- #
    r_base = mdf["sum_p"].rolling(12, min_periods=12).sum() / mdf["n"].rolling(12, min_periods=12).sum()
    tech_out = {}
    for tech in ("wind", "solar"):
        sg, spg = mdf[f"{tech}_sum_g"], mdf[f"{tech}_sum_pg"]
        price = spg / sg
        r_price = spg.rolling(12, min_periods=12).sum() / sg.rolling(12, min_periods=12).sum()
        tech_out[tech] = {
            "price": lst(price),
            "rate": lst(price / mdf["base"], 4),
            "gwh": lst(sg / 1000, 1),
            "r_price": lst(r_price),
            "r_rate": lst(r_price / r_base, 4),
        }

    # ---- dygnsspread ----------------------------------------------------- #
    day = loc.index.normalize()
    dg = loc.groupby(day)["price"].agg(["max", "min", "mean", "count"])
    dg = dg[dg["count"] >= MIN_DAY_HOURS]
    dg["spread"] = dg["max"] - dg["min"]
    dg["mm"] = dg["max"] - dg["mean"]
    dg["month"] = dg.index.to_period("M").astype(str)
    sp = dg.groupby("month").agg(
        spread_mean=("spread", "mean"),
        spread_max=("spread", "max"),
        mm_mean=("mm", "mean"),
        mm_max=("mm", "max"),
        days=("spread", "size"),
    ).reindex(months)

    # ---- timprofil per månad (för duck curve, dag- och nattpriser) ------ #
    hp = (loc.groupby([loc.index.to_period("M").astype(str), loc.index.hour])["price"]
             .mean().unstack().reindex(index=months, columns=range(24)))

    zone_out = {
        "months": months,
        "base": lst(mdf["base"]),
        "r_base": lst(r_base),
        "neg_hours": [None if pd.isna(v) else int(v) for v in mdf["neg_hours"]],
        "wind": tech_out["wind"],
        "solar": tech_out["solar"],
        "spread": {c: lst(sp[c]) for c in sp.columns if c != "days"},
        "spread_days": [None if pd.isna(v) else int(v) for v in sp["days"]],
        "n": [None if pd.isna(v) else int(v) for v in mdf["n"]],
        "hp": [[None if pd.isna(v) else round(float(v), 1) for v in row] for row in hp.to_numpy()],
    }

    # ---- senaste dygnen i timupplösning ---------------------------------- #
    cutoff = loc.index.max().normalize() - pd.Timedelta(days=RECENT_DAYS - 1)
    rec = loc[loc.index >= cutoff]
    recent_out = {
        "t": [t.strftime("%Y-%m-%d %H:%M") for t in rec.index],
        "price": lst(rec["price"], 2),
        "wind": lst(rec["wind"], 0),
        "solar": lst(rec["solar"], 0),
    }
    return zone_out, recent_out


def lst(series, nd: int = 2) -> list:
    return [None if pd.isna(v) else round(float(v), nd) for v in series]


# --------------------------------------------------------------------------- #
# Fiktiv solpark med batteri (BESS)
# --------------------------------------------------------------------------- #
PV = {"lat": 56.674, "lon": 12.857, "tilt": 30, "aspect": 0, "loss": 14, "year": 2019}   # aspect 0 = söder i PVGIS
PVGIS_URLS = [
    "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc",
    "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc",
    "https://re.jrc.ec.europa.eu/api/seriescalc",
]
BESS_HOURS = [1, 2, 4]            # batteriets varaktighet vid full effekt
BESS_POWER_PER_MWP = 1.0          # MW batterieffekt per MWp solpark
BESS_RTE = 0.88                   # verkningsgrad tur och retur (laddning + urladdning)
SCENARIOS = ["none"] + [f"{h}h" for h in BESS_HOURS]


def profile_array(prof: pd.DataFrame) -> np.ndarray:
    """Gör om (månad, dag, timme, cf) till en 12x31x24-tabell med kapacitetsfaktor (MW per MWp), UTC."""
    arr = np.full((12, 31, 24), np.nan)
    arr[prof["month"].to_numpy() - 1, prof["day"].to_numpy() - 1, prof["hour"].to_numpy()] = prof["cf"].to_numpy()
    arr[1, 28, :] = np.where(np.isnan(arr[1, 28, :]), arr[1, 27, :], arr[1, 28, :])      # 29 februari saknas i vanliga år
    return arr


def annual_yield(arr: np.ndarray) -> float:
    """Årsproduktion i kWh per kWp (summa av timvärdena, 29 februari räknas inte dubbelt)."""
    return float(np.nansum(arr) - np.nansum(arr[1, 28, :]))


def fetch_pv_profile(allow_network: bool = True) -> np.ndarray | None:
    """Timprofil för 1 MWp i Halmstad (lutning 30°, söder) från PVGIS, cachad i data/raw."""
    path = RAW_DIR / f"pvgis_{PV['year']}_t{PV['tilt']}_a{PV['aspect']}_l{PV['loss']}.csv.gz"
    if path.exists():
        return profile_array(pd.read_csv(path))
    if not allow_network:
        return None
    import requests
    params = {"lat": PV["lat"], "lon": PV["lon"], "peakpower": 1, "loss": PV["loss"], "angle": PV["tilt"],
              "aspect": PV["aspect"], "startyear": PV["year"], "endyear": PV["year"], "pvcalculation": 1,
              "outputformat": "json", "usehorizon": 1, "mountingplace": "free", "pvtechchoice": "crystSi"}
    data = None
    for url in PVGIS_URLS:
        try:
            r = requests.get(url, params=params, timeout=180)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as exc:
            log.warning("PVGIS (%s) misslyckades: %s", url.split("/api/")[1], exc)
    if data is None:
        return None
    rows = data["outputs"]["hourly"]
    t = pd.to_datetime([r["time"] for r in rows], format="%Y%m%d:%H%M")
    prof = pd.DataFrame({"month": t.month, "day": t.day, "hour": t.hour, "cf": [r["P"] / 1000.0 for r in rows]})
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    prof.to_csv(path, index=False, compression="gzip")
    log.info("PVGIS: hämtade %d timmar för %s (år %d)", len(prof), "Halmstad", PV["year"])
    return profile_array(prof)


def demo_pv_array() -> np.ndarray:
    """Syntetisk profil (bara för förhandsvisning utan nätverk)."""
    rng = np.random.default_rng(3)
    arr = np.zeros((12, 31, 24))
    for m in range(12):
        for d in range(31):
            doy = m * 30.4 + d
            seas = np.cos(2 * np.pi * (doy - 172) / 365)          # 1 vid midsommar
            day_len = 12 + 5.5 * seas
            peak = 0.14 + 0.52 * (seas + 1) / 2
            cloud = np.clip(rng.beta(2.2, 1.3), 0.12, 1.0)
            for h in range(24):
                x = (h + 0.5 - 11.0) / (day_len / 2)               # UTC ≈ lokal tid minus 1–2 h
                arr[m, d, h] = max(0.0, peak * cloud * np.cos(np.clip(x, -1, 1) * np.pi / 2) ** 1.3) if abs(x) < 1 else 0.0
    arr[1, 28, :] = arr[1, 27, :]
    return arr


def optimise_day(p: np.ndarray, g: np.ndarray, energy: float, power: float, rte: float):
    """
    Bästa laddning (c) och urladdning (d) för ett dygn, i MWh per timme.
    Batteriet laddas bara från solparken (ingen nätladdning) och måste vara tomt vid dygnets slut.
    Intäkten är summan av pris * (produktion - c + d).  Lösningen är exakt (linjärprogram).
    """
    from scipy.optimize import linprog
    n = len(p)
    eta = rte ** 0.5
    L = np.tril(np.ones((n, n)))
    cost = np.concatenate([p, -p])
    A_ub = np.block([[eta * L, -L / eta], [-eta * L, L / eta]])
    b_ub = np.concatenate([np.full(n, energy), np.zeros(n)])
    A_eq = np.concatenate([np.full(n, eta), np.full(n, -1.0 / eta)])[None, :]
    bounds = [(0.0, float(min(power, gi))) for gi in g] + [(0.0, power)] * n
    res = linprog(cost, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=[0.0], bounds=bounds, method="highs")
    if res.status != 0:
        return np.zeros(n), np.zeros(n)
    return np.clip(res.x[:n], 0, None), np.clip(res.x[n:], 0, None)


def compute_solar_bess(prices: dict, cf_arr: np.ndarray, window: list[str], names: dict) -> dict:
    """Capture price/rate för en solpark (1 MWp) utan och med 1, 2 och 4 timmars batteri, per elområde."""
    power = BESS_POWER_PER_MWP
    result = {}
    for key, price in prices.items():
        s = price.dropna()
        if s.empty:
            continue
        loc_idx = s.index.tz_convert(TZ)
        month = np.asarray(loc_idx.strftime("%Y-%m"))
        sel = np.isin(month, window)
        if sel.sum() < 24 * 200:
            continue
        s, loc_idx, month = s[sel], loc_idx[sel], month[sel]
        p_all = s.to_numpy(dtype=float)
        g_all = np.nan_to_num(cf_arr[s.index.month - 1, s.index.day - 1, s.index.hour])
        date = np.asarray(loc_idx.strftime("%Y-%m-%d"))
        days = pd.Series(np.arange(len(s))).groupby(date).indices

        acc = {m: {sc: np.zeros(4) for sc in SCENARIOS} for m in window}        # intäkt, produktion, laddat, urladdat
        base = {m: np.zeros(2) for m in window}                                  # summa pris, antal timmar
        cands = {m: [] for m in window}                                          # kandidater till exempeldag
        for d, idx in days.items():
            n = len(idx)
            if n < 23:
                continue
            p, g, m = p_all[idx], g_all[idx], month[idx[0]]
            base[m] += (p.sum(), n)
            rev0 = float((p * g).sum())
            acc[m]["none"] += (rev0, g.sum(), 0.0, 0.0)
            if n == 24 and g.sum() > 0:
                cands[m].append((float(g.sum()), d))
            for hrs in BESS_HOURS:
                if g.sum() < 1e-6 or p.max() - p.min() < 1e-6:
                    acc[m][f"{hrs}h"] += (rev0, g.sum(), 0.0, 0.0)
                    continue
                c, dch = optimise_day(p, g, hrs * power, power, BESS_RTE)
                acc[m][f"{hrs}h"] += (float((p * (g - c + dch)).sum()), g.sum(), c.sum(), dch.sum())

        def cp_cr(a, b):
            return (a[0] / a[1], (a[0] / a[1]) / (b[0] / b[1])) if a[1] > 0 and b[1] > 0 else (None, None)

        tot_base = sum(base.values(), np.zeros(2))
        total, monthly = {}, {"months": window, "base": [None if base[m][1] == 0 else round(base[m][0] / base[m][1], 2) for m in window]}
        for sc in SCENARIOS:
            a = sum((acc[m][sc] for m in window), np.zeros(4))
            cp, cr = cp_cr(a, tot_base)
            entry = {"cp": None if cp is None else round(cp, 2), "cr": None if cr is None else round(cr, 4)}
            if sc != "none" and a[1] > 0:
                hrs = int(sc[:-1])
                entry["share"] = round(a[2] / a[1], 4)                       # andel av produktionen som går via batteriet
                entry["cycles"] = round(a[3] / (hrs * power), 1)             # fulla cykler under perioden
            total[sc] = entry
            vals = [cp_cr(acc[m][sc], base[m]) for m in window]
            monthly[sc] = {"cp": [None if v[0] is None else round(v[0], 2) for v in vals],
                           "cr": [None if v[1] is None else round(v[1], 4) for v in vals]}

        # exempeldag: dagen med medianproduktion i varje månad
        examples = []
        for m in window:
            if not cands[m]:
                continue
            ordered = sorted(cands[m])
            d = ordered[len(ordered) // 2][1]
            idx = days[d]
            p, g = p_all[idx], g_all[idx]
            ex = {"month": m, "date": d, "p": [round(float(v), 2) for v in p], "pv": [round(float(v), 3) for v in g]}
            for hrs in BESS_HOURS:
                c, dch = optimise_day(p, g, hrs * power, power, BESS_RTE)
                ex[f"{hrs}h"] = [round(float(v), 3) for v in (g - c + dch)]
            examples.append(ex)

        result[key] = {"name": names.get(key, key), "base": round(float(tot_base[0] / tot_base[1]), 2),
                       "yield": round(float(sum(acc[m]["none"][1] for m in window)), 0),
                       "total": total, "monthly": monthly, "examples": examples}
        log.info("%s: solpark + BESS klar (%d dygn)", key, len(days))
    return result


# --------------------------------------------------------------------------- #
# Prispåverkan: hur mycket rör sig priset per GW restlast?
# --------------------------------------------------------------------------- #
# Idé: 1 GW batteriladdning verkar på priset som 1 GW extra efterfrågan, och 1 GW urladdning som 1 GW
# mindre. Hur priset rör sig när restlasten (förbrukning minus vind och sol) varierar av sig själv
# går att skatta ur historiken: pris mot restlast, med fasta effekter för månad (bränslepris, vattenläge)
# och timme på dygnet x vardag/helg. Vind och förbrukning varierar av väderskäl, så det är bra variation.
SLOPE_MONTHS = 24
DAYPARTS = {"natt": [22, 23, 0, 1, 2, 3, 4, 5], "morgon": [6, 7, 8, 9], "dag": [10, 11, 12, 13, 14, 15],
            "kväll": [16, 17, 18, 19, 20, 21]}
REFERENCE_ZONE = "DE"             # kontrolleras för i övriga områden, eftersom de prissätts tillsammans med Tyskland


def _rl_gw(d: pd.DataFrame) -> pd.Series:
    """Restlast i GW: förbrukning minus vind och sol (används bara om serien finns)."""
    rl = d["load"].copy()
    if d["wind"].notna().mean() >= 0.5:
        rl = rl - d["wind"]
    if d["solar"].notna().mean() >= 0.5:
        rl = rl - d["solar"]
    return rl / 1000.0


def _fe_matrix(idx_local) -> np.ndarray:
    month_id = (idx_local.year * 12 + idx_local.month).to_numpy()
    _, mi = np.unique(month_id, return_inverse=True)
    hd = (idx_local.hour + 24 * (idx_local.dayofweek >= 5)).to_numpy()      # timme x vardag/helg
    n = len(idx_local)
    z = np.zeros((n, mi.max() + 1 + 48))
    z[np.arange(n), mi] = 1.0
    z[np.arange(n), mi.max() + 1 + hd] = 1.0
    return z


def _cluster_ols(y: np.ndarray, x: np.ndarray, g: np.ndarray, k_fe: int):
    """OLS med klusterrobusta standardfel (kluster = vecka). Returnerar koefficienter och standardfel."""
    n, k = x.shape
    xtx_inv = np.linalg.inv(x.T @ x)
    b = xtx_inv @ (x.T @ y)
    e = y - x @ b
    order = np.argsort(g, kind="stable")
    gs, xs, es = g[order], x[order], e[order]
    starts = np.flatnonzero(np.r_[True, gs[1:] != gs[:-1]])
    scores = np.add.reduceat(xs * es[:, None], starts, axis=0)
    n_cl = len(starts)
    meat = scores.T @ scores
    v = xtx_inv @ meat @ xtx_inv * (n_cl / max(n_cl - 1, 1)) * ((n - 1) / max(n - k - k_fe, 1))
    return b, np.sqrt(np.diag(v))


def _fit(y: np.ndarray, xs: list, z: np.ndarray, g: np.ndarray):
    """Frisch–Waugh: rensa y och x från fasta effekter, kör sedan OLS. Returnerar (b, lo, hi, y_resid, x1_resid)."""
    yy = np.column_stack([y] + xs)
    coef, *_ = np.linalg.lstsq(z, yy, rcond=None)
    r = yy - z @ coef
    b, se = _cluster_ols(r[:, 0], r[:, 1:], g, z.shape[1])
    return float(b[0]), float(b[0] - 1.96 * se[0]), float(b[0] + 1.96 * se[0]), r[:, 0], r[:, 1]


def _ols_row(f) -> dict:
    return {"b": round(f[0], 3), "lo": round(f[1], 3), "hi": round(f[2], 3)}


def estimate_slopes(frames: dict, names: dict, targets: list, months_back: int = SLOPE_MONTHS) -> dict:
    """
    Fyra modellvarianter per område (alla med fasta effekter för månad och klockslag/vardag/helg):
      a  bara områdets egen restlast
      b  + Tysklands restlast
      c  + restlast i alla andra områden vi har data för (inklusive Norge)   <- huvudskattning
      d  som c men bara de senaste 12 månaderna
    Banden i dashboarden spänner över b, c och d.
    """
    rl_all = {}
    for key, df in frames.items():
        if "load" in df and df["load"].notna().sum() >= 24 * 120:
            rl_all[key] = _rl_gw(df[["load", "wind", "solar"]])
    rl_all = pd.DataFrame(rl_all)
    out = {}
    for key in targets:
        df = frames.get(key)
        if df is None or key not in rl_all or "price" not in df:
            continue
        d = pd.DataFrame({"price": df["price"], "rl": rl_all[key]})
        d = d[d.index >= d.index.max() - pd.DateOffset(months=months_back)]
        d = d[d["price"].notna() & d["rl"].notna()]
        if len(d) < 24 * 120:
            continue
        others = rl_all.drop(columns=[key]).reindex(d.index)
        others = others.loc[:, others.notna().mean() >= 0.7]
        others = others.interpolate(limit=6, limit_area="inside")
        ref_ok = REFERENCE_ZONE in others and key != REFERENCE_ZONE

        lo_p, hi_p = np.percentile(d["price"], [1, 99])
        y_all = d["price"].clip(lo_p, hi_p).to_numpy(dtype=float)
        idx_local = d.index.tz_convert(TZ)
        z_all = _fe_matrix(idx_local)
        week_all = (idx_local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]").astype(np.int64) // 7)
        x_own = d["rl"].to_numpy(dtype=float)
        hours = idx_local.hour.to_numpy()
        recent = (d.index >= d.index.max() - pd.DateOffset(months=12))

        def run(cols, mask=None):
            ok = np.ones(len(d), dtype=bool) if mask is None else mask.copy()
            xs = [x_own]
            for c in cols:
                v = others[c].to_numpy(dtype=float)
                ok &= ~np.isnan(v)
                xs.append(v)
            if ok.sum() < 24 * 60:
                return None
            return _fit(y_all[ok], [x[ok] for x in xs], z_all[ok], week_all[ok]), ok

        res = {"name": names.get(key, key), "n": int(len(d)), "central": "a", "b": None, "c": None, "d": None}
        fa = run([])
        res["a"] = _ols_row(fa[0])
        central = fa
        fb = run([REFERENCE_ZONE]) if ref_ok else None
        if fb:
            res["b"], res["central"], central = _ols_row(fb[0]), "b", fb
        ctrl_cols = list(others.columns)
        fc = run(ctrl_cols) if ctrl_cols else None
        if fc:
            res["c"], res["central"], central = _ols_row(fc[0]), "c", fc
            res["controls"] = ctrl_cols
            fd = run(ctrl_cols, recent)
            if fd:
                res["d"] = _ols_row(fd[0])
        used = [res[k] for k in ("b", "c", "d") if res[k]] or [res["a"]]
        res["band"] = {"lo": round(min(u["lo"] for u in used), 3), "hi": round(max(u["hi"] for u in used), 3)}

        # per tid på dygnet, samma specifikation som huvudskattningen
        cols_c = ctrl_cols if res["central"] == "c" else ([REFERENCE_ZONE] if res["central"] == "b" else [])
        res["dayparts"] = {}
        for name, hrs in DAYPARTS.items():
            f = run(cols_c, np.isin(hours, hrs))
            if f:
                res["dayparts"][name] = {**_ols_row(f[0]), "n": int(f[1].sum())}
        # binnat spridningsdiagram av renade värden
        xr, yr = central[0][4], central[0][3]
        edges = np.quantile(xr, np.linspace(0, 1, 21))
        bin_id = np.clip(np.searchsorted(edges, xr, side="right") - 1, 0, 19)
        res["bins"] = {"x": [round(float(xr[bin_id == i].mean()), 3) for i in range(20)],
                       "y": [round(float(yr[bin_id == i].mean()), 2) for i in range(20)]}
        res["rl_range"] = [round(float(np.percentile(xr, 5)), 2), round(float(np.percentile(xr, 95)), 2)]
        out[key] = res
        c = res[res["central"]]
        log.info("%s: prispåverkan %.1f €/MWh per GW (huvudskattning %s; spann %.1f–%.1f), n=%d", key, c["b"], res["central"],
                 res["band"]["lo"], res["band"]["hi"], len(d))
    return out


# --------------------------------------------------------------------------- #
# Arbitragedjup: hur många batterier tål day-ahead-arbitrage?
# --------------------------------------------------------------------------- #
# Flottan av batterier är prisundertagare. När hela flottan laddar/laddar ur med en andel x av full effekt
# flyttas priset med kappa * x  (kappa = prispåverkan per GW * flottans storlek i GW).
# Intäkten per MW beror bara på kappa, så ett svep över kappa räcker för alla flottstorlekar och alla
# prispåverkansvärden. Dashboarden gör resten (kappa = s * N).
DEPTH_KAPPA = [0, 0.5, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384]
DEPTH_BREAKS = np.array([0, 0.01, 0.025, 0.05, 0.1, 0.18, 0.3, 0.5, 0.75, 1.0])   # glesare vid hög effekt
DEPTH_DAY_STEP = 4                 # var fjärde dygn räcker för ett årsmedel
DEPTH_HOURS = [1, 2, 4]


@functools.lru_cache(maxsize=None)
def _fleet_matrices(n: int, k: int, eta: float):
    ones = np.ones((1, k))
    lk = np.kron(np.tril(np.ones((n, n))), ones)
    a_ub = np.block([[eta * lk, -lk / eta], [-eta * lk, lk / eta]])
    a_eq = np.concatenate([np.full(n * k, eta), np.full(n * k, -1.0 / eta)])[None, :]
    return a_ub, a_eq


def fleet_day(p: np.ndarray, kappa: float, tau: float, rte: float = BESS_RTE):
    """
    Flottans bästa laddning (c) och urladdning (d) ett dygn, som andel av full effekt per timme.
    Prispåverkan är linjär och approximeras med trappsteg (ger ett exakt linjärprogram).
    """
    from scipy.optimize import linprog
    n, eta = len(p), rte ** 0.5
    breaks = np.array([0.0, 1.0]) if kappa == 0 else DEPTH_BREAKS
    w, x = np.diff(breaks), (breaks[:-1] + breaks[1:]) / 2
    k = len(w)
    a_ub, a_eq = _fleet_matrices(n, k, eta)
    cost = np.concatenate([(p[:, None] + kappa * x[None, :]).ravel(), -(p[:, None] - kappa * x[None, :]).ravel()])
    b_ub = np.concatenate([np.full(n, tau), np.zeros(n)])
    bounds = [(0.0, float(wi)) for _ in range(n) for wi in w] * 2
    res = linprog(cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=[0.0], bounds=bounds, method="highs")
    if res.status != 0:
        return np.zeros(n), np.zeros(n)
    return res.x[: n * k].reshape(n, k).sum(1), res.x[n * k:].reshape(n, k).sum(1)


def compute_depth(prices: dict, window: list[str], names: dict, day_step: int) -> dict:
    result = {}
    for key, price in prices.items():
        s = price.dropna()
        if s.empty:
            continue
        loc_idx = s.index.tz_convert(TZ)
        month = np.asarray(loc_idx.strftime("%Y-%m"))
        sel = np.isin(month, window)
        if sel.sum() < 24 * 200:
            continue
        s, loc_idx = s[sel], loc_idx[sel]
        date = np.asarray(loc_idx.strftime("%Y-%m-%d"))
        groups = pd.Series(np.arange(len(s))).groupby(date).indices
        vals = s.to_numpy(dtype=float)
        days = [vals[idx] for _, idx in sorted(groups.items()) if len(idx) == 24]
        sample = days[::day_step]
        if len(sample) < 10:
            continue
        spread0 = float(np.mean([d.max() - d.min() for d in days]))
        res = {}
        for hrs in DEPTH_HOURS:
            rev, spr, cyc = [], [], []
            for kappa in DEPTH_KAPPA:
                r_sum = s_sum = c_sum = 0.0
                for p in sample:
                    c, d = fleet_day(p, kappa, float(hrs))
                    r_sum += float((d * (p - kappa * d) - c * (p + kappa * c)).sum())
                    pp = p + kappa * (c - d)
                    s_sum += float(pp.max() - pp.min())
                    c_sum += float(d.sum() / hrs)
                n = len(sample)
                rev.append(round(max(0.0, r_sum / n * 365 / 1000), 2))      # k€ per MW och år
                spr.append(round(s_sum / n, 2))
                cyc.append(round(c_sum / n, 3))
            res[f"{hrs}h"] = {"rev": rev, "spread": spr, "cycles": cyc}
        result[key] = {"name": names.get(key, key), "spread0": round(spread0, 1), "days": len(sample), "res": res}
        log.info("%s: arbitragedjup klart (%d dygn, rev0 2h = %.0f k€/MW/år)", key, len(sample), res["2h"]["rev"][0])
    return result


# --------------------------------------------------------------------------- #
# Demodata (för att förhandsgranska dashboarden utan token)
# --------------------------------------------------------------------------- #
def demo_frame(key: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp.now(tz="UTC").floor("h"), freq="h")
    n = len(idx)
    local = idx.tz_convert(TZ)
    hour = local.hour.to_numpy()
    doy = local.dayofyear.to_numpy()
    winter = np.cos(2 * np.pi * (doy - 15) / 365)

    x = np.zeros(n)
    eps = rng.normal(size=n)
    for i in range(1, n):
        x[i] = 0.994 * x[i - 1] + 0.12 * eps[i]
    wind_cf = np.clip(0.33 + 0.22 * x / x.std() + 0.08 * winter, 0.0, 1.0)

    day_len = 12 + 6 * (-winter)
    sun = np.clip(np.sin(np.pi * (hour - (12 - day_len / 2)) / day_len), 0, None)
    cloud = np.clip(0.75 + 0.2 * rng.normal(size=n) * 0.5, 0.2, 1.0)
    solar_cf = sun * (0.45 + 0.4 * (-winter)) * cloud

    level = {"SE1": 0.45, "SE2": 0.5, "SE3": 0.85, "SE4": 1.0, "DE": 1.5, "DK1": 1.3, "DK2": 1.3, "FI": 0.75}.get(key, 0.8)
    shape = 10 * np.exp(-((hour - 8) ** 2) / 8) + 16 * np.exp(-((hour - 19) ** 2) / 6) - 6 * np.exp(-((hour - 3) ** 2) / 10)
    price = (58 + 24 * winter + shape) * level - 70 * (wind_cf - 0.33) * (0.5 + level / 2)
    price -= 22 * level * solar_cf
    price += rng.normal(scale=6, size=n) + np.where(rng.random(n) < 0.004, rng.normal(120, 60, size=n), 0)
    price = np.maximum(price, -40)

    wind_mw = {"SE1": 900, "SE2": 2300, "SE3": 2000, "SE4": 1500, "DE": 55000, "DK1": 4500, "DK2": 1800, "FI": 6500}.get(key, 700) * wind_cf
    solar_mw = {"SE3": 700, "SE4": 600, "DE": 70000, "DK1": 2800, "DK2": 1500, "FI": 900}.get(key)
    solar = solar_mw * solar_cf if solar_mw else np.full(n, np.nan)   # SE1/SE2 saknar solrapportering
    load_mean = {"SE1": 1900, "SE2": 2400, "SE3": 9500, "SE4": 3400, "DE": 55000, "DK1": 3200, "DK2": 1700, "FI": 9500, "NO1": 4500, "NO2": 4000, "NO3": 2500, "NO4": 1700, "NO5": 3000}.get(key, 3000)
    load = load_mean * (1 + 0.12 * winter + 0.08 * np.sin((hour - 6) / 24 * 2 * np.pi) + 0.02 * rng.normal(size=n))
    rl = (load - wind_mw - np.nan_to_num(solar)) / 1000
    s_demo = {"SE1": 1.0, "SE2": 1.2, "SE3": 2.0, "SE4": 2.8, "DE": 1.6, "DK1": 2.2, "DK2": 2.4, "FI": 1.8}.get(key, 2.0)
    price = price + s_demo * (rl - rl.mean())
    df = pd.DataFrame({"price": price, "wind": wind_mw, "solar": solar, "load": load}, index=idx)
    if ZONES[key].get("ctrl"):
        df["price"] = np.nan
        df["solar"] = np.nan
    return df


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zones", default=",".join(ZONES), help="kommaseparerad lista, t.ex. SE3,SE4")
    ap.add_argument("--start", default=os.getenv("START_DATE", START_DEFAULT), help="första datum vid ny cache")
    ap.add_argument("--demo", action="store_true", help="skapa syntetisk demodata (ingen token)")
    ap.add_argument("--no-fetch", action="store_true", help="hoppa över hämtning, räkna om från cache")
    ap.add_argument("--include-partial", action="store_true", help="ta med pågående månad")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    keys = [k.strip().upper() for k in args.zones.split(",") if k.strip()]
    unknown = [k for k in keys if k not in ZONES]
    if unknown:
        log.error("Okänt område: %s (giltiga: %s)", ", ".join(unknown), ", ".join(ZONES))
        return 2

    client = no_match = None
    if not args.demo and not args.no_fetch:
        token = os.getenv("ENTSOE_API_KEY", "").strip()
        if not token:
            log.error("ENTSOE_API_KEY saknas. Sätt miljövariabeln (eller GitHub-secret) och kör igen,\n"
                      "eller kör med --demo för syntetisk data.")
            return 1
        from entsoe import EntsoePandasClient
        from entsoe.exceptions import NoMatchingDataError
        client, no_match = EntsoePandasClient(api_key=token), NoMatchingDataError

    summary_zones, recent_zones, frames = {}, {}, {}
    for i, key in enumerate(keys):
        info = ZONES[key]
        if args.demo:
            df = demo_frame(key, seed=100 + i)
        elif args.no_fetch:
            df = load_cache(key)
        else:
            ctrl = info.get("ctrl", False)
            start_key = args.start
            if ctrl:                     # kontrollområden behöver bara de senaste ca två åren
                limit = (pd.Timestamp.now(tz=TZ).normalize() - pd.DateOffset(months=SLOPE_MONTHS + 1)).strftime("%Y-%m-%d")
                start_key = max(args.start, limit)
            df = update_cache(client, no_match, key, info["code"], start_key, gen_only=ctrl)
        if df is None or df.empty:
            log.warning("%s: ingen data, hoppar över", key)
            continue
        frames[key] = df
        if info.get("ctrl"):
            continue                     # inga egna flikar för kontrollområden
        result = compute_zone(df, include_partial=args.include_partial)
        if result is None:
            log.warning("%s: för lite data för att räkna", key)
            continue
        zone_out, recent_out = result
        zone_out["name"] = info["name"]
        summary_zones[key] = zone_out
        recent_zones[key] = recent_out

    if not summary_zones:
        log.error("Ingen data att skriva.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (OUT_DIR / "summary.json").write_text(
        json.dumps({"generated": stamp, "demo": bool(args.demo), "unit": "EUR/MWh",
                    "order": list(summary_zones), "zones": summary_zones},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    (OUT_DIR / "recent.json").write_text(
        json.dumps({"generated": stamp, "zones": recent_zones}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    months_all = sorted({m for z in summary_zones.values() for m in z["months"]})

    # ---- fiktiv solpark med batteri -------------------------------------- #
    try:
        cf_arr = demo_pv_array() if args.demo else fetch_pv_profile(allow_network=not args.no_fetch)
        if cf_arr is None:
            log.warning("Solpark + BESS hoppades över (ingen PVGIS-profil).")
        else:
            months_all = sorted({m for z in summary_zones.values() for m in z["months"]})
            res = compute_solar_bess({k: frames[k]["price"] for k in summary_zones}, cf_arr, months_all[-12:],
                                     {k: ZONES[k]["name"] for k in summary_zones})
            site = {"name": "Halmstad", **PV, "bess_hours": BESS_HOURS, "bess_power_mw_per_mwp": BESS_POWER_PER_MWP,
                    "rte": BESS_RTE, "window": months_all[-12:], "annual_kwh_per_kwp": round(annual_yield(cf_arr))}
            log.info("Solparkens årsproduktion i profilen: %d kWh/kWp", site["annual_kwh_per_kwp"])
            (OUT_DIR / "solar_bess.json").write_text(
                json.dumps({"generated": stamp, "demo": bool(args.demo), "site": site, "zones": res},
                           ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as exc:                       # felet ska aldrig stoppa resten av uppdateringen
        log.warning("Solpark + BESS hoppades över: %s", exc)

    # ---- prispåverkan (skattas ur last, vind, sol och pris) ----------------- #
    try:
        slopes = estimate_slopes(frames, {k: ZONES[k]["name"] for k in frames}, list(summary_zones))
        if slopes:
            (OUT_DIR / "slope.json").write_text(
                json.dumps({"generated": stamp, "demo": bool(args.demo), "months": SLOPE_MONTHS, "reference": REFERENCE_ZONE,
                            "dayparts": DAYPARTS, "zones": slopes}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        else:
            log.warning("Prispåverkan: ingen last i datan ännu, hoppar över")
    except Exception as exc:
        log.warning("Prispåverkan hoppades över: %s", exc)

    # ---- arbitragedjup (tung beräkning: görs bara när underlaget ändrats) --- #
    try:
        window = months_all[-12:]
        step = 20 if args.demo else DEPTH_DAY_STEP
        sig = {"window": window, "rte": BESS_RTE, "kappa": DEPTH_KAPPA, "step": step,
               "breaks": DEPTH_BREAKS.tolist(), "hours": DEPTH_HOURS, "zones": sorted(summary_zones)}
        path = OUT_DIR / "depth.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if old and old.get("sig") == sig and not args.demo:
            log.info("Arbitragedjup: oförändrat underlag, hoppar över beräkningen")
        else:
            depth = compute_depth({k: frames[k]["price"] for k in summary_zones}, window,
                                  {k: ZONES[k]["name"] for k in summary_zones}, step)
            path.write_text(json.dumps({"generated": stamp, "demo": bool(args.demo), "sig": sig, "window": window,
                                        "rte": BESS_RTE, "kappa": DEPTH_KAPPA, "durations": DEPTH_HOURS, "zones": depth},
                                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as exc:
        log.warning("Arbitragedjup hoppades över: %s", exc)
    log.info("Klart: %d områden skrivna till %s", len(summary_zones), OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
