import { useState } from "react";
import Cashiers from "./Cashiers";
import { MODE_LABELS, PRICES, sendJson } from "./format";

const KINDS = ["cash", "check", "venmo", "card", "other"];

function EventSetup({ api, event, isOwner, onChanged, onDeleted }) {
  const yardSale = event.mode === "yard_sale";
  const free = event.plan === "free";
  const [paying, setPaying] = useState(false);
  const base = `/api/events/${event.id}`;
  const link = `${window.location.origin}/e/${event.code}`;
  const [error, setError] = useState("");
  const [name, setName] = useState(event.name);
  const [newName, setNewName] = useState("");
  const [department, setDepartment] = useState("");
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState("venmo");
  const [holder, setHolder] = useState("");

  // Send a change, then reload the event. Returns true if it worked.
  const request = async (path, options) => {
    setError("");
    try {
      const response = await api(path, options);
      if (!response.ok) {
        const result = await response.json();
        setError(result.error || "Something went wrong");
        return false;
      }
      await onChanged();
      return true;
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
      return false;
    }
  };

  const rename = (e) => {
    e.preventDefault();
    request(base, sendJson("PUT", { name }));
  };

  const addRow = async (e) => {
    e.preventDefault();
    const body = yardSale ? { name: newName } : { name: newName, department };
    if (await request(`${base}/${yardSale ? "sellers" : "items"}`, sendJson("POST", body))) {
      setNewName("");
    }
  };

  const addMethod = async (e) => {
    e.preventDefault();
    const body = { label, kind, holder_seller_id: holder ? Number(holder) : null };
    if (await request(`${base}/payment-methods`, sendJson("POST", body))) {
      setLabel("");
      setHolder("");
    }
  };

  const remove = (path, what) => {
    if (window.confirm(`Remove ${what}? Past sales keep it.`)) {
      request(path, { method: "DELETE" });
    }
  };

  const setStatus = (action) => {
    const warning =
      action === "close"
        ? "Close this event? Cashiers can't ring up sales or refunds until it's reopened. You'll get the final report by email, and the event and its sales will be deleted 6 months from now."
        : "Reopen this event for sales?";
    if (window.confirm(warning)) {
      request(`${base}/${action}`, { method: "POST" });
    }
  };

  const deleteEvent = async () => {
    const typed = window.prompt(
      `This permanently deletes "${event.name}" and every sale in it. Type the event name to confirm.`,
    );
    if (typed !== event.name) return;
    setError("");
    const response = await api(base, { method: "DELETE" });
    if (response.ok) onDeleted();
    else setError("Could not delete the event");
  };

  const pay = async () => {
    setPaying(true);
    setError("");
    try {
      const response = await api(`${base}/checkout`, { method: "POST" });
      const result = await response.json();
      if (response.ok) {
        // Stripe's payment page brings them back to /app?paid=<event id>
        window.location.href = result.url;
        return;
      }
      setError(result.error || "Could not start the payment");
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
    setPaying(false);
  };

  const sellerName = (id) => event.sellers.find((s) => s.id === id)?.name;
  const rows = yardSale ? event.sellers : event.items;

  return (
    <div className="cashiers">
      {error && <div className="login-error">{error}</div>}

      {!event.paid && (
        <section className="report-section pay-banner">
          <h3>Pay to start selling</h3>
          <p className="report-note">
            Set everything up first if you like. Cashiers can't ring up sales until the
            event is paid for. If you just paid, it can take a few seconds to show here.
          </p>
          <div className="cashier-actions">
            <button className="checkout-btn" onClick={pay} disabled={paying}>
              {paying ? "Opening Stripe..." : `Pay ${PRICES[event.mode]} with Stripe`}
            </button>
            <button className="tab-button" onClick={onChanged}>
              Check Again
            </button>
            {isOwner && (
              <button
                className="tab-button"
                onClick={() => request(`${base}/mark-paid`, { method: "POST" })}
              >
                Give It Free
              </button>
            )}
          </div>
        </section>
      )}

      <section className="report-section">
        <h3>Event</h3>
        <form className="cashier-form" onSubmit={rename}>
          <label>
            Name
            <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
          </label>
          <button type="submit" className="tab-button" disabled={name === event.name}>
            Rename
          </button>
        </form>
        {free ? (
          <p className="report-note">
            Free sale. You ring up the sales yourself, and everything is deleted at{" "}
            {new Date(event.purge_after).toLocaleString()}.
          </p>
        ) : (
          <p className="report-note">
            {MODE_LABELS[event.mode]}. Cashiers log in at <strong>{link}</strong> (event
            code {event.code}).
          </p>
        )}
        {!free && event.purge_after && (
          <p className="report-note">
            Closed. This event and every sale in it will be permanently deleted on{" "}
            {new Date(event.purge_after).toLocaleDateString()}. Download a copy to keep it.
          </p>
        )}
        <div className="cashier-actions">
          {!free && (
            <a className="tab-button" href={`${base}/sales.csv`}>
              Download Sales CSV
            </a>
          )}
          {yardSale && !free && (
            <a className="tab-button" href={`${base}/payouts.csv`}>
              Download Payouts CSV
            </a>
          )}
          {event.status === "open" ? (
            <button className="tab-button" onClick={() => setStatus("close")}>
              Close Event
            </button>
          ) : (
            <button className="tab-button" onClick={() => setStatus("reopen")}>
              Reopen Event
            </button>
          )}
          <button className="tab-button" onClick={deleteEvent}>
            Delete Event
          </button>
        </div>
      </section>

      {!free && <section className="report-section">
        <h3>{yardSale ? "Sellers" : "Items"}</h3>
        {yardSale && (
          <p className="report-note">The people or families whose things are for sale.</p>
        )}
        <form className="cashier-form" onSubmit={addRow}>
          <label>
            Name
            <input value={newName} maxLength={50} onChange={(e) => setNewName(e.target.value)} />
          </label>
          {!yardSale && (
            <label>
              Department (optional)
              <input
                value={department}
                maxLength={50}
                onChange={(e) => setDepartment(e.target.value)}
              />
            </label>
          )}
          <button type="submit" className="tab-button">
            Add
          </button>
        </form>
        {rows.length === 0 && <p>None yet</p>}
        {rows.map((row) => (
          <div key={row.id} className="cashier-row">
            <span className="cashier-name">
              {row.name}
              {row.department && <span className="report-note"> ({row.department})</span>}
            </span>
            <div className="cashier-actions">
              <button
                className="tab-button"
                onClick={() => remove(`${base}/${yardSale ? "sellers" : "items"}/${row.id}`, row.name)}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </section>}

      <section className="report-section">
        <h3>Payment Methods</h3>
        {yardSale && !free && (
          <p className="report-note">
            Pick whose account the money goes to, such as Mary's Venmo. Money paid
            to the cash box is shared out at the end.
          </p>
        )}
        <form className="cashier-form" onSubmit={addMethod}>
          <label>
            Label
            <input value={label} maxLength={50} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <label>
            Kind
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>
          {yardSale && !free && (
            <label>
              Money goes to
              <select value={holder} onChange={(e) => setHolder(e.target.value)}>
                <option value="">Cash box</option>
                {event.sellers.map((seller) => (
                  <option key={seller.id} value={seller.id}>
                    {seller.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="submit" className="tab-button">
            Add
          </button>
        </form>
        {event.payment_methods.map((method) => (
          <div key={method.id} className="cashier-row">
            <span className="cashier-name">
              {method.label}
              {yardSale && !free && (
                <span className="report-note">
                  {" "}
                  (goes to {sellerName(method.holder_seller_id) ?? "cash box"})
                </span>
              )}
            </span>
            <div className="cashier-actions">
              <button
                className="tab-button"
                onClick={() => remove(`${base}/payment-methods/${method.id}`, method.label)}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </section>

      {!free && (
        <>
          <h3>Cashiers</h3>
          <Cashiers api={api} eventId={event.id} />
        </>
      )}
    </div>
  );
}

export default EventSetup;
