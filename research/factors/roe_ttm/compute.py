from core.factor.compute import ttm_balance_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return ttm_balance_factor(store, "roe_ttm", "total_hldr_eqy_exc_min_int", start, end, input_end)
