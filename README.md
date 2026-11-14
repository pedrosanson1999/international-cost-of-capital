# international-cost-of-capital

Streamlit app that compares a project's cost of equity, cost of debt and WACC in **USD** and in the
**currency of a selected country**.

## Data sources (fetched live, with a Refresh button)
| Data | Source |
|---|---|
| U.S. industry unlevered betas | Damodaran `betas.xls` → "Industry Averages" → *Unlevered beta* |
| USD BBB spread | Damodaran `ratings.html` → Baa2/BBB, large non-financial firms |
| Corporate tax rates | OECD Corporate Tax Statistics (combined statutory CIT rate, via the OECD SDMX API) |
| 10-year government yields | Trading Economics `/bonds` (HTML scrape, no API key) |

If a live fetch fails (e.g. a site blocks the server), the app falls back to `data/snapshot.json`
and shows a warning. Every fetched value can be overridden manually in the sidebar.

## Formulas
- β<sub>E</sub> = β<sub>U</sub> [1 + (1 − τ) D/E]
- R<sub>e</sub> = R<sub>f</sub> + β<sub>E</sub> × ERP;  R<sub>d</sub> = R<sub>f</sub> + BBB spread
- (1 + R*) = (1 + R)(1 + R<sub>f</sub>*)/(1 + R<sub>f</sub>) for equity and (auto) debt
- WACC = (1 − d) R<sub>e</sub> + d R<sub>d</sub> (1 − τ)

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
pytest          # checks the slide example: Re = 10.6486%, WACC = 8.5265%
```

## Deploy
Push this repo to GitHub, then on https://share.streamlit.io create an app pointing to `app.py`.
