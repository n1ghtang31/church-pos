import { useState } from "react";

function Login({ onLogin }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
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
      <h1>Church Charity Drive POS</h1>
      <form className="login-form" onSubmit={submit}>
        <label htmlFor="code">Enter your code</label>
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
