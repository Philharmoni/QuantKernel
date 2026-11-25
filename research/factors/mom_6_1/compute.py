from core.factor.compute import momentum_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return momentum_factor(store, "mom_6_1", lookback=126, skip=21,
                           start=start, end=end, input_end=input_end)
