import { useState, useRef } from "react";
import { allocateDiscount, money, sendJson, toCents } from "./format";

function NumberPad({ title, showNote, onAdd, onCancel }) {
  const [input, setInput] = useState("");
  const [note, setNote] = useState("");

  // Allow only one decimal point and two decimal places
  const press = (key) =>
    setInput((prev) => (/^\d*(\.\d{0,2})?$/.test(prev + key) ? prev + key : prev));

  const add = () => {
    const cents = toCents(input);
    if (!cents || cents <= 0) {
      alert("Please enter a valid amount");
      return;
    }
    onAdd(cents, note.trim());
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <h2>{title}</h2>

        <div className="price-display">
          <span className="dollar-sign">$</span>
          <span className="price-value">{input || "0.00"}</span>
        </div>

        {showNote && (
          <input
            className="line-note"
            placeholder="What is it? (optional)"
            maxLength={80}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        )}

        <div className="number-pad">
          {["7", "8", "9", "4", "5", "6", "1", "2", "3"].map((key) => (
            <button key={key} onClick={() => press(key)}>
              {key}
            </button>
          ))}
          <button onClick={() => setInput("")} className="clear-btn">
            C
          </button>
          <button onClick={() => press("0")}>0</button>
          <button onClick={() => press(".")}>.</button>
        </div>

        <div className="modal-actions">
          <button onClick={() => setInput((prev) => prev.slice(0, -1))} className="backspace-btn">
            ← Backspace
          </button>
          <button onClick={onCancel} className="cancel-btn">
            Cancel
          </button>
          <button onClick={add} className="add-btn">
            OK
          </button>
        </div>
      </div>
    </div>
  );
}

