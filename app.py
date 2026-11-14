"""International Cost of Capital App - single-file Streamlit app.

Compares a project's cost of equity, cost of debt and WACC in USD and in the currency of a
selected country.

Run locally:   pip install -r requirements.txt   then   streamlit run app.py

Sections
  1. Calculations        (relevering, CAPM, parity conversion, WACC)
  2. Data sources        (Damodaran, OECD, Trading Economics fetchers + parsers)
  3. Fallback snapshot   (used only when a live fetch fails)
  4. Streamlit app       (inputs, results, comparison, downloads)

Data sources
  * Industry unlevered betas (US): Damodaran betas.xls, sheet "Industry Averages", column "Unlevered beta"
  * BBB default spread: Damodaran ratings.html, "Baa2/BBB" row for large non-financial firms
  * Corporate tax rates: OECD Corporate Tax Statistics (combined statutory CIT rate, via the OECD SDMX API)
  * 10-year government bond yields: Trading Economics /bonds (HTML scrape, no API key)

Formulas (from class slides; rates are decimals)
  beta_E = beta_U * [1 + (1 - tau) * D/E],   D/E = d / (1 - d)
  R_e = R_f + beta_E * ERP;   R_d = R_f + BBB spread
  (1 + R_e*) = (1 + R_e) * (1 + R_f*) / (1 + R_f)      (same conversion for auto foreign R_d*)
  WACC = (1 - d) * R_e + d * R_d * (1 - tau)
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import altair as alt
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


# ======================================================================================
# 1. CALCULATIONS
# ======================================================================================
def levered_beta(beta_u: float, debt_share: float, tax_rate: float) -> float:
    """beta_E = beta_U * [1 + (1 - tau) D/E] with D/E = d / (1 - d)."""
    if not 0 <= debt_share < 1:
        raise ValueError("Debt share must be in [0, 1).")
    de = debt_share / (1 - debt_share)
    return beta_u * (1 + (1 - tax_rate) * de)


def unlevered_beta(beta_e: float, debt_share: float, tax_rate: float) -> float:
    """Inverse of levered_beta: beta_U = beta_E / [1 + (1 - tau) D/E]."""
    de = debt_share / (1 - debt_share)
    return beta_e / (1 + (1 - tax_rate) * de)


def capm(rf: float, beta_e: float, erp: float) -> float:
    return rf + beta_e * erp


def convert_rate(r_domestic: float, rf_domestic: float, rf_foreign: float) -> float:
    """(1 + R*) = (1 + R) (1 + Rf*) / (1 + Rf)."""
    return (1 + r_domestic) * (1 + rf_foreign) / (1 + rf_domestic) - 1


def implied_foreign_spread(spread_usd: float, rf_usd: float, rf_foreign: float) -> float:
    """Spread over the foreign risk-free rate implied by converting the USD debt cost."""
    rd_foreign = convert_rate(rf_usd + spread_usd, rf_usd, rf_foreign)
    return rd_foreign - rf_foreign


def wacc(re: float, rd: float, debt_share: float, tax_rate: float) -> float:
    return (1 - debt_share) * re + debt_share * rd * (1 - tax_rate)


@dataclass
class CurrencyResult:
    currency: str
    risk_free: float
    beta_e: float
    cost_of_equity: float
    spread: float
    pretax_cost_of_debt: float
    aftertax_cost_of_debt: float
    equity_contribution: float
    debt_contribution: float
    wacc: float


@dataclass
class ProjectResult:
    beta_u: float
    beta_e: float
    tax_rate: float
    debt_share: float
    erp: float
    usd: CurrencyResult
    foreign: CurrencyResult
    foreign_spread_is_manual: bool
    wacc_foreign_direct: float  # USD WACC converted directly via parity (cross-check)

    def to_dict(self) -> dict:
        return asdict(self)


def compute_project(
    *,
    beta_u: float,
    tax_rate: float,
    debt_share: float,
    erp: float,
    rf_usd: float,
    spread_usd: float,
    rf_foreign: float,
    foreign_currency: str = "FCY",
    spread_foreign_manual: float | None = None,
) -> ProjectResult:
    """Compute cost of equity, cost of debt and WACC in USD and a foreign currency.

    If ``spread_foreign_manual`` is None, the foreign pre-tax cost of debt is the USD
    cost of debt converted with the parity formula; otherwise R_d* = R_f* + manual spread.
    """
    d, tau = debt_share, tax_rate
    beta_e = levered_beta(beta_u, d, tau)

    # USD
    re_usd = capm(rf_usd, beta_e, erp)
    rd_usd = rf_usd + spread_usd
    usd = CurrencyResult(
        currency="USD",
        risk_free=rf_usd,
        beta_e=beta_e,
        cost_of_equity=re_usd,
        spread=spread_usd,
        pretax_cost_of_debt=rd_usd,
        aftertax_cost_of_debt=rd_usd * (1 - tau),
        equity_contribution=(1 - d) * re_usd,
        debt_contribution=d * rd_usd * (1 - tau),
        wacc=wacc(re_usd, rd_usd, d, tau),
    )

    # Foreign currency
    re_fx = convert_rate(re_usd, rf_usd, rf_foreign)
    if spread_foreign_manual is None:
        rd_fx = convert_rate(rd_usd, rf_usd, rf_foreign)
    else:
        rd_fx = rf_foreign + spread_foreign_manual
    foreign = CurrencyResult(
        currency=foreign_currency,
        risk_free=rf_foreign,
        beta_e=beta_e,
        cost_of_equity=re_fx,
        spread=rd_fx - rf_foreign,
        pretax_cost_of_debt=rd_fx,
        aftertax_cost_of_debt=rd_fx * (1 - tau),
        equity_contribution=(1 - d) * re_fx,
        debt_contribution=d * rd_fx * (1 - tau),
        wacc=wacc(re_fx, rd_fx, d, tau),
    )

    return ProjectResult(
        beta_u=beta_u,
        beta_e=beta_e,
        tax_rate=tau,
        debt_share=d,
        erp=erp,
        usd=usd,
        foreign=foreign,
        foreign_spread_is_manual=spread_foreign_manual is not None,
        wacc_foreign_direct=convert_rate(usd.wacc, rf_usd, rf_foreign),
    )

# ======================================================================================
# 2. DATA SOURCES
# ======================================================================================
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

TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tradingeconomics.com/",
    "Upgrade-Insecure-Requests": "1",
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
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True).replace("\xa0", " "))
    m = re.search(r"as of\s+([A-Z][a-z]+\.?\s+\d{4})", text) or re.search(
        r"Date\s*of\s*Analysis\s*:?\s*(?:Data used is as of\s*)?([A-Za-z]+\.?\s+\d{4})", text, re.I
    )
    as_of = m.group(1).strip() if m else "date not shown on page"

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


TE_NAME_ALIASES = {"US": "United States", "U.S.": "United States", "UK": "United Kingdom",
                   "Korea": "South Korea", "Czechia": "Czech Republic"}
_TE_DATE = re.compile(r"^[A-Z][a-z]{2}/\d{1,2}$")


def _te_clean_name(name: str) -> str:
    name = re.sub(r"\s+10Y$", "", name.strip())
    return TE_NAME_ALIASES.get(name, name)


def _num(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "").replace("%", ""))
    except ValueError:
        return None


def parse_te_bonds_html(html: str, today: date | None = None) -> pd.DataFrame:
    """Parse the 10-year yield tables on tradingeconomics.com/bonds.

    Strategy 1: rows tagged data-symbol with td#p (yield) and td#date (normal page layout).
    Strategy 2: any table row containing a link to /<country>/government-bond-yield; the yield is the
    first numeric cell after the country cell and the date is the cell that looks like 'Nov/13'.
    """
    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    for tr in soup.select("tr[data-symbol]"):
        name_td = tr.select_one("td.datatable-item-first")
        yld_td = tr.find("td", id="p")
        date_td = tr.find("td", id="date")
        y = _num(yld_td.get_text(strip=True)) if yld_td else None
        if name_td is None or y is None:
            continue
        rows.append({"country": _te_clean_name(name_td.get_text(" ", strip=True)), "yield": y / 100,
                     "date": _te_date(date_td.get_text(strip=True), today) if date_td else ""})

    if not rows:  # strategy 2
        for tr in soup.find_all("tr"):
            link = tr.find("a", href=re.compile(r"/government-bond-yield/?$"))
            if link is None:
                continue
            cells = tr.find_all("td")
            texts = [c.get_text(" ", strip=True) for c in cells]
            try:
                i = next(k for k, c in enumerate(cells) if link in c.find_all("a"))
            except StopIteration:
                continue
            y = next((v for v in (_num(t) for t in texts[i + 1:]) if v is not None), None)
            if y is None:
                continue
            d = next((t for t in texts if _TE_DATE.match(t)), "")
            rows.append({"country": _te_clean_name(link.get_text(" ", strip=True)), "yield": y / 100,
                         "date": _te_date(d, today) if d else ""})

    df = pd.DataFrame(rows)
    if df.empty or "United States" not in set(df["country"]):
        title = soup.title.get_text(strip=True)[:80] if soup.title else "no title"
        raise ValueError(
            f"United States 10Y yield not found on Trading Economics page "
            f"(page title: '{title}', {len(html):,} characters, {len(rows)} rows parsed)"
        )
    df = df[df["country"] != "Euro area"].drop_duplicates("country")
    return df.sort_values("country").reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------------------
def _snapshot() -> dict:
    return SNAPSHOT


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


TE_COUNTRY_URL = "https://tradingeconomics.com/{slug}/government-bond-yield"


def te_slug(country: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", country.lower()).strip("-")


def parse_te_country_html(html: str) -> tuple[float, str]:
    """Yield (decimal) and quote date from a single-country page, e.g. /thailand/government-bond-yield.

    Uses the chart metadata ("last": 1.995) for full precision, else the quote cell (td#p);
    the date comes from the page description ("... rose to 2.00% on November 13, 2026 ...").
    """
    y = None
    m = re.search(r"TEChartsMeta\s*=\s*\[\s*\{.*?\"last\"\s*:\s*(-?[\d.]+)", html, re.S)
    if m:
        y = float(m.group(1))
    else:
        soup = BeautifulSoup(html, "html.parser")
        p = soup.find("td", id="p")
        y = _num(p.get_text(strip=True)) if p else None
    if y is None:
        raise ValueError("Yield not found on Trading Economics country page")
    d = re.search(r"\bon ([A-Z][a-z]+ \d{1,2}, \d{4})", html)
    as_of = datetime.strptime(d.group(1), "%B %d, %Y").date().isoformat() if d else "date not shown"
    return y / 100, as_of


def fetch_country_yield(country: str) -> FetchResult:
    """Live 10Y yield for one country from its own page (used when the /bonds table is blocked)."""
    url = TE_COUNTRY_URL.format(slug=te_slug(country))
    try:
        y, as_of = parse_te_country_html(_get(url).text)
        return FetchResult(f"{country} 10Y yield", {"yield": y, "date": as_of}, url, as_of, "live")
    except Exception as exc:  # noqa: BLE001
        return FetchResult(f"{country} 10Y yield", None, url, "", "failed", error=f"{type(exc).__name__}: {exc}"[:300])


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

# ======================================================================================
# 3. FALLBACK SNAPSHOT (values read from the live sources on 2026-11-12/13)
# ======================================================================================
SNAPSHOT = {
    "betas": {
        "as_of": "2026-01-05",
        "rows": [
            {"industry": "Advertising", "n_firms": 52.0, "beta": 1.2105, "de_ratio": 0.402, "eff_tax": 0.0502, "unlevered_beta": 0.9301, "unlevered_beta_cash_adj": 1.008},
            {"industry": "Aerospace/Defense", "n_firms": 79.0, "beta": 0.9455, "de_ratio": 0.1556, "eff_tax": 0.1158, "unlevered_beta": 0.8467, "unlevered_beta_cash_adj": 0.8694},
            {"industry": "Air Transport", "n_firms": 23.0, "beta": 1.1855, "de_ratio": 0.9117, "eff_tax": 0.0829, "unlevered_beta": 0.7041, "unlevered_beta_cash_adj": 0.7579},
            {"industry": "Apparel", "n_firms": 35.0, "beta": 0.9359, "de_ratio": 0.3129, "eff_tax": 0.0961, "unlevered_beta": 0.758, "unlevered_beta_cash_adj": 0.7946},
            {"industry": "Auto & Truck", "n_firms": 33.0, "beta": 1.4565, "de_ratio": 0.197, "eff_tax": 0.0374, "unlevered_beta": 1.269, "unlevered_beta_cash_adj": 1.3082},
            {"industry": "Auto Parts", "n_firms": 35.0, "beta": 1.3394, "de_ratio": 0.4146, "eff_tax": 0.15, "unlevered_beta": 1.0217, "unlevered_beta_cash_adj": 1.1283},
            {"industry": "Bank (Money Center)", "n_firms": 15.0, "beta": 0.761, "de_ratio": 1.6419, "eff_tax": 0.1843, "unlevered_beta": 0.3411, "unlevered_beta_cash_adj": 0.4439},
            {"industry": "Banks (Regional)", "n_firms": 568.0, "beta": 0.3985, "de_ratio": 0.521, "eff_tax": 0.1761, "unlevered_beta": 0.2865, "unlevered_beta_cash_adj": 0.3744},
            {"industry": "Beverage (Alcoholic)", "n_firms": 14.0, "beta": 0.8125, "de_ratio": 0.4334, "eff_tax": 0.1235, "unlevered_beta": 0.6132, "unlevered_beta_cash_adj": 0.628},
            {"industry": "Beverage (Soft)", "n_firms": 27.0, "beta": 0.6415, "de_ratio": 0.2059, "eff_tax": 0.0685, "unlevered_beta": 0.5557, "unlevered_beta_cash_adj": 0.5755},
            {"industry": "Broadcasting", "n_firms": 24.0, "beta": 0.4708, "de_ratio": 0.8585, "eff_tax": 0.0773, "unlevered_beta": 0.2864, "unlevered_beta_cash_adj": 0.3153},
            {"industry": "Brokerage & Investment Banking", "n_firms": 32.0, "beta": 1.1715, "de_ratio": 1.3557, "eff_tax": 0.1527, "unlevered_beta": 0.5808, "unlevered_beta_cash_adj": 0.6794},
            {"industry": "Building Materials", "n_firms": 41.0, "beta": 1.1115, "de_ratio": 0.26, "eff_tax": 0.1802, "unlevered_beta": 0.9302, "unlevered_beta_cash_adj": 0.9605},
            {"industry": "Business & Consumer Services", "n_firms": 155.0, "beta": 0.888, "de_ratio": 0.1972, "eff_tax": 0.1038, "unlevered_beta": 0.7736, "unlevered_beta_cash_adj": 0.806},
            {"industry": "Cable TV", "n_firms": 9.0, "beta": 0.7407, "de_ratio": 1.4694, "eff_tax": 0.1064, "unlevered_beta": 0.3524, "unlevered_beta_cash_adj": 0.3629},
            {"industry": "Chemical (Basic)", "n_firms": 29.0, "beta": 1.0122, "de_ratio": 0.9935, "eff_tax": 0.0768, "unlevered_beta": 0.58, "unlevered_beta_cash_adj": 0.6368},
            {"industry": "Chemical (Diversified)", "n_firms": 4.0, "beta": 0.8507, "de_ratio": 1.7611, "eff_tax": 0.0, "unlevered_beta": 0.3666, "unlevered_beta_cash_adj": 0.4061},
            {"industry": "Chemical (Specialty)", "n_firms": 59.0, "beta": 0.9698, "de_ratio": 0.2988, "eff_tax": 0.1368, "unlevered_beta": 0.7922, "unlevered_beta_cash_adj": 0.8245},
            {"industry": "Coal & Related Energy", "n_firms": 16.0, "beta": 1.071, "de_ratio": 0.0714, "eff_tax": 0.0313, "unlevered_beta": 1.0165, "unlevered_beta_cash_adj": 1.1824},
            {"industry": "Computer Services", "n_firms": 64.0, "beta": 1.0879, "de_ratio": 0.251, "eff_tax": 0.1053, "unlevered_beta": 0.9155, "unlevered_beta_cash_adj": 0.9617},
            {"industry": "Computers/Peripherals", "n_firms": 36.0, "beta": 1.3503, "de_ratio": 0.0462, "eff_tax": 0.0591, "unlevered_beta": 1.3051, "unlevered_beta_cash_adj": 1.3246},
            {"industry": "Construction Supplies", "n_firms": 40.0, "beta": 1.1504, "de_ratio": 0.1762, "eff_tax": 0.1604, "unlevered_beta": 1.0161, "unlevered_beta_cash_adj": 1.0468},
            {"industry": "Diversified", "n_firms": 20.0, "beta": 0.881, "de_ratio": 0.1555, "eff_tax": 0.0276, "unlevered_beta": 0.7889, "unlevered_beta_cash_adj": 0.8431},
            {"industry": "Drugs (Biotechnology)", "n_firms": 496.0, "beta": 1.1353, "de_ratio": 0.1304, "eff_tax": 0.0108, "unlevered_beta": 1.0341, "unlevered_beta_cash_adj": 1.0795},
            {"industry": "Drugs (Pharmaceutical)", "n_firms": 228.0, "beta": 0.9828, "de_ratio": 0.1454, "eff_tax": 0.0299, "unlevered_beta": 0.8862, "unlevered_beta_cash_adj": 0.9151},
            {"industry": "Education", "n_firms": 32.0, "beta": 0.7809, "de_ratio": 0.2438, "eff_tax": 0.1554, "unlevered_beta": 0.6602, "unlevered_beta_cash_adj": 0.7196},
            {"industry": "Electrical Equipment", "n_firms": 112.0, "beta": 1.2511, "de_ratio": 0.12, "eff_tax": 0.0482, "unlevered_beta": 1.1478, "unlevered_beta_cash_adj": 1.1898},
            {"industry": "Electronics (Consumer & Office)", "n_firms": 8.0, "beta": 0.866, "de_ratio": 0.058, "eff_tax": 0.0, "unlevered_beta": 0.8299, "unlevered_beta_cash_adj": 0.9274},
            {"industry": "Electronics (General)", "n_firms": 114.0, "beta": 0.9715, "de_ratio": 0.1101, "eff_tax": 0.0804, "unlevered_beta": 0.8974, "unlevered_beta_cash_adj": 0.9375},
            {"industry": "Engineering/Construction", "n_firms": 48.0, "beta": 1.21, "de_ratio": 0.1401, "eff_tax": 0.1364, "unlevered_beta": 1.0949, "unlevered_beta_cash_adj": 1.1374},
            {"industry": "Entertainment", "n_firms": 92.0, "beta": 0.8252, "de_ratio": 0.1591, "eff_tax": 0.033, "unlevered_beta": 0.7372, "unlevered_beta_cash_adj": 0.7628},
            {"industry": "Environmental & Waste Services", "n_firms": 53.0, "beta": 0.9455, "de_ratio": 0.2145, "eff_tax": 0.0427, "unlevered_beta": 0.8145, "unlevered_beta_cash_adj": 0.8248},
            {"industry": "Farming/Agriculture", "n_firms": 35.0, "beta": 1.1292, "de_ratio": 0.5185, "eff_tax": 0.0629, "unlevered_beta": 0.813, "unlevered_beta_cash_adj": 0.8458},
            {"industry": "Financial Svcs. (Non-bank & Insurance)", "n_firms": 176.0, "beta": 0.9697, "de_ratio": 2.7213, "eff_tax": 0.1206, "unlevered_beta": 0.3189, "unlevered_beta_cash_adj": 0.3277},
            {"industry": "Food Processing", "n_firms": 78.0, "beta": 0.6075, "de_ratio": 0.4373, "eff_tax": 0.1037, "unlevered_beta": 0.4574, "unlevered_beta_cash_adj": 0.4694},
            {"industry": "Food Wholesalers", "n_firms": 13.0, "beta": 0.8666, "de_ratio": 0.4697, "eff_tax": 0.0915, "unlevered_beta": 0.6409, "unlevered_beta_cash_adj": 0.6478},
            {"industry": "Furn/Home Furnishings", "n_firms": 27.0, "beta": 0.8232, "de_ratio": 0.4233, "eff_tax": 0.1138, "unlevered_beta": 0.6248, "unlevered_beta_cash_adj": 0.6521},
            {"industry": "Green & Renewable Energy", "n_firms": 15.0, "beta": 0.8563, "de_ratio": 1.1311, "eff_tax": 0.0, "unlevered_beta": 0.4633, "unlevered_beta_cash_adj": 0.4723},
            {"industry": "Healthcare Products", "n_firms": 204.0, "beta": 0.9086, "de_ratio": 0.1279, "eff_tax": 0.0485, "unlevered_beta": 0.8291, "unlevered_beta_cash_adj": 0.8569},
            {"industry": "Healthcare Support Services", "n_firms": 104.0, "beta": 0.872, "de_ratio": 0.3543, "eff_tax": 0.098, "unlevered_beta": 0.6889, "unlevered_beta_cash_adj": 0.7449},
            {"industry": "Heathcare Information and Technology", "n_firms": 115.0, "beta": 1.1083, "de_ratio": 0.1574, "eff_tax": 0.0638, "unlevered_beta": 0.9913, "unlevered_beta_cash_adj": 1.0163},
            {"industry": "Homebuilding", "n_firms": 30.0, "beta": 0.9106, "de_ratio": 0.2134, "eff_tax": 0.1699, "unlevered_beta": 0.785, "unlevered_beta_cash_adj": 0.8532},
            {"industry": "Hospitals/Healthcare Facilities", "n_firms": 31.0, "beta": 0.7997, "de_ratio": 0.5992, "eff_tax": 0.1135, "unlevered_beta": 0.5517, "unlevered_beta_cash_adj": 0.5649},
            {"industry": "Hotel/Gaming", "n_firms": 63.0, "beta": 1.0804, "de_ratio": 0.3975, "eff_tax": 0.0831, "unlevered_beta": 0.8323, "unlevered_beta_cash_adj": 0.8757},
            {"industry": "Household Products", "n_firms": 110.0, "beta": 0.8155, "de_ratio": 0.1815, "eff_tax": 0.065, "unlevered_beta": 0.7178, "unlevered_beta_cash_adj": 0.7411},
            {"industry": "Information Services", "n_firms": 15.0, "beta": 0.9206, "de_ratio": 0.3317, "eff_tax": 0.1816, "unlevered_beta": 0.7372, "unlevered_beta_cash_adj": 0.7564},
            {"industry": "Insurance (General)", "n_firms": 21.0, "beta": 0.672, "de_ratio": 0.2563, "eff_tax": 0.1277, "unlevered_beta": 0.5636, "unlevered_beta_cash_adj": 0.5781},
            {"industry": "Insurance (Life)", "n_firms": 20.0, "beta": 0.6445, "de_ratio": 0.6784, "eff_tax": 0.1519, "unlevered_beta": 0.4272, "unlevered_beta_cash_adj": 0.5317},
            {"industry": "Insurance (Prop/Cas.)", "n_firms": 57.0, "beta": 0.4841, "de_ratio": 0.1483, "eff_tax": 0.1837, "unlevered_beta": 0.4357, "unlevered_beta_cash_adj": 0.457},
            {"industry": "Investments & Asset Management", "n_firms": 283.0, "beta": 0.6596, "de_ratio": 0.3269, "eff_tax": 0.0353, "unlevered_beta": 0.5297, "unlevered_beta_cash_adj": 0.5881},
            {"industry": "Machinery", "n_firms": 105.0, "beta": 0.9636, "de_ratio": 0.1469, "eff_tax": 0.1337, "unlevered_beta": 0.868, "unlevered_beta_cash_adj": 0.894},
            {"industry": "Metals & Mining", "n_firms": 73.0, "beta": 1.043, "de_ratio": 0.1098, "eff_tax": 0.0252, "unlevered_beta": 0.9636, "unlevered_beta_cash_adj": 1.0104},
            {"industry": "Office Equipment & Services", "n_firms": 14.0, "beta": 1.3344, "de_ratio": 0.481, "eff_tax": 0.123, "unlevered_beta": 0.9806, "unlevered_beta_cash_adj": 1.0382},
            {"industry": "Oil/Gas (Integrated)", "n_firms": 4.0, "beta": 0.2994, "de_ratio": 0.1385, "eff_tax": 0.2824, "unlevered_beta": 0.2712, "unlevered_beta_cash_adj": 0.278},
            {"industry": "Oil/Gas (Production and Exploration)", "n_firms": 142.0, "beta": 0.7214, "de_ratio": 0.3759, "eff_tax": 0.0666, "unlevered_beta": 0.5628, "unlevered_beta_cash_adj": 0.5785},
            {"industry": "Oil/Gas Distribution", "n_firms": 23.0, "beta": 0.6692, "de_ratio": 0.5853, "eff_tax": 0.0917, "unlevered_beta": 0.4651, "unlevered_beta_cash_adj": 0.4693},
            {"industry": "Oilfield Svcs/Equip.", "n_firms": 97.0, "beta": 0.9512, "de_ratio": 0.3736, "eff_tax": 0.0875, "unlevered_beta": 0.743, "unlevered_beta_cash_adj": 0.7871},
            {"industry": "Packaging & Container", "n_firms": 19.0, "beta": 1.0219, "de_ratio": 0.5511, "eff_tax": 0.1557, "unlevered_beta": 0.7231, "unlevered_beta_cash_adj": 0.749},
            {"industry": "Paper/Forest Products", "n_firms": 6.0, "beta": 0.958, "de_ratio": 0.4369, "eff_tax": 0.086, "unlevered_beta": 0.7216, "unlevered_beta_cash_adj": 0.7697},
            {"industry": "Power", "n_firms": 46.0, "beta": 0.4824, "de_ratio": 0.7415, "eff_tax": 0.1275, "unlevered_beta": 0.31, "unlevered_beta_cash_adj": 0.3146},
            {"industry": "Precious Metals", "n_firms": 56.0, "beta": 0.8366, "de_ratio": 0.0728, "eff_tax": 0.0597, "unlevered_beta": 0.7932, "unlevered_beta_cash_adj": 0.8309},
            {"industry": "Publishing & Newspapers", "n_firms": 19.0, "beta": 0.563, "de_ratio": 0.2394, "eff_tax": 0.1021, "unlevered_beta": 0.4773, "unlevered_beta_cash_adj": 0.5119},
            {"industry": "R.E.I.T.", "n_firms": 190.0, "beta": 0.6418, "de_ratio": 0.8446, "eff_tax": 0.0158, "unlevered_beta": 0.3929, "unlevered_beta_cash_adj": 0.4005},
            {"industry": "Real Estate (Development)", "n_firms": 14.0, "beta": 0.8436, "de_ratio": 1.0183, "eff_tax": 0.0491, "unlevered_beta": 0.4783, "unlevered_beta_cash_adj": 0.5641},
            {"industry": "Real Estate (General/Diversified)", "n_firms": 12.0, "beta": 0.81, "de_ratio": 0.5356, "eff_tax": 0.0465, "unlevered_beta": 0.5779, "unlevered_beta_cash_adj": 0.627},
            {"industry": "Real Estate (Operations & Services)", "n_firms": 54.0, "beta": 0.9655, "de_ratio": 0.2464, "eff_tax": 0.087, "unlevered_beta": 0.8149, "unlevered_beta_cash_adj": 0.8576},
            {"industry": "Recreation", "n_firms": 49.0, "beta": 1.0235, "de_ratio": 0.6299, "eff_tax": 0.1145, "unlevered_beta": 0.6951, "unlevered_beta_cash_adj": 0.7365},
            {"industry": "Reinsurance", "n_firms": 1.0, "beta": 0.5813, "de_ratio": 0.4347, "eff_tax": 0.3036, "unlevered_beta": 0.4384, "unlevered_beta_cash_adj": 0.5777},
            {"industry": "Restaurant/Dining", "n_firms": 64.0, "beta": 0.9241, "de_ratio": 0.2722, "eff_tax": 0.0992, "unlevered_beta": 0.7674, "unlevered_beta_cash_adj": 0.783},
            {"industry": "Retail (Automotive)", "n_firms": 34.0, "beta": 0.936, "de_ratio": 0.4536, "eff_tax": 0.1077, "unlevered_beta": 0.6984, "unlevered_beta_cash_adj": 0.7132},
            {"industry": "Retail (Building Supply)", "n_firms": 14.0, "beta": 1.5353, "de_ratio": 0.2329, "eff_tax": 0.1184, "unlevered_beta": 1.307, "unlevered_beta_cash_adj": 1.3177},
            {"industry": "Retail (Distributors)", "n_firms": 62.0, "beta": 0.9484, "de_ratio": 0.2824, "eff_tax": 0.1459, "unlevered_beta": 0.7827, "unlevered_beta_cash_adj": 0.8016},
            {"industry": "Retail (General)", "n_firms": 23.0, "beta": 0.8052, "de_ratio": 0.0794, "eff_tax": 0.191, "unlevered_beta": 0.76, "unlevered_beta_cash_adj": 0.7809},
            {"industry": "Retail (Grocery and Food)", "n_firms": 15.0, "beta": 1.1183, "de_ratio": 0.5195, "eff_tax": 0.1222, "unlevered_beta": 0.8048, "unlevered_beta_cash_adj": 0.8484},
            {"industry": "Retail (REITs)", "n_firms": 26.0, "beta": 0.6207, "de_ratio": 0.5642, "eff_tax": 0.016, "unlevered_beta": 0.4362, "unlevered_beta_cash_adj": 0.4427},
            {"industry": "Retail (Special Lines)", "n_firms": 94.0, "beta": 1.0894, "de_ratio": 0.1976, "eff_tax": 0.1007, "unlevered_beta": 0.9488, "unlevered_beta_cash_adj": 1.0019},
            {"industry": "Rubber& Tires", "n_firms": 3.0, "beta": 0.5294, "de_ratio": 3.5847, "eff_tax": 0.0, "unlevered_beta": 0.1435, "unlevered_beta_cash_adj": 0.1544},
            {"industry": "Semiconductor", "n_firms": 66.0, "beta": 1.5182, "de_ratio": 0.0259, "eff_tax": 0.0511, "unlevered_beta": 1.4893, "unlevered_beta_cash_adj": 1.5046},
            {"industry": "Semiconductor Equip", "n_firms": 31.0, "beta": 1.3972, "de_ratio": 0.0486, "eff_tax": 0.0996, "unlevered_beta": 1.3481, "unlevered_beta_cash_adj": 1.3917},
            {"industry": "Shipbuilding & Marine", "n_firms": 8.0, "beta": 0.7529, "de_ratio": 0.2255, "eff_tax": 0.0526, "unlevered_beta": 0.644, "unlevered_beta_cash_adj": 0.6608},
            {"industry": "Shoe", "n_firms": 11.0, "beta": 1.0173, "de_ratio": 0.1194, "eff_tax": 0.1186, "unlevered_beta": 0.9337, "unlevered_beta_cash_adj": 1.0002},
            {"industry": "Software (Entertainment)", "n_firms": 77.0, "beta": 1.0283, "de_ratio": 0.0204, "eff_tax": 0.0529, "unlevered_beta": 1.0128, "unlevered_beta_cash_adj": 1.0207},
            {"industry": "Software (Internet)", "n_firms": 29.0, "beta": 1.6887, "de_ratio": 0.123, "eff_tax": 0.0305, "unlevered_beta": 1.5461, "unlevered_beta_cash_adj": 1.5905},
            {"industry": "Software (System & Application)", "n_firms": 309.0, "beta": 1.2766, "de_ratio": 0.0558, "eff_tax": 0.0551, "unlevered_beta": 1.2254, "unlevered_beta_cash_adj": 1.2482},
            {"industry": "Steel", "n_firms": 19.0, "beta": 1.0628, "de_ratio": 0.2351, "eff_tax": 0.0928, "unlevered_beta": 0.9035, "unlevered_beta_cash_adj": 0.9449},
            {"industry": "Telecom (Wireless)", "n_firms": 12.0, "beta": 0.5381, "de_ratio": 0.5195, "eff_tax": 0.0402, "unlevered_beta": 0.3873, "unlevered_beta_cash_adj": 0.3924},
            {"industry": "Telecom. Equipment", "n_firms": 57.0, "beta": 0.9232, "de_ratio": 0.0922, "eff_tax": 0.0706, "unlevered_beta": 0.8635, "unlevered_beta_cash_adj": 0.8873},
            {"industry": "Telecom. Services", "n_firms": 39.0, "beta": 0.6287, "de_ratio": 0.9606, "eff_tax": 0.034, "unlevered_beta": 0.3654, "unlevered_beta_cash_adj": 0.3815},
            {"industry": "Tobacco", "n_firms": 10.0, "beta": 0.7943, "de_ratio": 0.2297, "eff_tax": 0.1477, "unlevered_beta": 0.6776, "unlevered_beta_cash_adj": 0.6903},
            {"industry": "Transportation", "n_firms": 19.0, "beta": 0.8599, "de_ratio": 0.3645, "eff_tax": 0.0854, "unlevered_beta": 0.6753, "unlevered_beta_cash_adj": 0.7115},
            {"industry": "Transportation (Railroads)", "n_firms": 4.0, "beta": 0.9751, "de_ratio": 0.2779, "eff_tax": 0.1698, "unlevered_beta": 0.8069, "unlevered_beta_cash_adj": 0.8137},
            {"industry": "Trucking", "n_firms": 26.0, "beta": 1.0113, "de_ratio": 0.2523, "eff_tax": 0.126, "unlevered_beta": 0.8504, "unlevered_beta_cash_adj": 0.8689},
            {"industry": "Utility (General)", "n_firms": 14.0, "beta": 0.2394, "de_ratio": 0.8148, "eff_tax": 0.1277, "unlevered_beta": 0.1486, "unlevered_beta_cash_adj": 0.1491},
            {"industry": "Utility (Water)", "n_firms": 14.0, "beta": 0.4127, "de_ratio": 0.6236, "eff_tax": 0.1119, "unlevered_beta": 0.2812, "unlevered_beta_cash_adj": 0.2825},
            {"industry": "Total Market", "n_firms": 5994.0, "beta": 0.9121, "de_ratio": 0.3517, "eff_tax": 0.083, "unlevered_beta": 0.7217, "unlevered_beta_cash_adj": 0.7557},
            {"industry": "Total Market (without financials)", "n_firms": 4822.0, "beta": 0.9913, "de_ratio": 0.1729, "eff_tax": 0.071, "unlevered_beta": 0.8775, "unlevered_beta_cash_adj": 0.901},
        ],
    },
    "bbb_spread": {"as_of": "January 2026", "value": 0.0111, "rating": "Baa2/BBB"},
    "tax": {
        "as_of": "OECD Corporate Tax Statistics (latest year per jurisdiction, mostly 2026)",
        "rows": [
            {"code": "AUS", "country": "Australia", "year": 2026, "rate": 0.3},
            {"code": "AUT", "country": "Austria", "year": 2026, "rate": 0.23},
            {"code": "BEL", "country": "Belgium", "year": 2026, "rate": 0.25},
            {"code": "CAN", "country": "Canada", "year": 2026, "rate": 0.26},
            {"code": "CHL", "country": "Chile", "year": 2026, "rate": 0.27},
            {"code": "COL", "country": "Colombia", "year": 2025, "rate": 0.35},
            {"code": "CRI", "country": "Costa Rica", "year": 2026, "rate": 0.3},
            {"code": "CZE", "country": "Czechia", "year": 2026, "rate": 0.21},
            {"code": "DNK", "country": "Denmark", "year": 2026, "rate": 0.22},
            {"code": "EST", "country": "Estonia", "year": 2026, "rate": 0.22},
            {"code": "FIN", "country": "Finland", "year": 2026, "rate": 0.2},
            {"code": "FRA", "country": "France", "year": 2026, "rate": 0.3613},
            {"code": "DEU", "country": "Germany", "year": 2026, "rate": 0.30133},
            {"code": "GRC", "country": "Greece", "year": 2026, "rate": 0.22},
            {"code": "HUN", "country": "Hungary", "year": 2026, "rate": 0.09},
            {"code": "ISL", "country": "Iceland", "year": 2026, "rate": 0.2},
            {"code": "IRL", "country": "Ireland", "year": 2026, "rate": 0.125},
            {"code": "ISR", "country": "Israel", "year": 2025, "rate": 0.23},
            {"code": "ITA", "country": "Italy", "year": 2026, "rate": 0.2781},
            {"code": "JPN", "country": "Japan", "year": 2026, "rate": 0.2974},
            {"code": "KOR", "country": "Korea", "year": 2026, "rate": 0.275},
            {"code": "LVA", "country": "Latvia", "year": 2026, "rate": 0.2},
            {"code": "LTU", "country": "Lithuania", "year": 2026, "rate": 0.17},
            {"code": "LUX", "country": "Luxembourg", "year": 2026, "rate": 0.23870000000000002},
            {"code": "MEX", "country": "Mexico", "year": 2026, "rate": 0.3},
            {"code": "NLD", "country": "Netherlands", "year": 2026, "rate": 0.258},
            {"code": "NZL", "country": "New Zealand", "year": 2026, "rate": 0.28},
            {"code": "NOR", "country": "Norway", "year": 2026, "rate": 0.22},
            {"code": "POL", "country": "Poland", "year": 2026, "rate": 0.19},
            {"code": "PRT", "country": "Portugal", "year": 2026, "rate": 0.295},
            {"code": "SVK", "country": "Slovak Republic", "year": 2026, "rate": 0.24},
            {"code": "SVN", "country": "Slovenia", "year": 2026, "rate": 0.22},
            {"code": "ESP", "country": "Spain", "year": 2026, "rate": 0.25},
            {"code": "SWE", "country": "Sweden", "year": 2026, "rate": 0.20600000000000002},
            {"code": "CHE", "country": "Switzerland", "year": 2026, "rate": 0.194719},
            {"code": "TUR", "country": "Türkiye", "year": 2025, "rate": 0.25},
            {"code": "GBR", "country": "United Kingdom", "year": 2026, "rate": 0.25},
            {"code": "USA", "country": "United States", "year": 2026, "rate": 0.255285},
            {"code": "ALB", "country": "Albania", "year": 2026, "rate": 0.15},
            {"code": "AGO", "country": "Angola", "year": 2026, "rate": 0.25},
            {"code": "AND", "country": "Andorra", "year": 2026, "rate": 0.1},
            {"code": "AIA", "country": "Anguilla", "year": 2026, "rate": 0.0},
            {"code": "ATG", "country": "Antigua and Barbuda", "year": 2026, "rate": 0.25},
            {"code": "ARG", "country": "Argentina", "year": 2026, "rate": 0.35},
            {"code": "ARM", "country": "Armenia", "year": 2026, "rate": 0.18},
            {"code": "ABW", "country": "Aruba", "year": 2026, "rate": 0.22},
            {"code": "AZE", "country": "Azerbaijan", "year": 2026, "rate": 0.2},
            {"code": "BHS", "country": "Bahamas", "year": 2026, "rate": 0.0},
            {"code": "BHR", "country": "Bahrain", "year": 2026, "rate": 0.0},
            {"code": "GGY", "country": "Bailiwick of Guernsey", "year": 2026, "rate": 0.0},
            {"code": "BRB", "country": "Barbados", "year": 2026, "rate": 0.09},
            {"code": "BLZ", "country": "Belize", "year": 2026, "rate": 0.0},
            {"code": "BEN", "country": "Benin", "year": 2026, "rate": 0.3},
            {"code": "BMU", "country": "Bermuda", "year": 2026, "rate": 0.0},
            {"code": "BIH", "country": "Bosnia and Herzegovina", "year": 2026, "rate": 0.1},
            {"code": "BWA", "country": "Botswana", "year": 2026, "rate": 0.22},
            {"code": "BRA", "country": "Brazil", "year": 2026, "rate": 0.34},
            {"code": "VGB", "country": "British Virgin Islands", "year": 2026, "rate": 0.0},
            {"code": "BRN", "country": "Brunei Darussalam", "year": 2026, "rate": 0.185},
            {"code": "BGR", "country": "Bulgaria", "year": 2026, "rate": 0.1},
            {"code": "BFA", "country": "Burkina Faso", "year": 2026, "rate": 0.275},
            {"code": "CPV", "country": "Cabo Verde", "year": 2026, "rate": 0.204},
            {"code": "CMR", "country": "Cameroon", "year": 2026, "rate": 0.33},
            {"code": "CYM", "country": "Cayman Islands", "year": 2026, "rate": 0.0},
            {"code": "CHN", "country": "China (People’s Republic of)", "year": 2026, "rate": 0.25},
            {"code": "COG", "country": "Congo", "year": 2026, "rate": 0.28},
            {"code": "COK", "country": "Cook Islands", "year": 2026, "rate": 0.2},
            {"code": "CIV", "country": "Côte d’Ivoire", "year": 2026, "rate": 0.25},
            {"code": "HRV", "country": "Croatia", "year": 2026, "rate": 0.18},
            {"code": "CUW", "country": "Curaçao", "year": 2026, "rate": 0.15},
            {"code": "COD", "country": "Democratic Republic of the Congo", "year": 2026, "rate": 0.3},
            {"code": "DJI", "country": "Djibouti", "year": 2026, "rate": 0.25},
            {"code": "DMA", "country": "Dominica", "year": 2026, "rate": 0.25},
            {"code": "DOM", "country": "Dominican Republic", "year": 2026, "rate": 0.27},
            {"code": "EGY", "country": "Egypt", "year": 2026, "rate": 0.225},
            {"code": "SWZ", "country": "Eswatini", "year": 2026, "rate": 0.25},
            {"code": "FRO", "country": "Faroe Islands", "year": 2026, "rate": 0.18},
            {"code": "FJI", "country": "Fiji", "year": 2026, "rate": 0.25},
            {"code": "GAB", "country": "Gabon", "year": 2026, "rate": 0.3},
            {"code": "GEO", "country": "Georgia", "year": 2026, "rate": 0.15},
            {"code": "GIB", "country": "Gibraltar", "year": 2026, "rate": 0.15},
            {"code": "GRL", "country": "Greenland", "year": 2026, "rate": 0.265},
            {"code": "GRD", "country": "Grenada", "year": 2026, "rate": 0.28},
            {"code": "GTM", "country": "Guatemala", "year": 2026, "rate": 0.25},
            {"code": "HTI", "country": "Haiti", "year": 2026, "rate": 0.3},
            {"code": "HND", "country": "Honduras", "year": 2026, "rate": 0.25},
            {"code": "HKG", "country": "Hong Kong (China)", "year": 2026, "rate": 0.165},
            {"code": "IND", "country": "India", "year": 2026, "rate": 0.25170000000000003},
            {"code": "IDN", "country": "Indonesia", "year": 2026, "rate": 0.22},
            {"code": "IMN", "country": "Isle of Man", "year": 2026, "rate": 0.0},
            {"code": "JAM", "country": "Jamaica", "year": 2026, "rate": 0.25},
            {"code": "JEY", "country": "Jersey", "year": 2026, "rate": 0.0},
            {"code": "JOR", "country": "Jordan", "year": 2026, "rate": 0.2},
            {"code": "KAZ", "country": "Kazakhstan", "year": 2026, "rate": 0.2},
            {"code": "KEN", "country": "Kenya", "year": 2026, "rate": 0.3},
            {"code": "KWT", "country": "Kuwait", "year": 2026, "rate": 0.15},
            {"code": "LBR", "country": "Liberia", "year": 2026, "rate": 0.25},
            {"code": "LIE", "country": "Liechtenstein", "year": 2026, "rate": 0.125},
            {"code": "MAC", "country": "Macau (China)", "year": 2026, "rate": 0.12},
            {"code": "MYS", "country": "Malaysia", "year": 2026, "rate": 0.24},
            {"code": "MDV", "country": "Maldives", "year": 2026, "rate": 0.15},
            {"code": "MLT", "country": "Malta", "year": 2026, "rate": 0.35},
            {"code": "MRT", "country": "Mauritania", "year": 2026, "rate": 0.25},
            {"code": "MUS", "country": "Mauritius", "year": 2026, "rate": 0.15},
            {"code": "MDA", "country": "Moldova", "year": 2026, "rate": 0.12},
            {"code": "MCO", "country": "Monaco", "year": 2026, "rate": 0.25},
            {"code": "MNG", "country": "Mongolia", "year": 2026, "rate": 0.25},
            {"code": "MNE", "country": "Montenegro", "year": 2026, "rate": 0.15},
            {"code": "MSR", "country": "Montserrat", "year": 2026, "rate": 0.3},
            {"code": "MAR", "country": "Morocco", "year": 2026, "rate": 0.35},
            {"code": "NAM", "country": "Namibia", "year": 2026, "rate": 0.32},
            {"code": "NGA", "country": "Nigeria", "year": 2026, "rate": 0.3},
            {"code": "MKD", "country": "North Macedonia", "year": 2026, "rate": 0.1},
            {"code": "OMN", "country": "Oman", "year": 2026, "rate": 0.15},
            {"code": "PAK", "country": "Pakistan", "year": 2026, "rate": 0.29},
            {"code": "PAN", "country": "Panama", "year": 2026, "rate": 0.25},
            {"code": "PNG", "country": "Papua New Guinea", "year": 2026, "rate": 0.3},
            {"code": "PRY", "country": "Paraguay", "year": 2026, "rate": 0.1},
            {"code": "PER", "country": "Peru", "year": 2026, "rate": 0.295},
            {"code": "PHL", "country": "Philippines", "year": 2026, "rate": 0.25},
            {"code": "QAT", "country": "Qatar", "year": 2026, "rate": 0.1},
            {"code": "ROU", "country": "Romania", "year": 2026, "rate": 0.16},
            {"code": "KNA", "country": "Saint Kitts and Nevis", "year": 2026, "rate": 0.25},
            {"code": "LCA", "country": "Saint Lucia", "year": 2026, "rate": 0.3},
            {"code": "VCT", "country": "Saint Vincent and the Grenadines", "year": 2026, "rate": 0.28},
            {"code": "WSM", "country": "Samoa", "year": 2026, "rate": 0.27},
            {"code": "SMR", "country": "San Marino", "year": 2026, "rate": 0.18},
            {"code": "SAU", "country": "Saudi Arabia", "year": 2026, "rate": 0.2},
            {"code": "SEN", "country": "Senegal", "year": 2026, "rate": 0.3},
            {"code": "SRB", "country": "Serbia", "year": 2026, "rate": 0.15},
            {"code": "SYC", "country": "Seychelles", "year": 2026, "rate": 0.25},
            {"code": "SLE", "country": "Sierra Leone", "year": 2026, "rate": 0.25},
            {"code": "SGP", "country": "Singapore", "year": 2026, "rate": 0.17},
            {"code": "ZAF", "country": "South Africa", "year": 2026, "rate": 0.27},
            {"code": "LKA", "country": "Sri Lanka", "year": 2026, "rate": 0.3},
            {"code": "THA", "country": "Thailand", "year": 2026, "rate": 0.2},
            {"code": "TGO", "country": "Togo", "year": 2026, "rate": 0.27},
            {"code": "TTO", "country": "Trinidad and Tobago", "year": 2026, "rate": 0.3},
            {"code": "TUN", "country": "Tunisia", "year": 2026, "rate": 0.2},
            {"code": "TCA", "country": "Turks and Caicos Islands", "year": 2026, "rate": 0.0},
            {"code": "UKR", "country": "Ukraine", "year": 2026, "rate": 0.18},
            {"code": "ARE", "country": "United Arab Emirates", "year": 2026, "rate": 0.09},
            {"code": "URY", "country": "Uruguay", "year": 2026, "rate": 0.25},
            {"code": "UZB", "country": "Uzbekistan", "year": 2026, "rate": 0.15},
            {"code": "VNM", "country": "Viet Nam", "year": 2026, "rate": 0.2},
            {"code": "ZMB", "country": "Zambia", "year": 2026, "rate": 0.3},
        ],
    },
    "yields": {
        "as_of": "2026-11-12/13",
        "rows": [
            {"country": "United States", "yield": 0.051269999999999996, "date_label": "Nov/13"},
            {"country": "United Kingdom", "yield": 0.052816, "date_label": "Nov/12"},
            {"country": "Japan", "yield": 0.0287, "date_label": "Nov/13"},
            {"country": "Australia", "yield": 0.051289999999999995, "date_label": "Nov/13"},
            {"country": "Germany", "yield": 0.033256, "date_label": "Nov/12"},
            {"country": "Brazil", "yield": 0.15439999999999998, "date_label": "Nov/12"},
            {"country": "Russia", "yield": 0.1673, "date_label": "Nov/12"},
            {"country": "India", "yield": 0.07248, "date_label": "Nov/12"},
            {"country": "Canada", "yield": 0.0381, "date_label": "Nov/12"},
            {"country": "Italy", "yield": 0.04388499999999999, "date_label": "Nov/12"},
            {"country": "France", "yield": 0.04836, "date_label": "Nov/12"},
            {"country": "South Africa", "yield": 0.0864, "date_label": "Nov/12"},
            {"country": "China", "yield": 0.016040000000000002, "date_label": "Nov/13"},
            {"country": "Switzerland", "yield": 0.005533, "date_label": "Nov/12"},
            {"country": "Mexico", "yield": 0.09278, "date_label": "Nov/12"},
            {"country": "Netherlands", "yield": 0.034256, "date_label": "Nov/12"},
            {"country": "New Zealand", "yield": 0.0473, "date_label": "Nov/13"},
            {"country": "Portugal", "yield": 0.03796, "date_label": "Nov/12"},
            {"country": "South Korea", "yield": 0.04014, "date_label": "Nov/13"},
            {"country": "Spain", "yield": 0.039045, "date_label": "Nov/12"},
            {"country": "Greece", "yield": 0.041740000000000006, "date_label": "Nov/12"},
            {"country": "Turkey", "yield": 0.3336, "date_label": "Nov/12"},
            {"country": "Finland", "yield": 0.037145000000000004, "date_label": "Nov/12"},
            {"country": "Lithuania", "yield": 0.042699999999999995, "date_label": "Nov/12"},
            {"country": "Norway", "yield": 0.04327, "date_label": "Nov/12"},
            {"country": "Poland", "yield": 0.06268, "date_label": "Nov/12"},
            {"country": "Romania", "yield": 0.0708, "date_label": "Nov/12"},
            {"country": "Slovakia", "yield": 0.042539999999999994, "date_label": "Nov/12"},
            {"country": "Slovenia", "yield": 0.03828, "date_label": "Nov/12"},
            {"country": "Sweden", "yield": 0.03063, "date_label": "Nov/12"},
            {"country": "Hungary", "yield": 0.054889, "date_label": "Nov/12"},
            {"country": "Iceland", "yield": 0.06764, "date_label": "Nov/12"},
            {"country": "Ireland", "yield": 0.034894, "date_label": "Nov/12"},
            {"country": "Austria", "yield": 0.035707, "date_label": "Nov/12"},
            {"country": "Belgium", "yield": 0.042911000000000005, "date_label": "Nov/12"},
            {"country": "Croatia", "yield": 0.038665, "date_label": "Nov/12"},
            {"country": "Czech Republic", "yield": 0.051711, "date_label": "Nov/12"},
            {"country": "Denmark", "yield": 0.03121, "date_label": "Nov/12"},
            {"country": "Peru", "yield": 0.0626, "date_label": "Nov/12"},
            {"country": "Chile", "yield": 0.0596, "date_label": "Nov/12"},
            {"country": "Colombia", "yield": 0.12711, "date_label": "Nov/12"},
            {"country": "Singapore", "yield": 0.0233, "date_label": "Nov/13"},
            {"country": "Taiwan", "yield": 0.018600000000000002, "date_label": "Nov/13"},
            {"country": "Thailand", "yield": 0.01978, "date_label": "Nov/13"},
            {"country": "Vietnam", "yield": 0.045726, "date_label": "Nov/13"},
            {"country": "Hong Kong", "yield": 0.03825, "date_label": "Nov/13"},
            {"country": "Indonesia", "yield": 0.07629999999999999, "date_label": "Nov/12"},
            {"country": "Israel", "yield": 0.04111, "date_label": "Nov/12"},
            {"country": "Malaysia", "yield": 0.038552, "date_label": "Nov/12"},
            {"country": "Pakistan", "yield": 0.12408, "date_label": "Nov/12"},
            {"country": "Philippines", "yield": 0.07769999999999999, "date_label": "Nov/13"},
            {"country": "Zambia", "yield": 0.1475, "date_label": "Nov/12"},
            {"country": "Kenya", "yield": 0.125, "date_label": "Nov/12"},
            {"country": "Nigeria", "yield": 0.16596, "date_label": "Nov/11"},
        ],
    },
}

# ======================================================================================
# 4. STREAMLIT APP
# ======================================================================================
st.set_page_config(page_title="International Cost of Capital", page_icon="🌍", layout="wide")

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"


# ---------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------
@st.cache_data(ttl=6 * 3600, show_spinner="Fetching latest data from Damodaran, OECD and Trading Economics...")
def load_data():
    return fetch_all()


@st.cache_data(ttl=3600, show_spinner=False)
def load_country_yield(country: str):
    return fetch_country_yield(country)


def pct(x, nd=4):
    return "n/a" if x is None or pd.isna(x) else f"{x * 100:.{nd}f}%"


def override_input(label: str, fetched: float | None, key: str, *, is_pct=True, min_v=0.0, max_v=100.0,
                   step=0.01, fmt="%.4f", force=False, help_text=None) -> tuple[float, bool]:
    """Toggle + number input. Off -> shows the fetched value (disabled). On -> manual value."""
    on = st.toggle(f"Override {label}", value=force, key=f"ovr_{key}", disabled=force,
                   help=help_text or ("No fetched value available - enter it manually." if force else None))
    scale = 100 if is_pct else 1
    shown = (fetched if fetched is not None and not pd.isna(fetched) else 0.0) * scale
    if on:
        val = st.number_input(f"{label}{' (%)' if is_pct else ''}", min_value=min_v, max_value=max_v,
                              value=float(shown), step=step, format=fmt, key=f"val_{key}")
        return val / scale, True
    st.number_input(f"{label}{' (%)' if is_pct else ''} - fetched", value=float(shown), format=fmt,
                    disabled=True, key=f"show_{key}_{shown:.6f}")
    return (fetched if fetched is not None and not pd.isna(fetched) else 0.0), False


data = load_data()
betas = data["betas"].data
countries = build_country_table(data["yields"].data, data["tax"].data)
bbb_usd_fetched = data["bbb"].data

# ---------------------------------------------------------------------------------------
# Sidebar inputs
# ---------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Data")
    if st.button("🔄 Refresh data fetch", width="stretch"):
        load_data.clear()
        load_country_yield.clear()
        for k in [k for k in st.session_state if k.startswith("show_")]:
            del st.session_state[k]
        st.rerun()
    st.caption("Fetched: " + data["yields"].fetched_at)

    st.header("Project")
    industries = betas["industry"].tolist()
    industry = st.selectbox("Industry (Damodaran US)", industries,
                            index=industries.index(DEFAULT_INDUSTRY) if DEFAULT_INDUSTRY in industries else 0)
    country_list = countries["country"].tolist()
    country = st.selectbox("Target country", country_list,
                           index=country_list.index(DEFAULT_COUNTRY) if DEFAULT_COUNTRY in country_list else 0)
    erp = st.slider("Equity risk premium (%)", 0.0, 15.0, 6.5, 0.1) / 100
    debt_share = st.slider("Target debt share D/(D+E) (%)", 0.0, 90.0, 30.0, 1.0) / 100

    # The /bonds table is behind a bot check for servers; fall back to each country's own page.
    yield_src = {c: (data["yields"].status, data["yields"].source_url) for c in ("United States", country)}
    if data["yields"].status != "live":
        for c in ("United States", country):
            cy = load_country_yield(c)
            if cy.status == "live":
                countries.loc[countries["country"] == c, ["yield_10y", "yield_date"]] = [cy.data["yield"], cy.data["date"]]
                yield_src[c] = ("live (country page)", cy.source_url)
    yields_live = all(v[0].startswith("live") for v in yield_src.values())

    crow = countries.set_index("country").loc[country]
    us_row = countries.set_index("country").loc["United States"]
    ccy = crow["currency"]

    st.header("Project tax rate")
    tax_choice = st.radio("Use the corporate tax rate of", ["U.S.", country if country != "United States" else "Selected country (U.S.)"],
                          index=0, horizontal=True)

    st.header("Manual overrides")
    with st.expander("Override fetched data", expanded=False):
        bu_fetched = float(betas.set_index("industry").loc[industry, "unlevered_beta"])
        beta_u, ovr_beta = override_input("Unlevered beta", bu_fetched, "beta", is_pct=False,
                                          min_v=0.0, max_v=5.0, step=0.01)
        st.divider()
        tax_us, ovr_tax_us = override_input("U.S. corporate tax rate", us_row["tax_rate"], "tax_us")
        tax_fx, ovr_tax_fx = override_input(f"{country} corporate tax rate", crow["tax_rate"], "tax_fx",
                                            force=pd.isna(crow["tax_rate"]))
        st.divider()
        spread_usd, ovr_spread = override_input("USD BBB spread", bbb_usd_fetched, "spread_usd", max_v=20.0)
        st.divider()
        rf_us, ovr_rf_us = override_input("U.S. 10Y government yield", us_row["yield_10y"], "rf_us", max_v=50.0)
        rf_fx, ovr_rf_fx = override_input(f"{country} 10Y government yield", crow["yield_10y"], "rf_fx",
                                          min_v=-5.0, max_v=100.0)
        st.divider()
        auto_spread_fx = implied_foreign_spread(spread_usd, rf_us, rf_fx)
        spread_fx_manual, ovr_spread_fx = override_input(
            f"{ccy} BBB spread", auto_spread_fx, "spread_fx", max_v=50.0,
            help_text="Off = auto: USD cost of debt converted with (1+Rd*) = (1+Rd)(1+Rf*)/(1+Rf).")
        if not ovr_spread_fx:
            spread_fx_manual = None

tax_rate = tax_us if tax_choice == "U.S." else tax_fx
tax_label = "U.S." if tax_choice == "U.S." else country

# ---------------------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------------------
res = compute_project(beta_u=beta_u, tax_rate=tax_rate, debt_share=debt_share, erp=erp, rf_usd=rf_us,
                      spread_usd=spread_usd, rf_foreign=rf_fx, foreign_currency=ccy,
                      spread_foreign_manual=spread_fx_manual)

fx_col = f"{country} ({ccy})"
results = pd.DataFrame(
    {
        "Item": ["10Y government yield (risk-free)", "Unlevered beta", "Levered (equity) beta",
                 "Equity risk premium", "Cost of equity", "BBB spread", "Pre-tax cost of debt",
                 f"Project tax rate ({tax_label})", "After-tax cost of debt", "Equity weight (1 - d)",
                 "Debt weight (d)", "Equity contribution", "Debt contribution", "WACC"],
        "USD": [pct(rf_us), f"{beta_u:.4f}", f"{res.beta_e:.6f}", pct(erp), pct(res.usd.cost_of_equity),
                pct(res.usd.spread), pct(res.usd.pretax_cost_of_debt), pct(tax_rate),
                pct(res.usd.aftertax_cost_of_debt), pct(1 - debt_share, 2), pct(debt_share, 2),
                pct(res.usd.equity_contribution), pct(res.usd.debt_contribution), pct(res.usd.wacc)],
        fx_col: [pct(rf_fx), f"{beta_u:.4f}", f"{res.beta_e:.6f}", "via parity", pct(res.foreign.cost_of_equity),
                 pct(res.foreign.spread) + (" (manual)" if ovr_spread_fx else " (auto)"),
                 pct(res.foreign.pretax_cost_of_debt), pct(tax_rate), pct(res.foreign.aftertax_cost_of_debt),
                 pct(1 - debt_share, 2), pct(debt_share, 2), pct(res.foreign.equity_contribution),
                 pct(res.foreign.debt_contribution), pct(res.foreign.wacc)],
    }
)


@st.cache_data
def all_industries(betas_df, tax_rate, debt_share, erp, rf_us, spread_usd, rf_fx, spread_fx_manual):
    rows = []
    for _, b in betas_df.iterrows():
        r = compute_project(beta_u=b["unlevered_beta"], tax_rate=tax_rate, debt_share=debt_share, erp=erp,
                            rf_usd=rf_us, spread_usd=spread_usd, rf_foreign=rf_fx,
                            spread_foreign_manual=spread_fx_manual)
        rows.append({"Industry": b["industry"], "Unlevered beta": b["unlevered_beta"], "Levered beta": r.beta_e,
                     "Cost of equity (USD)": r.usd.cost_of_equity, "Cost of equity (FCY)": r.foreign.cost_of_equity,
                     "After-tax cost of debt (USD)": r.usd.aftertax_cost_of_debt,
                     "After-tax cost of debt (FCY)": r.foreign.aftertax_cost_of_debt,
                     "WACC (USD)": r.usd.wacc, "WACC (FCY)": r.foreign.wacc})
    return pd.DataFrame(rows)


ind = all_industries(betas, tax_rate, debt_share, erp, rf_us, spread_usd, rf_fx, spread_fx_manual)
ind = ind.rename(columns=lambda c: c.replace("FCY", ccy) if ccy != "USD" else c.replace("(FCY)", "(USD, target country)"))

sources = pd.DataFrame(
    [
        {"Data": "Unlevered beta - " + industry, "Value": f"{beta_u:.4f}",
         "Source": data["betas"].source_url, "As of": data["betas"].as_of,
         "Status": "manual" if ovr_beta else data["betas"].status},
        {"Data": "USD BBB (Baa2) spread, large non-financial firms", "Value": pct(spread_usd, 2),
         "Source": data["bbb"].source_url, "As of": data["bbb"].as_of,
         "Status": "manual" if ovr_spread else data["bbb"].status},
        {"Data": "U.S. corporate tax rate (combined statutory)", "Value": pct(tax_us, 2),
         "Source": data["tax"].source_url, "As of": f"{us_row['tax_year']:.0f}" if not pd.isna(us_row["tax_year"]) else "",
         "Status": "manual" if ovr_tax_us else data["tax"].status},
        {"Data": f"{country} corporate tax rate (combined statutory)", "Value": pct(tax_fx, 2),
         "Source": data["tax"].source_url, "As of": f"{crow['tax_year']:.0f}" if not pd.isna(crow["tax_year"]) else "not in OECD data",
         "Status": "manual" if ovr_tax_fx else data["tax"].status},
        {"Data": "U.S. 10Y government yield", "Value": pct(rf_us, 3), "Source": yield_src["United States"][1],
         "As of": us_row["yield_date"], "Status": "manual" if ovr_rf_us else yield_src["United States"][0]},
        {"Data": f"{country} 10Y government yield", "Value": pct(rf_fx, 3), "Source": yield_src[country][1],
         "As of": crow["yield_date"], "Status": "manual" if ovr_rf_fx else yield_src[country][0]},
    ]
)

# ---------------------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------------------
st.title("🌍 International Cost of Capital")
st.caption(f"**{industry}** project in **{country}** · project tax rate: **{tax_label}** ({pct(tax_rate, 2)}) · "
           f"ERP {pct(erp, 1)} · debt share {pct(debt_share, 0)}")

fallbacks = [r for k, r in data.items() if r.status == "snapshot" and not (k == "yields" and yields_live)]
if fallbacks:
    st.warning("Live fetch failed for: " + ", ".join(r.name for r in fallbacks)
               + ". Using the bundled snapshot instead - press **Refresh data fetch** to retry, or override manually.")
if pd.isna(crow["tax_rate"]):
    st.info(f"OECD has no corporate tax rate for {country}; enter it under Manual overrides.")

tab_res, tab_ind, tab_assume, tab_src = st.tabs(["Results", "All-industry comparison", "Assumptions & formulas", "Data sources"])

with tab_res:
    c1, c2 = st.columns(2)
    for col, title, r in [(c1, "USD", res.usd), (c2, fx_col, res.foreign)]:
        with col:
            st.subheader(title)
            m1, m2, m3 = st.columns(3)
            m1.metric("Cost of equity", pct(r.cost_of_equity, 2))
            m2.metric("After-tax cost of debt", pct(r.aftertax_cost_of_debt, 2))
            m3.metric("WACC", pct(r.wacc, 2))
    st.dataframe(results, hide_index=True, width="stretch")
    st.caption(f"Cross-check: converting the USD WACC directly, (1+WACC)(1+Rf*)/(1+Rf) - 1 = "
               f"{pct(res.wacc_foreign_direct)}. It differs from the component WACC by "
               f"d·τ·(k-1) when the foreign spread is auto, because the tax shield applies to nominal interest.")

with tab_ind:
    st.markdown(f"All Damodaran industries under the current settings (selected country **{country}**, tax {tax_label}).")
    wacc_cols = [c for c in ind.columns if c.startswith("WACC")]
    chart_df = ind.melt(id_vars=["Industry"], value_vars=wacc_cols, var_name="Currency", value_name="WACC")
    chart = (
        alt.Chart(chart_df)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("Industry:N", sort=alt.EncodingSortField("WACC", order="descending"), title=None),
            x=alt.X("WACC:Q", axis=alt.Axis(format="%"), title="WACC"),
            yOffset="Currency:N",
            color=alt.Color("Currency:N", scale=alt.Scale(range=["#2a6ebb", "#e07b39"]), legend=alt.Legend(orient="top")),
            opacity=alt.condition(alt.datum.Industry == industry, alt.value(1.0), alt.value(0.55)),
            tooltip=["Industry", "Currency", alt.Tooltip("WACC:Q", format=".2%")],
        )
        .properties(height=max(400, 22 * len(ind)))
    )
    st.altair_chart(chart, width="stretch")
    fmt = {c: "{:.2%}" for c in ind.columns if c not in ("Industry", "Unlevered beta", "Levered beta")}
    fmt.update({"Unlevered beta": "{:.4f}", "Levered beta": "{:.4f}"})
    st.dataframe(ind.style.format(fmt), hide_index=True, width="stretch", height=500)

with tab_assume:
    st.markdown(
        f"""
**Main assumptions**
- The project's business risk is captured by Damodaran's **U.S. industry unlevered beta**; the same beta applies
  in both currencies (a USD-based CAPM, then converted).
- Debt and the interest tax shield have **zero beta**, so the beta is relevered at the target debt share.
- The **U.S. 10-year government yield** is the USD risk-free rate; the target country's 10-year yield is its
  local-currency risk-free rate.
- The USD pre-tax cost of debt is the U.S. 10Y yield plus Damodaran's **Baa2/BBB spread** for large non-financial firms.
- Converting to the foreign currency relies on **relative PPP** (expected depreciation = inflation differential) and
  the **Fisher effect**, so the risk-free rate differential captures expected currency depreciation. No extra
  country risk premium is added.
- **One project tax rate** (U.S. or {country}, chosen in the sidebar), one debt share and one ERP are used in both currencies.
- Corporate tax rates are OECD **combined (central + sub-central) statutory** rates.
"""
    )
    st.markdown("**Formulas**")
    st.latex(r"\beta_E = \beta_U\left[1 + (1-\tau)\tfrac{D}{E}\right],\quad \tfrac{D}{E} = \tfrac{d}{1-d}")
    st.latex(r"R_e = R_f + \beta_E \times ERP, \qquad R_d = R_f + \text{BBB spread}")
    st.latex(r"(1+R_e^*) = (1+R_e)\,\frac{1+R_f^*}{1+R_f}, \qquad (1+R_d^*) = (1+R_d)\,\frac{1+R_f^*}{1+R_f}")
    st.latex(r"\frac{S_{t+1}}{S_t} = \frac{1+\pi^*}{1+\pi} \quad\text{(relative PPP)}")
    st.latex(r"WACC = (1-d)\,R_e + d\,R_d\,(1-\tau)")
    st.markdown("If the foreign BBB spread is overridden, $R_d^* = R_f^* + s^*_{manual}$. "
                "Auto foreign spread: $s^* = R_d^* - R_f^* = s\\,(1+R_f^*)/(1+R_f)$.")

with tab_src:
    st.dataframe(sources, hide_index=True, width="stretch",
                 column_config={"Source": st.column_config.LinkColumn("Source")})
    for k, r in data.items():
        note = ""
        if k == "yields" and r.status != "live" and yields_live:
            note = (" - the all-country table is behind a bot check, so the U.S. and selected-country yields "
                    "above were fetched live from their individual Trading Economics pages")
        st.caption(f"**{r.name}** - status: {r.status}; fetched {r.fetched_at}; as of {r.as_of}"
                   + (f"; live error: {r.error}" if r.error else "") + note)
    st.markdown("OECD rates are pulled from the OECD Data Explorer API (dataset `DSD_TAX_CIT@DF_CIT`), which is the "
                "data behind the *Statutory corporate income tax rates* chapter of Corporate Tax Statistics.")
    with st.expander("All countries: yields and tax rates"):
        st.dataframe(countries.style.format({"yield_10y": "{:.3%}", "tax_rate": "{:.2%}", "tax_year": "{:.0f}"}),
                     hide_index=True, width="stretch")

# ---------------------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------------------
inputs = pd.DataFrame(
    {"Input": ["Industry", "Target country", "Currency", "Project tax rate from", "Project tax rate", "ERP",
               "Debt share", "Unlevered beta", "USD BBB spread", "U.S. 10Y yield", f"{country} 10Y yield",
               "Foreign spread mode", "Generated"],
     "Value": [industry, country, ccy, tax_label, tax_rate, erp, debt_share, beta_u, spread_usd, rf_us, rf_fx,
               "manual" if ovr_spread_fx else "auto (parity)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")]}
)
numeric = pd.DataFrame(
    {"Metric": ["Risk-free rate", "Levered beta", "Cost of equity", "BBB spread", "Pre-tax cost of debt",
                "After-tax cost of debt", "WACC"],
     "USD": [rf_us, res.beta_e, res.usd.cost_of_equity, res.usd.spread, res.usd.pretax_cost_of_debt,
             res.usd.aftertax_cost_of_debt, res.usd.wacc],
     fx_col: [rf_fx, res.beta_e, res.foreign.cost_of_equity, res.foreign.spread, res.foreign.pretax_cost_of_debt,
              res.foreign.aftertax_cost_of_debt, res.foreign.wacc]}
)
xbuf = io.BytesIO()
with pd.ExcelWriter(xbuf, engine="openpyxl") as xw:
    inputs.to_excel(xw, sheet_name="Inputs", index=False)
    numeric.to_excel(xw, sheet_name="Results", index=False)
    ind.to_excel(xw, sheet_name="All industries", index=False)
    sources.to_excel(xw, sheet_name="Sources", index=False)

st.divider()
d1, d2, d3 = st.columns(3)
d1.download_button("⬇️ Results (Excel, all sheets)", xbuf.getvalue(), "cost_of_capital_results.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
d2.download_button("⬇️ Results (CSV)", numeric.to_csv(index=False).encode(), "cost_of_capital_results.csv",
                   "text/csv", width="stretch")
d3.download_button("⬇️ All-industry comparison (CSV)", ind.to_csv(index=False).encode(),
                   "all_industry_comparison.csv", "text/csv", width="stretch")
