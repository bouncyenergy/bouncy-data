#!/usr/bin/env python3
"""
Bouncy Energy – marknadsdata från ENTSO-E.

Hämtar day-ahead-priser och faktisk produktion (vind, sol) per elområde,
sparar en lokal cache (data/raw/*.csv.gz) och räknar ut:

  * capture price / capture rate per månad och teknik (vind, sol)
  * rullande 12 månader (produktionsviktat)
  * dygnsspread per månad (medel och max)
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
}

log = logging.getLogger("bouncy")


# --------------------------------------------------------------------------- #
# Hämtning från ENTSO-E
# --------------------------------------------------------------------------- #
def month_chunks(start: pd.Timestamp, end: pd.Timestamp):
    """Delar upp [start, end) i kalendermånader (ENTSO-E vill ha rimliga intervall)."""
    cur = start
    while cur < end:
        nxt = min(cur + pd.offsets.MonthBegin(1), end)
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


def fetch_zone(client, no_match_error, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
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


def update_cache(client, no_match_error, key: str, code: str, start_default: str) -> pd.DataFrame | None:
    cached = load_cache(key)
    now_local = pd.Timestamp.now(tz=TZ)
    end = now_local.normalize() + pd.Timedelta(days=2)   # day-ahead för i morgon finns efter ca 13:00
    if cached is not None and len(cached):
        # Hämta om de senaste 7 dagarna – produktionsdata revideras i efterhand
        start = (cached.index.max() - pd.Timedelta(days=7)).tz_convert(TZ).normalize()
    else:
        start = pd.Timestamp(start_default, tz=TZ)
    log.info("%s: hämtar %s → %s", key, start.date(), end.date())
    new = fetch_zone(client, no_match_error, code, start, end)
    if new is None:
        log.warning("%s: ingen ny data", key)
        return cached
    merged = new if cached is None else new.combine_first(cached).sort_index()
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
    ).reindex(months)

    zone_out = {
        "months": months,
        "base": lst(mdf["base"]),
        "r_base": lst(r_base),
        "neg_hours": [None if pd.isna(v) else int(v) for v in mdf["neg_hours"]],
        "wind": tech_out["wind"],
        "solar": tech_out["solar"],
        "spread": {c: lst(sp[c]) for c in sp.columns},
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

    level = {"SE1": 0.45, "SE2": 0.5, "SE3": 0.85, "SE4": 1.0, "DE": 1.5, "DK1": 1.3, "DK2": 1.3, "FI": 0.75}[key]
    shape = 10 * np.exp(-((hour - 8) ** 2) / 8) + 16 * np.exp(-((hour - 19) ** 2) / 6) - 6 * np.exp(-((hour - 3) ** 2) / 10)
    price = (58 + 24 * winter + shape) * level - 70 * (wind_cf - 0.33) * (0.5 + level / 2)
    price -= 22 * level * solar_cf
    price += rng.normal(scale=6, size=n) + np.where(rng.random(n) < 0.004, rng.normal(120, 60, size=n), 0)
    price = np.maximum(price, -40)

    wind_mw = {"SE1": 900, "SE2": 2300, "SE3": 2000, "SE4": 1500, "DE": 55000, "DK1": 4500, "DK2": 1800, "FI": 6500}[key] * wind_cf
    solar_mw = {"SE3": 700, "SE4": 600, "DE": 70000, "DK1": 2800, "DK2": 1500, "FI": 900}.get(key)
    solar = solar_mw * solar_cf if solar_mw else np.full(n, np.nan)   # SE1/SE2 saknar solrapportering
    return pd.DataFrame({"price": price, "wind": wind_mw, "solar": solar}, index=idx)


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

    summary_zones, recent_zones = {}, {}
    for i, key in enumerate(keys):
        info = ZONES[key]
        if args.demo:
            df = demo_frame(key, seed=100 + i)
        elif args.no_fetch:
            df = load_cache(key)
        else:
            df = update_cache(client, no_match, key, info["code"], args.start)
        if df is None or df.empty:
            log.warning("%s: ingen data, hoppar över", key)
            continue
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
    log.info("Klart: %d områden skrivna till %s", len(summary_zones), OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
