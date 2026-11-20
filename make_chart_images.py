#!/usr/bin/env python3
"""Skapar bilder av dashboardens diagram så att de kan visas på bouncyenergy.com.

Läser docs/data/solar_bess.json (skapas av update_data.py) och skriver
  docs/img/capture-rate.png   (2x, för webben)
  docs/img/capture-rate.svg
Bilderna visar samma siffror som fliken "Solar park + BESS" (ENTSO-E reported,
PVGIS-profil om ett område saknar tillräcklig rapportering) och uppdateras vid
varje nattlig körning. Datumet i bildens fotrad är datumet för senaste datakörning.

Körs efter update_data.py:   python make_chart_images.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data" / "solar_bess.json"
OUT = ROOT / "docs" / "img"

ZONES = ["SE1", "SE2", "SE3", "SE4"]
SCENARIOS = [("none", "Without BESS", "#b9c3c8"),
             ("1h", "1 h BESS", "#6fa8bd"),
             ("2h", "2 h BESS", "#2f7a96"),
             ("4h", "4 h BESS", "#142228")]
INK = "#142228"
MUTED = "#5b6a70"
GRID = "#e3e7e9"


def zone_entry(zone: dict) -> dict:
    """Samma val som dashboarden: ENTSO-E-rapporterad profil om den finns, annars PVGIS."""
    return zone.get("reported") or zone


def pick_font() -> str:
    """Första tillgängliga typsnittet i listan (Inter är sidans brödtextfont)."""
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Inter", "Liberation Sans", "Arial", "Helvetica", "DejaVu Sans"):
        if name in have:
            return name
    return "sans-serif"


def main() -> int:
    if not DATA.exists():
        print(f"Hittar inte {DATA}. Kör update_data.py först.", file=sys.stderr)
        return 1
    d = json.loads(DATA.read_text(encoding="utf-8"))
    updated = (d.get("generated") or "")[:10]

    plt.rcParams.update({
        "font.family": pick_font(),
        "svg.fonttype": "path",
    })
    fig, ax = plt.subplots(figsize=(10.2, 3.5), dpi=200)
    fig.patch.set_facecolor("white")

    width = 0.19
    gap = 0.015
    for zi, key in enumerate(ZONES):
        entry = zone_entry(d["zones"][key])
        for si, (scn, _label, color) in enumerate(SCENARIOS):
            cr = entry["total"][scn]["cr"] * 100
            x = zi + (si - 1.5) * (width + gap)
            ax.bar(x, cr, width=width, color=color, zorder=3)
            ax.text(x, cr + 2.5, f"{cr:.0f} %", ha="center", va="bottom",
                    fontsize=6.6, color="#1f3a4a", zorder=4)

    ax.set_xticks(range(len(ZONES)))
    ax.set_xticklabels([f"{k}\navg {zone_entry(d['zones'][k])['base']:.0f} €" for k in ZONES],
                       fontsize=8.5, color=INK, linespacing=1.4)
    ax.set_ylim(0, 155)
    ax.set_yticks([0, 50, 100, 150])
    ax.set_yticklabels(["0 %", "50 %", "100 %", "150 %"], fontsize=8, color=MUTED)
    ax.tick_params(axis="both", length=0, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c9d0d3")
    ax.set_xlim(-0.55, len(ZONES) - 0.45)

    handles = [Patch(facecolor=c, label=lbl) for _, lbl, c in SCENARIOS]
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.02), ncol=4,
              frameon=False, fontsize=8.5, handlelength=1.1, handleheight=1.1,
              columnspacing=1.6, labelcolor=INK)

    footer = "Solar capture rate by price zone, last 12 months"
    if updated:
        footer += f" · Updated {updated}"
    footer += " · Source: ENTSO-E, PVGIS · Illustrative model, not a forecast"
    fig.text(0.012, 0.012, footer, fontsize=6.8, color=MUTED, ha="left", va="bottom")

    fig.subplots_adjust(left=0.07, right=0.995, top=0.88, bottom=0.2)

    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "capture-rate.png", facecolor="white")
    fig.savefig(OUT / "capture-rate.svg", facecolor="white")
    print("Skrev", OUT / "capture-rate.png", "och .svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
