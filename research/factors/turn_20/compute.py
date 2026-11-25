from core.factor.compute import rolling_price_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return rolling_price_factor(store, "turn_20", "cast(turn_s20 as DOUBLE)/turn_n20",
                                "turn_n20>=20", start, end, input_end)
