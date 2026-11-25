from core.factor.compute import quarterly_yoy_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return quarterly_yoy_factor(store, "np_yoy_q", "n_income_attr_p", start, end, input_end)
