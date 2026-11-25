from core.factor.compute import simple_price_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return simple_price_factor(store, "log_total_mv", "ln(total_mv*10000)",
                               "total_mv>0 AND isfinite(total_mv)", start, end, input_end)
