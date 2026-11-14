"""Pure cost-of-capital calculations (no Streamlit, no network).

All rates are decimals (0.065 = 6.5%).

Formulas (from class slides)
----------------------------
Relevering (debt and tax shield have zero beta):
    beta_E = beta_U * [1 + (1 - tau) * D/E],   D/E = d / (1 - d)

CAPM, USD:
    R_e = R_f + beta_E * ERP
    R_d = R_f + BBB spread  (pre-tax)

Foreign currency (relative PPP + Fisher -> interest-rate parity on expected returns):
    (1 + R_e*) = (1 + R_e) * (1 + R_f*) / (1 + R_f)
    (1 + R_d*) = (1 + R_d) * (1 + R_f*) / (1 + R_f)     [auto foreign debt cost]
    => implied foreign spread s* = R_d* - R_f* = s * (1 + R_f*) / (1 + R_f)

WACC (same project tax rate and debt share in both currencies):
    WACC = (1 - d) * R_e + d * R_d * (1 - tau)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


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
