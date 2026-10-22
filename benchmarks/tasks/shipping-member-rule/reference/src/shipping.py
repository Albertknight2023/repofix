"""Small order-shipping rules."""


def shipping_fee(subtotal: int, is_member: bool) -> int:
    if subtotal < 0:
        raise ValueError("subtotal must not be negative")
    if subtotal >= 100 or is_member:
        return 0
    return 10
