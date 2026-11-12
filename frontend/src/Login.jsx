import { useState } from "react";

// A cashier's link is /e/<event code>, which fills in the event for them
const linkedEventCode = () => {
  const match = /^\/e\/([A-Za-z0-9]+)\/?$/.exec(window.location.pathname);
  return match ? match[1].toUpperCase() : "";
};

function Login({ onLogin }) {
  const [eventCode, setEventCode] = useState(linkedEventCode);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const linked = linkedEventCode() !== "";

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, event_code: eventCode.trim() || null }),
      });
      const result = await response.json();
      if (response.ok) {
        onLogin(result);
        return;
      }
      setError(result.error || "Could not log in");
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
    setCode("");
    setSubmitting(false);
  };

  return (
    <div className="login-container">
      <h1>Sale POS</h1>
      <form className="login-form" onSubmit={submit}>
        {linked ? (
          <p className="report-note">Event {eventCode}</p>
        ) : (
          <>
            <label htmlFor="event-code">Event code (blank for admin)</label>
            <input
              id="event-code"
              autoComplete="off"
              autoCapitalize="characters"
              maxLength={12}
              value={eventCode}
              onChange={(e) => setEventCode(e.target.value)}
            />
          </>
        )}
        <label htmlFor="code">Enter your PIN</label>
        <input
          id="code"
          type="password"
          inputMode="numeric"
          autoComplete="off"
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value)}
        />
        <button
          type="submit"
          className="checkout-btn"
          disabled={submitting || !code}
        >
          Log In
        </button>
        {error && <div className="login-error">{error}</div>}
      </form>
    </div>
  );
}

export default Login;
