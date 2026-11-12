"""Money math for sales and settlement. All amounts are integer cents."""

CASH_BOX = None


def allocate_discount(line_cents, total_cents):
    """Split total_cents across lines in proportion to line_cents.

    Uses the largest-remainder method so the shares always add up to total_cents
    exactly, e.g. lines of 1000/1500/2500 sold for 3000 become 600/900/1500.
    """
    subtotal = sum(line_cents)
    if any(cents < 0 for cents in line_cents):
        raise ValueError("Line amounts must not be negative")
    if total_cents < 0:
        raise ValueError("Total must not be negative")
    if total_cents > subtotal:
        raise ValueError("Total must not be more than the sum of the items")
    if total_cents == subtotal:
        return list(line_cents)

    shares = []
    remainders = []
    for index, cents in enumerate(line_cents):
        share, remainder = divmod(cents * total_cents, subtotal)
        shares.append(share)
        remainders.append((remainder, cents, -index))

    # Hand the leftover cents to the lines that lost the most to rounding.
    # Ties go to the bigger line, then the earlier one, so the result is repeatable.
    leftover = total_cents - sum(shares)
    for _, _, neg_index in sorted(remainders, reverse=True)[:leftover]:
        shares[-neg_index] += 1
    return shares


def settle(owed, held):
    """Work out who pays whom so every seller ends up with what they sold.

    owed maps seller id -> cents their items brought in.
    held maps holder -> cents paid to them, where the holder is a seller id
    (their Venmo, say) or CASH_BOX.

    Returns a list of (payer, payee, cents). The cash box pays out first, then the
    biggest balances are matched first, which keeps the number of payments small.
    """
    people = set(owed) | set(held)
    net = {person: held.get(person, 0) - owed.get(person, 0) for person in people}

    payers = sorted(
        (person for person in people if net[person] > 0),
        key=lambda person: (person is not CASH_BOX, -net[person], str(person)),
    )
    payees = sorted(
        (person for person in people if net[person] < 0),
        key=lambda person: (net[person], str(person)),
    )
    remaining = {person: abs(net[person]) for person in payers + payees}

    transfers = []
    i = j = 0
    while i < len(payers) and j < len(payees):
        payer, payee = payers[i], payees[j]
        cents = min(remaining[payer], remaining[payee])
        transfers.append((payer, payee, cents))
        remaining[payer] -= cents
        remaining[payee] -= cents
        if remaining[payer] == 0:
            i += 1
        if remaining[payee] == 0:
            j += 1
    return transfers
