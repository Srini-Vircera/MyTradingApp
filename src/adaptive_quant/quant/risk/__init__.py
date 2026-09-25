"""Independent risk engine (Milestone 7) with final authority over proposed portfolios.

See ``docs/RISK_MANAGEMENT.md``. Any failure inside a rule raises
:class:`~adaptive_quant.quant.risk.models.RiskCalculationError`; callers must
refuse the cycle (fail closed).
"""
