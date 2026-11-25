from core.factor.compute import balance_market_cap_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return balance_market_cap_factor(store, "bp", "total_hldr_eqy_exc_min_int", start, end, input_end)
