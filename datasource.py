"""Live data fetchers with a bundled snapshot fallback.

Sources
-------
* Industry unlevered betas (US): Damodaran, betas.xls, sheet "Industry Averages", column "Unlevered beta"
* BBB default spread: Damodaran, ratings.html, "Baa2/BBB" row of the large non-financial firm table
* Corporate tax rates: OECD Corporate Tax Statistics database (the data behind the
  "Statutory corporate income tax rates" chapter), SDMX API, combined (central + sub-central)
  statutory rate, latest year available per jurisdiction
* 10-year government bond yields: Trading Economics /bonds (HTML scrape, no API key)

Each ``fetch_*`` function returns a :class:`FetchResult`. If the live request or parsing fails,
the result falls back to ``data/snapshot.json`` and records the error so the app can say so.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests
from bs4 import BeautifulSoup

BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html"
OECD_PAGE_URL = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/full-report/"
    "statutory-corporate-income-tax-rates_440f9286.html#chapter-d1e2342-08e74a33fb"
)
OECD_API_URL = (
    "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,/all"
    "?startPeriod={start}&format=csvfilewithlabels"
)
TE_BONDS_URL = "https://tradingeconomics.com/bonds"

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "snapshot.json"
TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Trading Economics name -> OECD name, where they differ
TE_TO_OECD = {
    "South Korea": "Korea",
    "Turkey": "Türkiye",
    "Czech Republic": "Czechia",
    "Slovakia": "Slovak Republic",
    "China": "China (People’s Republic of)",
    "Hong Kong": "Hong Kong (China)",
    "Vietnam": "Viet Nam",
}

CURRENCY = {
    "United States": "USD", "United Kingdom": "GBP", "Japan": "JPY", "Australia": "AUD",
    "Germany": "EUR", "Brazil": "BRL", "Russia": "RUB", "India": "INR", "Canada": "CAD",
    "Italy": "EUR", "France": "EUR", "South Africa": "ZAR", "China": "CNY", "Switzerland": "CHF",
    "Mexico": "MXN", "Netherlands": "EUR", "New Zealand": "NZD", "Portugal": "EUR",
    "South Korea": "KRW", "Spain": "EUR", "Greece": "EUR", "Turkey": "TRY", "Finland": "EUR",
    "Lithuania": "EUR", "Norway": "NOK", "Poland": "PLN", "Romania": "RON", "Slovakia": "EUR",
    "Slovenia": "EUR", "Sweden": "SEK", "Hungary": "HUF", "Iceland": "ISK", "Ireland": "EUR",
    "Austria": "EUR", "Belgium": "EUR", "Croatia": "EUR", "Czech Republic": "CZK",
    "Denmark": "DKK", "Peru": "PEN", "Chile": "CLP", "Colombia": "COP", "Singapore": "SGD",
    "Taiwan": "TWD", "Thailand": "THB", "Vietnam": "VND", "Hong Kong": "HKD",
    "Indonesia": "IDR", "Israel": "ILS", "Malaysia": "MYR", "Pakistan": "PKR",
    "Philippines": "PHP", "Zambia": "ZMW", "Kenya": "KES", "Nigeria": "NGN",
}


@dataclass
class FetchResult:
    name: str
    data: Any
    source_url: str
    as_of: str
    status: str  # "live" or "snapshot"
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    error: str | None = None


# --------------------------------------------------------------------------------------
# Parsers (pure functions; unit-tested offline)
# --------------------------------------------------------------------------------------
def _excel_date(value: Any) -> str:
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) and value > 20000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).strftime("%Y-%m-%d")
    return str(value)


def parse_betas_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Parse the 'Industry Averages' sheet read with header=None."""
    as_of = "unknown"
    header_row = None
    for i in range(min(len(raw), 40)):
        first = str(raw.iat[i, 0]).strip().lower()
        if first.startswith("date updated"):
            as_of = _excel_date(raw.iat[i, 1])
        if first == "industry name":
            header_row = i
            break
    if header_row is None:
        raise ValueError("Could not find the 'Industry Name' header row in betas.xls")

    headers = [str(h).strip().lower() for h in raw.iloc[header_row]]

    def col(label: str) -> int:
        if label not in headers:
            raise ValueError(f"Column '{label}' not found in betas.xls")
        return headers.index(label)

    body = raw.iloc[header_row + 1 :]
    df = pd.DataFrame(
        {
            "industry": body.iloc[:, 0].astype(str).str.strip(),
            "n_firms": pd.to_numeric(body.iloc[:, col("number of firms")], errors="coerce"),
            "beta": pd.to_numeric(body.iloc[:, col("beta")], errors="coerce"),
            "de_ratio": pd.to_numeric(body.iloc[:, col("d/e ratio")], errors="coerce"),
            "eff_tax": pd.to_numeric(body.iloc[:, col("effective tax rate")], errors="coerce"),
            "unlevered_beta": pd.to_numeric(body.iloc[:, col("unlevered beta")], errors="coerce"),
            "unlevered_beta_cash_adj": pd.to_numeric(
                body.iloc[:, col("unlevered beta corrected for cash")], errors="coerce"
            ),
        }
    )
    df = df[(df["industry"] != "") & (df["industry"] != "nan") & df["unlevered_beta"].notna()]
    if len(df) < 50:
        raise ValueError(f"Only {len(df)} industries parsed from betas.xls")
    return df.reset_index(drop=True), as_of


