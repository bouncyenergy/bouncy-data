# Bouncy Energy – marknadsdata

> **Ny här? Börja med KOM-IGANG.md.**

Statisk dashboard med capture rates (vind, sol), prisspreadar och produktion för SE1–SE4, Finland, Danmark (DK1, DK2) och Tyskland.
Data hämtas från ENTSO-E varje natt av ett Python-skript. Webbsidan är bara HTML + JSON, utan server.

```
update_data.py            hämtar och räknar, skriver docs/data/*.json
docs/index.html           dashboarden (Plotly.js)
docs/data/                summary.json, recent.json  (innehåller demodata tills första riktiga körningen)
data/raw/                 cache med timdata per område (skapas av skriptet)
.github/workflows/        nattlig körning + publicering
```

## 1. Prova lokalt (utan token)

```bash
pip install -r requirements.txt
python update_data.py --demo
python -m http.server -d docs 8000      # öppna http://localhost:8000
```

## 2. Riktig data lokalt

```bash
export ENTSOE_API_KEY="din-token"       # Windows PowerShell: $env:ENTSOE_API_KEY="din-token"
python update_data.py                   # första körningen hämtar historik från 2024-01-01 (ca 10 min)
python update_data.py --zones SE3,SE4   # bara vissa områden
python update_data.py --start 2023-01-01  # längre historik (gäller bara vid tom cache)
```

Därefter hämtas bara nya dagar. De senaste 7 dagarna hämtas om, eftersom produktionsdata revideras.

## 3. Publicera automatiskt (GitHub Pages)

1. Skapa ett privat eller publikt repo på GitHub och ladda upp hela mappen.
2. *Settings → Secrets and variables → Actions → New repository secret*: namn `ENTSOE_API_KEY`, värde din token.
3. *Settings → Pages → Build and deployment → Source: GitHub Actions*.
4. *Actions → Uppdatera data och publicera → Run workflow*. Första körningen tar ett tag.
5. Sidan ligger sedan på `https://DITT-ANVÄNDARNAMN.github.io/REPONAMN/` och uppdateras varje dag.

Token ligger bara som secret hos GitHub, aldrig i sidans filer.

## 4. Lägg in på Bouncyenergy.com (one.com Website Builder)

Skapa en undersida (t.ex. "Marknadsdata"), lägg till ett inbäddnings-/HTML-block och klistra in:

```html
<iframe src="https://DITT-ANVÄNDARNAMN.github.io/REPONAMN/"
        style="width:100%;height:1100px;border:0" loading="lazy"
        title="Marknadsdata från ENTSO-E"></iframe>
```

Om blocket saknas eller iframe krånglar: peka en subdomän (t.ex. `data.bouncyenergy.com`) mot GitHub Pages.
I one.coms DNS-inställningar: CNAME `data` → `DITT-ANVÄNDARNAMN.github.io`, och fyll i samma domän under *Settings → Pages → Custom domain*.
Länka sedan till subdomänen från menyn.

Direktlänkar: `?zone=SE4&tab=spread` (flikar: `capture`, `spread`, `recent`).

## Anpassa

* Färger och typsnitt: variablerna överst i `docs/index.html`.
* Elområden: `ZONES` överst i `update_data.py`.
* Ytterligare tekniker (t.ex. vattenkraft): lägg till kolumn i `pick_generation` och i `compute_zone`.

## Att känna till

* Svensk solproduktion rapporteras ofta ofullständigt till ENTSO-E. Månader med mindre än 80 % täckning visas som tomma (`MIN_COVERAGE`).
* Priser är i EUR/MWh. SEK kräver valutakurs och är inte inbyggt.
* Kvartspriser (från okt 2025) slås ihop till timmar för jämförbarhet.
* Källhänvisning till ENTSO-E ligger i sidfoten. Kolla deras användarvillkor om du publicerar offentligt.
