from core.factor.compute import ttm_balance_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return ttm_balance_factor(store, "roa_ttm", "total_assets", start, end, input_end)
