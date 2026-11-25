from core.factor.compute import simple_price_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return simple_price_factor(store, "log_circ_mv", "ln(circ_mv*10000)",
                               "circ_mv>0 AND isfinite(circ_mv)", start, end, input_end)
