"""International Cost of Capital App (Streamlit).

Compares a project's cost of equity, cost of debt and WACC in USD and in the currency of a
selected country. Run with:  streamlit run app.py
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st

from coc.calculations import compute_project, implied_foreign_spread, levered_beta
from coc.data_sources import build_country_table, fetch_all

st.set_page_config(page_title="International Cost of Capital", page_icon="🌍", layout="wide")

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"


# ---------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------
@st.cache_data(ttl=6 * 3600, show_spinner="Fetching latest data from Damodaran, OECD and Trading Economics...")
def load_data():
    return fetch_all()


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
        {"Data": "U.S. 10Y government yield", "Value": pct(rf_us, 3), "Source": data["yields"].source_url,
         "As of": us_row["yield_date"], "Status": "manual" if ovr_rf_us else data["yields"].status},
        {"Data": f"{country} 10Y government yield", "Value": pct(rf_fx, 3), "Source": data["yields"].source_url,
         "As of": crow["yield_date"], "Status": "manual" if ovr_rf_fx else data["yields"].status},
    ]
)

# ---------------------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------------------
st.title("🌍 International Cost of Capital")
st.caption(f"**{industry}** project in **{country}** · project tax rate: **{tax_label}** ({pct(tax_rate, 2)}) · "
           f"ERP {pct(erp, 1)} · debt share {pct(debt_share, 0)}")

fallbacks = [r for r in data.values() if r.status == "snapshot"]
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
    for r in data.values():
        st.caption(f"**{r.name}** - status: {r.status}; fetched {r.fetched_at}; as of {r.as_of}"
                   + (f"; live error: {r.error}" if r.error else ""))
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
    inputs.to_excel(xw, "Inputs", index=False)
    numeric.to_excel(xw, "Results", index=False)
    ind.to_excel(xw, "All industries", index=False)
    sources.to_excel(xw, "Sources", index=False)

st.divider()
d1, d2, d3 = st.columns(3)
d1.download_button("⬇️ Results (Excel, all sheets)", xbuf.getvalue(), "cost_of_capital_results.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
d2.download_button("⬇️ Results (CSV)", numeric.to_csv(index=False).encode(), "cost_of_capital_results.csv",
                   "text/csv", width="stretch")
d3.download_button("⬇️ All-industry comparison (CSV)", ind.to_csv(index=False).encode(),
                   "all_industry_comparison.csv", "text/csv", width="stretch")
