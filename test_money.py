import pytest

from pos.money import CASH_BOX, allocate_discount, settle


def test_discount_splits_proportionally():
    # Johnny $10, Paul $15, Mary $25, all sold for $30
    assert allocate_discount([1000, 1500, 2500], 3000) == [600, 900, 1500]


def test_discount_always_adds_up():
    shares = allocate_discount([100, 100, 100], 200)
    assert sum(shares) == 200
    assert sorted(shares) == [66, 67, 67]

    for total in range(0, 1001, 7):
        assert sum(allocate_discount([333, 250, 417], total)) == total


def test_discount_edge_cases():
    assert allocate_discount([500, 700], 1200) == [500, 700]
    assert allocate_discount([500, 700], 0) == [0, 0]
    with pytest.raises(ValueError):
        allocate_discount([500, 700], 1201)
    with pytest.raises(ValueError):
        allocate_discount([500, 700], -1)


def test_settle_venmo_holder_pays_out():
    # Discounted $30 sale paid to Mary's Venmo: Mary keeps her $15 and pays the rest
    owed = {"johnny": 600, "paul": 900, "mary": 1500}
    held = {"mary": 3000}
    assert settle(owed, held) == [("mary", "paul", 900), ("mary", "johnny", 600)]


def test_settle_cash_box_pays_first():
    owed = {"johnny": 4200, "mary": 1000}
    held = {CASH_BOX: 3000, "mary": 2200}
    transfers = settle(owed, held)
    assert transfers[0] == (CASH_BOX, "johnny", 3000)
    assert ("mary", "johnny", 1200) in transfers
    _check_balanced(owed, held, transfers)


def test_settle_with_refund():
    # A $20 cash sale of Paul's was refunded from the cash box
    owed = {"paul": 2000 - 2000, "johnny": 500}
    held = {CASH_BOX: 2500 - 2000}
    assert settle(owed, held) == [(CASH_BOX, "johnny", 500)]


def test_settle_nothing_to_do():
    assert settle({"mary": 1000}, {"mary": 1000}) == []


def _check_balanced(owed, held, transfers):
    final = dict(held)
    for payer, payee, cents in transfers:
        final[payer] = final.get(payer, 0) - cents
        final[payee] = final.get(payee, 0) + cents
    for person, cents in owed.items():
        assert final.get(person, 0) == cents
    assert final.get(CASH_BOX, 0) == 0
