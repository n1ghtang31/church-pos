import { useState, useEffect } from "react";
import { MODE_LABELS, PRICES, sendJson } from "./format";

function Events({ api, onOpen, showOwner }) {
  const [events, setEvents] = useState([]);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  // "free", or the mode of a paid event
  const [kind, setKind] = useState("free");

  const load = async () => {
    const response = await api("/api/events");
    if (!response.ok) {
      throw new Error(`Status ${response.status}`);
    }
    setEvents(await response.json());
  };

  useEffect(() => {
    load().catch((error) => {
      console.error("Error:", error);
      setError("Could not load events");
    });
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const create = async (e) => {
    e.preventDefault();
    setError("");
    try {
      const body = kind === "free" ? { name, plan: "free" } : { name, plan: "paid", mode: kind };
      const response = await api("/api/events", sendJson("POST", body));
      const result = await response.json();
      if (!response.ok) {
        setError(result.error || "Could not create the event");
        return;
      }
      setName("");
      // Go straight to setting it up
      onOpen(result.id, "setup");
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
  };

  return (
    <div className="cashiers">
      {error && <div className="login-error">{error}</div>}

      <section className="report-section">
        <h3>New Event</h3>
        <form className="cashier-form" onSubmit={create}>
          <label>
            Name
            <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Kind
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="free">Free sale (just you, cleared at midnight)</option>
              {Object.entries(MODE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                  {showOwner ? "" : ` (${PRICES[value]})`}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className="tab-button">
            Create Event
          </button>
        </form>
      </section>

      <section className="report-section">
        <h3>Events</h3>
        {events.length === 0 && <p>No events yet</p>}
        {events.map((event) => (
          <div key={event.id} className="cashier-row">
            <div>
              <span className="cashier-name">{event.name}</span>
              <div className="report-note">
                {event.plan === "free" ? "Free sale" : MODE_LABELS[event.mode]}
                {!event.paid && ", not paid yet"}, code {event.code},{" "}
                {event.status === "open" ? "open" : "closed"}, created{" "}
                {new Date(event.created_at).toLocaleDateString()}
                {showOwner && `, ${event.owner_email ?? "no owner"}`}
              </div>
            </div>
            <div className="cashier-actions">
              <button className="tab-button" onClick={() => onOpen(event.id, "reports")}>
                Open
              </button>
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}

export default Events;
