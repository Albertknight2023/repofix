from shipping import shipping_fee


def test_small_nonmember_order_pays_shipping() -> None:
    assert shipping_fee(40, is_member=False) == 10


def test_small_member_order_has_free_shipping() -> None:
    assert shipping_fee(40, is_member=True) == 0
