-- Every row belongs to an event and goes when its event does, so purging an
-- event is a single DELETE. Money is integer cents.

CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('charity', 'yard_sale')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    timezone TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    purge_after TIMESTAMPTZ
);

CREATE TABLE items (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_items_event ON items(event_id);

CREATE TABLE sellers (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_sellers_event ON sellers(event_id);

-- holder_seller_id is whose account the money lands in (Mary's Venmo).
-- NULL means the cash box, or the organizer for a charity drive.
CREATE TABLE payment_methods (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('cash', 'check', 'venmo', 'card', 'other')),
    holder_seller_id BIGINT REFERENCES sellers(id),
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_payment_methods_event ON payment_methods(event_id);

CREATE TABLE cashiers (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    UNIQUE (event_id, pin_hash)
);
CREATE UNIQUE INDEX idx_cashiers_event_name ON cashiers(event_id, lower(name));

-- A refund is its own sale with negative amounts, linked to the original.
-- The unique constraint means a sale can only ever be refunded once.
CREATE TABLE sales (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    cashier_name TEXT NOT NULL,
    payment_method_id BIGINT NOT NULL REFERENCES payment_methods(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    subtotal_cents BIGINT NOT NULL,
    total_cents BIGINT NOT NULL,
    refund_of BIGINT UNIQUE REFERENCES sales(id)
);
CREATE INDEX idx_sales_event ON sales(event_id, created_at);

-- final_cents is the line's share of the sale total after any discount
CREATE TABLE sale_lines (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    item_id BIGINT REFERENCES items(id),
    seller_id BIGINT REFERENCES sellers(id),
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    list_cents BIGINT NOT NULL,
    final_cents BIGINT NOT NULL
);
CREATE INDEX idx_sale_lines_sale ON sale_lines(sale_id);
CREATE INDEX idx_sale_lines_event ON sale_lines(event_id);

-- Kept in the database so bans survive a restart
CREATE TABLE login_failures (
    ip TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    last_failure DOUBLE PRECISION NOT NULL,
    banned BOOLEAN NOT NULL DEFAULT FALSE,
    locked_until DOUBLE PRECISION
);
