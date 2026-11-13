import { useState } from "react";
import { sendJson } from "./format";

// A cashier's link is /e/<event code>, which fills in the event for them
const linkedEventCode = () => {
  const match = /^\/e\/([A-Za-z0-9]+)\/?$/.exec(window.location.pathname);
  return match ? match[1].toUpperCase() : "";
};

function Login({ onLogin }) {
  const linked = linkedEventCode() !== "";
  const [organizer, setOrganizer] = useState(!linked);
  const [forgot, setForgot] = useState(false);
  const [signingUp, setSigningUp] = useState(window.location.pathname === "/signup");
  const [unverified, setUnverified] = useState(false);
  const [name, setName] = useState("");
  const [eventCode, setEventCode] = useState(linkedEventCode);
  const [code, setCode] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    setMessage("");
    setUnverified(false);
    try {
      if (signingUp) {
        const response = await fetch("/api/signup", sendJson("POST", { email, name, password }));
        const result = await response.json();
        if (response.ok) {
          setMessage(result.message);
          setSigningUp(false);
          setPassword("");
        } else {
          setError(result.error || "Could not sign up");
        }
      } else if (forgot) {
        const response = await fetch("/api/password-reset", sendJson("POST", { email }));
        setMessage((await response.json()).message);
        setForgot(false);
      } else {
        const body = organizer ? { email, password } : { code, event_code: eventCode.trim() };
        const response = await fetch("/api/login", sendJson("POST", body));
        const result = await response.json();
        if (response.ok) {
          onLogin(result);
          return;
        }
        setError(result.error || "Could not log in");
        setUnverified(Boolean(result.unverified));
        setCode("");
        setPassword("");
      }
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
    setSubmitting(false);
  };

  const resend = async () => {
    const response = await fetch("/api/signup/resend", sendJson("POST", { email }));
    setError("");
    setUnverified(false);
    setMessage((await response.json()).message);
  };

  const switchTo = (toOrganizer) => {
    setOrganizer(toOrganizer);
    setForgot(false);
    setSigningUp(false);
    setError("");
    setMessage("");
  };

  return (
    <div className="login-container">
      <h1>SplitTheSale</h1>
      {!linked && (
        <div className="department-tabs">
          <button className={`tab-button ${!organizer ? "active" : ""}`} onClick={() => switchTo(false)}>
            Cashier
          </button>
          <button className={`tab-button ${organizer ? "active" : ""}`} onClick={() => switchTo(true)}>
            Organizer
          </button>
        </div>
      )}
      <form className="login-form" onSubmit={submit}>
        {organizer ? (
          <>
            {signingUp && (
              <>
                <label htmlFor="name">Your name</label>
                <input id="name" maxLength={50} value={name} onChange={(e) => setName(e.target.value)} />
              </>
            )}
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            {!forgot && (
              <>
                <label htmlFor="password">
                  {signingUp ? "Password (at least 10 characters)" : "Password"}
                </label>
                <input
                  id="password"
                  type="password"
                  autoComplete={signingUp ? "new-password" : "current-password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </>
            )}
            <button
              type="submit"
              className="checkout-btn"
              disabled={submitting || !email || (!forgot && !password) || (signingUp && !name)}
            >
              {signingUp ? "Sign Up" : forgot ? "Email Me a Reset Link" : "Log In"}
            </button>
            {!signingUp && (
              <button type="button" className="tab-button" onClick={() => setForgot(!forgot)}>
                {forgot ? "Back to Log In" : "Forgot password?"}
              </button>
            )}
            <button
              type="button"
              className="tab-button"
              onClick={() => {
                setSigningUp(!signingUp);
                setForgot(false);
                setError("");
              }}
            >
              {signingUp ? "Have an account? Log in" : "New here? Sign up"}
            </button>
          </>
        ) : (
          <>
            {linked ? (
              <p className="report-note">Event {eventCode}</p>
            ) : (
              <>
                <label htmlFor="event-code">Event code</label>
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
              disabled={submitting || !code || !eventCode.trim()}
            >
              Log In
            </button>
          </>
        )}
        {error && <div className="login-error">{error}</div>}
        {unverified && (
          <button type="button" className="tab-button" onClick={resend}>
            Send the link again
          </button>
        )}
        {message && <div className="message">{message}</div>}
      </form>
      <p className="report-note privacy-note">
        Sales are kept only while an event runs and for 6 months after it closes.
        Then the event and everything in it is permanently deleted. Organizers get a
        copy by email when their event closes.
      </p>
    </div>
  );
}

export default Login;
