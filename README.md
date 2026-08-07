# Westbridge Energy Market Dashboard (ENTSO-E)

Static dashboard for German/Dutch/Belgian/Luxembourg electricity demand, generation mix
and cross-border flows. A GitHub Actions workflow fetches fresh data from the ENTSO-E
Transparency Platform on a schedule and commits it into `data/*.json`; `index.html`
reads those files and renders the charts.

## Setup

1. Add your ENTSO-E API token as a repo secret named `ENTSOE_TOKEN`
   (Settings → Secrets and variables → Actions → New repository secret).
2. Enable GitHub Pages (Settings → Pages → Deploy from branch → main → / root).
3. Go to the Actions tab → "Update ENTSO-E dashboard data" → Run workflow (manual first run).
4. Open the Pages URL shown in Settings → Pages to view the dashboard.

**Important:** open the dashboard via the GitHub Pages URL, not by double-clicking
`index.html` on your computer — local files can't fetch other local JSON files
due to browser security restrictions.

## Adjusting

- Change refresh frequency: edit the `cron` line in `.github/workflows/update-data.yml`.
- Change history start date: edit `START_DATE` in `scripts/fetch_entsoe.py`.
