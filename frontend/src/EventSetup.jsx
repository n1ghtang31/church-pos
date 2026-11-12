import { useState } from "react";
import Cashiers from "./Cashiers";
import { MODE_LABELS, sendJson } from "./format";

const KINDS = ["cash", "check", "venmo", "card", "other"];

function EventSetup({ api, event, onChanged, onDeleted }) {
  const yardSale = event.mode === "yard_sale";
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
        ? "Close this event? Cashiers won't be able to ring up sales or refunds until it's reopened."
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

  const sellerName = (id) => event.sellers.find((s) => s.id === id)?.name;
  const rows = yardSale ? event.sellers : event.items;

  return (
    <div className="cashiers">
      {error && <div className="login-error">{error}</div>}

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
        <p className="report-note">
          {MODE_LABELS[event.mode]}. Cashiers log in at <strong>{link}</strong> (event
          code {event.code}).
        </p>
        <div className="cashier-actions">
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

      <section className="report-section">
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
      </section>

      <section className="report-section">
        <h3>Payment Methods</h3>
        {yardSale && (
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
          {yardSale && (
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
              {yardSale && (
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

      <h3>Cashiers</h3>
      <Cashiers api={api} eventId={event.id} />
    </div>
  );
}

export default EventSetup;