def parse_ratings_html(html: str) -> tuple[float, str]:
    """Return (Baa2/BBB spread as decimal, date text) for large non-financial firms.

    The large non-financial table is the left half of each row, so the first 'Baa2/BBB'
    cell in document order belongs to it; the spread is the next cell.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Date of Analysis:\s*(.+?)(?:\s{2,}|If you want|$)", text)
    as_of = m.group(1).strip() if m else "unknown"
    as_of = re.sub(r"^Data used is as of\s*", "", as_of)

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        for j, c in enumerate(cells):
            if c.replace(" ", "") == "Baa2/BBB" and j + 1 < len(cells):
                spread = float(cells[j + 1].replace("%", "").strip()) / 100
                if not 0 < spread < 0.2:
                    raise ValueError(f"Implausible BBB spread {spread}")
                return spread, as_of
    raise ValueError("Baa2/BBB row not found in ratings.html")


def parse_oecd_csv(text: str) -> pd.DataFrame:
    """Latest combined statutory CIT rate per jurisdiction from the SDMX csvfilewithlabels output."""
    df = pd.read_csv(io.StringIO(text))
    df = df[(df["MEASURE"] == "CIT_C") & (df["TARGETING"] == "ST")]
    df = df[pd.to_numeric(df["OBS_VALUE"], errors="coerce").notna()].copy()
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"])
    df["TIME_PERIOD"] = pd.to_numeric(df["TIME_PERIOD"])
    df = df.sort_values("TIME_PERIOD").groupby("REF_AREA", as_index=False).last()
    out = pd.DataFrame(
        {
            "code": df["REF_AREA"],
            "country": df["Reference area"],
            "year": df["TIME_PERIOD"].astype(int),
            "rate": df["OBS_VALUE"] / 100,
        }
    )
    if "USA" not in set(out["code"]):
        raise ValueError("United States missing from OECD response")
    return out.sort_values("country").reset_index(drop=True)


def _te_date(label: str, today: date) -> str:
    """Trading Economics shows 'Nov/13' (no year). Infer the year relative to today."""
    try:
        d = datetime.strptime(f"{label.strip()}/{today.year}", "%b/%d/%Y").date()
    except ValueError:
        return label.strip()
    if d > today + timedelta(days=2):
        d = d.replace(year=today.year - 1)
    return d.isoformat()


def parse_te_bonds_html(html: str, today: date | None = None) -> pd.DataFrame:
    """Parse the 10-year yield tables on tradingeconomics.com/bonds."""
    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("tr[data-symbol]"):
        name_td = tr.select_one("td.datatable-item-first")
        yld_td = tr.find("td", id="p")
        date_td = tr.find("td", id="date")
        if not (name_td and yld_td):
            continue
        name = name_td.get_text(" ", strip=True)
        try:
            y = float(yld_td.get_text(strip=True).replace(",", ""))
        except ValueError:
            continue
        rows.append(
            {
                "country": name,
                "yield": y / 100,
                "date": _te_date(date_td.get_text(strip=True), today) if date_td else "",
                "symbol": tr.get("data-symbol", ""),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty or "United States" not in set(df["country"]):
        raise ValueError("United States 10Y yield not found on Trading Economics page")
    df = df[df["country"] != "Euro area"].drop_duplicates("country")
    return df.sort_values("country").reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------------------
def _snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _with_fallback(name: str, url: str, live: Callable[[], tuple[Any, str]], snap: Callable[[dict], tuple[Any, str]]) -> FetchResult:
    try:
        data, as_of = live()
        return FetchResult(name, data, url, as_of, "live")
    except Exception as exc:  # noqa: BLE001 - any failure -> fallback
        data, as_of = snap(_snapshot())
        return FetchResult(name, data, url, as_of, "snapshot", error=f"{type(exc).__name__}: {exc}"[:300])


def fetch_betas() -> FetchResult:
    def live():
        raw = pd.read_excel(io.BytesIO(_get(BETAS_URL).content), sheet_name="Industry Averages", header=None, engine="xlrd")
        return parse_betas_frame(raw)

    return _with_fallback(
        "Industry unlevered betas (US)", BETAS_URL, live,
        lambda s: (pd.DataFrame(s["betas"]["rows"]), s["betas"]["as_of"]),
    )


def fetch_bbb_spread() -> FetchResult:
    def live():
        r = _get(RATINGS_URL)
        r.encoding = r.encoding or "utf-8"
        return parse_ratings_html(r.text)

    return _with_fallback(
        "USD BBB (Baa2) default spread", RATINGS_URL, live,
        lambda s: (s["bbb_spread"]["value"], s["bbb_spread"]["as_of"]),
    )


def fetch_tax_rates() -> FetchResult:
    def live():
        start = datetime.now(timezone.utc).year - 4
        df = parse_oecd_csv(_get(OECD_API_URL.format(start=start)).text)
        years = df["year"].value_counts()
        return df, f"OECD Corporate Tax Statistics, latest year per jurisdiction (mostly {years.idxmax()})"

    def snap(s):
        return pd.DataFrame(s["tax"]["rows"]), s["tax"]["as_of"]

    res = _with_fallback("Corporate tax rates (combined statutory)", OECD_PAGE_URL, live, snap)
    return res


def fetch_bond_yields() -> FetchResult:
    def live():
        df = parse_te_bonds_html(_get(TE_BONDS_URL).text)
        us_date = df.loc[df["country"] == "United States", "date"].iloc[0]
        return df, f"Trading Economics quote date {us_date} (per-country dates in table)"

    def snap(s):
        df = pd.DataFrame(s["yields"]["rows"]).rename(columns={"date_label": "date"})
        df["date"] = df["date"].map(lambda x: _te_date(x, date(2026, 11, 13)))
        return df, s["yields"]["as_of"]

    return _with_fallback("10-year government bond yields", TE_BONDS_URL, live, snap)


def fetch_all() -> dict[str, FetchResult]:
    return {
        "betas": fetch_betas(),
        "bbb": fetch_bbb_spread(),
        "tax": fetch_tax_rates(),
        "yields": fetch_bond_yields(),
    }


def build_country_table(yields: pd.DataFrame, tax: pd.DataFrame) -> pd.DataFrame:
    """One row per country with a 10Y yield, joined to its OECD tax rate (may be missing)."""
    tax_by_name = {str(n).replace("’", "'"): (r, y) for n, r, y in zip(tax["country"], tax["rate"], tax["year"])}
    rows = []
    for _, row in yields.iterrows():
        oecd_name = TE_TO_OECD.get(row["country"], row["country"]).replace("’", "'")
        rate, year = tax_by_name.get(oecd_name, (None, None))
        rows.append(
            {
                "country": row["country"],
                "currency": CURRENCY.get(row["country"], "LCY"),
                "yield_10y": row["yield"],
                "yield_date": row.get("date", ""),
                "tax_rate": rate,
                "tax_year": year,
            }
        )
    return pd.DataFrame(rows).sort_values("country").reset_index(drop=True)
