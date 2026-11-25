from core.factor.compute import ttm_market_cap_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return ttm_market_cap_factor(store, "ep_ttm", "n_income_attr_p", start, end, input_end)
