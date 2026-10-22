import pytest

from shipping import shipping_fee


def test_threshold_is_inclusive_for_nonmembers() -> None:
    assert shipping_fee(100, is_member=False) == 0


def test_order_just_below_threshold_pays_shipping() -> None:
    assert shipping_fee(99, is_member=False) == 10


def test_large_nonmember_order_has_free_shipping() -> None:
    assert shipping_fee(150, is_member=False) == 0


def test_negative_subtotal_is_rejected_even_for_member() -> None:
    with pytest.raises(ValueError, match="subtotal"):
        shipping_fee(-1, is_member=True)
