import { useState, useEffect } from "react";
import { sendJson } from "./format";

// Pages reached from an emailed link, before anyone is logged in

async function postJson(path, body) {
  const response = await fetch(path, sendJson("POST", body));
  return { ok: response.ok, result: await response.json() };
}

export function AcceptInvite({ token, onLogin }) {
  const [email, setEmail] = useState(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetch(`/api/invites/accept/${token}`)
      .then(async (response) => {
        const result = await response.json();
        if (response.ok) setEmail(result.email);
        else setError(result.error || "This invite isn't valid");
      })
      .catch(() => setError("Error connecting to server"));
  }, [token]);

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const { ok, result } = await postJson(`/api/invites/accept/${token}`, { name, password });
      if (ok) {
        window.history.replaceState(null, "", "/");
        onLogin(result);
        return;
      }
      setError(result.error || "Could not create your account");
    } catch {
      setError("Error connecting to server");
    }
    setSubmitting(false);
  };

  return (
    <div className="login-container">
      <h1>Sale POS</h1>
      <form className="login-form" onSubmit={submit}>
        {email && <p className="report-note">Creating an account for {email}</p>}
        <label htmlFor="name">Your name</label>
        <input id="name" maxLength={50} value={name} onChange={(e) => setName(e.target.value)} />
        <label htmlFor="password">Password (at least 10 characters)</label>
        <input
          id="password"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit" className="checkout-btn" disabled={submitting || !email}>
          Create Account
        </button>
        {error && <div className="login-error">{error}</div>}
      </form>
    </div>
  );
}

export function ResetPassword({ token }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    try {
      const { ok, result } = await postJson(`/api/password-reset/${token}`, { password });
      if (ok) setDone(true);
      else setError(result.error || "Could not change your password");
    } catch {
      setError("Error connecting to server");
    }
  };

  return (
    <div className="login-container">
      <h1>Sale POS</h1>
      {done ? (
        <div className="login-form">
          <p>Your password is changed.</p>
          <a className="checkout-btn" href="/">
            Log In
          </a>
        </div>
      ) : (
        <form className="login-form" onSubmit={submit}>
          <label htmlFor="password">New password (at least 10 characters)</label>
          <input
            id="password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button type="submit" className="checkout-btn" disabled={!password}>
            Change Password
          </button>
          {error && <div className="login-error">{error}</div>}
        </form>
      )}
    </div>
  );
}
