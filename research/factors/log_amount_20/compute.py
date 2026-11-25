from core.factor.compute import rolling_price_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return rolling_price_factor(store, "log_amount_20", "ln(cast(amount_s20 as DOUBLE)/amount_n20)",
                                "amount_n20>=20 AND amount_s20>0", start, end, input_end)
