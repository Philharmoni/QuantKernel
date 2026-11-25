from core.factor.compute import quarterly_yoy_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return quarterly_yoy_factor(store, "rev_yoy_q", "revenue", start, end, input_end)
