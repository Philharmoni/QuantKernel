from core.factor.compute import rolling_price_factor


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    return rolling_price_factor(store, "amihud_20", "cast(amihud_s20 as DOUBLE)/amihud_n20",
                                "amihud_n20>=20", start, end, input_end)