function Register({ api, event }) {
  const yardSale = event.mode === "yard_sale";
  const [cart, setCart] = useState([]);
  const [offerCents, setOfferCents] = useState(null);
  const [methodId, setMethodId] = useState(event.payment_methods[0]?.id ?? null);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const checkoutInFlight = useRef(false);
  // What the number pad is entering: { line: item or seller } or { offer: true }
  const [padFor, setPadFor] = useState(null);

  const departments = [...new Set(event.items.map((item) => item.department))];
  const [activeTab, setActiveTab] = useState(departments[0] ?? "");
  const choices = yardSale
    ? event.sellers
    : event.items.filter((item) => item.department === activeTab);

  const lineTotals = cart.map((line) => line.price_cents * line.quantity);
  const subtotal = lineTotals.reduce((sum, cents) => sum + cents, 0);
  const total = offerCents ?? subtotal;
  const shares = allocateDiscount(lineTotals, total);

  // Each seller's share of the sale, shown when an offer lowers the total
  const sellerShares = {};
  cart.forEach((line, index) => {
    sellerShares[line.name] = (sellerShares[line.name] ?? 0) + shares[index];
  });

  const changeCart = (newCart) => {
    setCart(newCart);
    // An offer was for the old cart, so take it again for the new one
    setOfferCents(null);
    setMessage("");
  };

  const addLine = (choice, cents, note) => {
    changeCart([
      ...cart,
      { key: Date.now(), refId: choice.id, name: choice.name, description: note, price_cents: cents, quantity: 1 },
    ]);
  };

  const updateQuantity = (key, quantity) => {
    changeCart(
      quantity <= 0
        ? cart.filter((line) => line.key !== key)
        : cart.map((line) => (line.key === key ? { ...line, quantity } : line)),
    );
  };

  const setOffer = (cents) => {
    if (cents > subtotal) {
      setMessage("An offer can't be more than the items add up to");
      return;
    }
    setOfferCents(cents);
    setMessage("");
  };

  const checkout = async () => {
    // A ref updates immediately, so a double click can't record the sale twice
    if (checkoutInFlight.current) return;
    if (cart.length === 0) {
      setMessage("Cart is empty!");
      return;
    }

    const sale = {
      payment_method_id: methodId,
      lines: cart.map((line) => ({
        [yardSale ? "seller_id" : "item_id"]: line.refId,
        description: line.description,
        quantity: line.quantity,
        price_cents: line.price_cents,
      })),
      total_cents: total,
    };

    checkoutInFlight.current = true;
    setSubmitting(true);
    try {
      const response = await api(`/api/events/${event.id}/sales`, sendJson("POST", sale));
      const result = await response.json();
      if (response.ok) {
        setCart([]);
        setOfferCents(null);
        setMessage(`Sale completed! Total: ${money(result.total_cents)}`);
      } else {
        setMessage(result.error || "Error processing sale");
      }
    } catch (error) {
      console.error("Error:", error);
      setMessage("Error connecting to server");
    } finally {
      checkoutInFlight.current = false;
      setSubmitting(false);
    }
  };

  if (event.status !== "open") {
    return <p className="login-error">This event is closed, so no more sales can be rung up.</p>;
  }

  return (
    <div className="pos-layout">
      <div className="items-section">
        <h2>{yardSale ? "Whose is it?" : "Items"}</h2>

        {!yardSale && (
          <div className="department-tabs">
            {departments.map((dept) => (
              <button
                key={dept}
                className={`tab-button ${activeTab === dept ? "active" : ""}`}
                onClick={() => setActiveTab(dept)}
              >
                {dept || "Items"}
              </button>
            ))}
          </div>
        )}

        {choices.length === 0 && (
          <p>No {yardSale ? "sellers" : "items"} yet. The admin adds them under Setup.</p>
        )}
        <div className="items-grid">
          {choices.map((choice) => (
            <div key={choice.id} className="item-card">
              <h3>{choice.name}</h3>
              <br></br>
              <button onClick={() => setPadFor({ line: choice })}>Select</button>
            </div>
          ))}
        </div>
      </div>

      <div className="cart-section">
        <h2>Cart</h2>
        {cart.length === 0 ? (
          <p>No items in cart</p>
        ) : (
          <div className="cart-items">
            {cart.map((line) => (
              <div key={line.key} className="cart-item">
                <div className="cart-item-info">
                  <span className="cart-item-name">
                    {line.name}
                    {line.description && ` (${line.description})`}
                  </span>
                  <span className="cart-item-price">{money(line.price_cents)}</span>
                </div>
                <div className="cart-item-controls">
                  <button onClick={() => updateQuantity(line.key, line.quantity - 1)}>-</button>
                  <span className="quantity">{line.quantity}</span>
                  <button onClick={() => updateQuantity(line.key, line.quantity + 1)}>+</button>
                  <button onClick={() => updateQuantity(line.key, 0)} className="remove-btn">
                    Remove
                  </button>
                </div>
                <div className="cart-item-subtotal">
                  Subtotal: {money(line.price_cents * line.quantity)}
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="cart-total">
          {offerCents !== null && (
            <p className="report-note">
              Items add up to {money(subtotal)}. Offer saves {money(subtotal - offerCents)}.
            </p>
          )}
          <h3>Total: {money(total)}</h3>
          {cart.length > 0 && (
            <div className="cashier-actions">
              <button className="tab-button" onClick={() => setPadFor({ offer: true })}>
                Set Total
              </button>
              {offerCents !== null && (
                <button className="tab-button" onClick={() => setOfferCents(null)}>
                  Clear Offer
                </button>
              )}
            </div>
          )}
          {yardSale && offerCents !== null && (
            <ul className="offer-split">
              {Object.entries(sellerShares).map(([name, cents]) => (
                <li key={name}>
                  {name}: {money(cents)}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="tender-section">
          <h3>Payment Method</h3>
          <div className="tender-options">
            {event.payment_methods.map((method) => (
              <label key={method.id}>
                <input
                  type="radio"
                  checked={methodId === method.id}
                  onChange={() => setMethodId(method.id)}
                />
                {method.label}
              </label>
            ))}
          </div>
        </div>

        <button
          className="checkout-btn"
          onClick={checkout}
          disabled={submitting || methodId === null}
        >
          {submitting ? "Processing..." : "Complete Sale"}
        </button>

        {message && <div className="message">{message}</div>}
      </div>

      {padFor && (
        <NumberPad
          title={padFor.offer ? "Total for everything" : `Price for ${padFor.line.name}`}
          showNote={yardSale && !padFor.offer}
          onCancel={() => setPadFor(null)}
          onAdd={(cents, note) => {
            if (padFor.offer) setOffer(cents);
            else addLine(padFor.line, cents, note);
            setPadFor(null);
          }}
        />
      )}
    </div>
  );
}

export default Register;
