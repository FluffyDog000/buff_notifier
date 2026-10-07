# Перенесено из csfloatpricesparcing/tests/test_profit.py, коммит 4a7d61c.
from notifier.estimate import estimate


def test_a_skin_is_valued_in_its_own_hundredth_when_it_can_be():
    sales = ([{"price": 100.0, "float_value": 0.155}] * 5
             + [{"price": 80.0, "float_value": 0.30}] * 20)
    price, basis = estimate(sales, 0.1534)
    assert price == 100.0 and "0.15" in basis
    price, basis = estimate(sales, 0.40)
    assert price == 80.0 and "предмета" in basis
    assert estimate([], 0.15)[0] is None
