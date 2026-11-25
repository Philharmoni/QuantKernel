from core.factor.compute import rolling_price_factor

EXPRESSION = ("sqrt(greatest(cast(ret_s2_20 as DOUBLE)/ret_n20"
              "-pow(cast(ret_s1_20 as DOUBLE)/ret_n20,2),0))")


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return rolling_price_factor(store, "vol_20", EXPRESSION, "ret_n20>=20", start, end, input_end)
