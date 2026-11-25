from core.factor.compute import momentum_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return momentum_factor(store, "rev_20", lookback=20, start=start, end=end, input_end=input_end)
