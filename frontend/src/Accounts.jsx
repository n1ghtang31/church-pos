import { useState, useEffect } from "react";
import BlockedDevices from "./BlockedDevices";
import { sendJson } from "./format";

// The site owner's screen for organizers, invites and blocked devices
function Accounts({ api }) {
  const [users, setUsers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = async () => {
    const [userResponse, inviteResponse] = await Promise.all([api("/api/users"), api("/api/invites")]);
    if (!userResponse.ok || !inviteResponse.ok) {
      throw new Error("Could not load accounts");
    }
    setUsers(await userResponse.json());
    setInvites(await inviteResponse.json());
  };

  useEffect(() => {
    load().catch((error) => {
      console.error("Error:", error);
      setError("Could not load accounts");
    });
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const invite = async (e) => {
    e.preventDefault();
    setError("");
    setMessage("");
    try {
      const response = await api("/api/invites", sendJson("POST", { email }));
      const result = await response.json();
      if (!response.ok) {
        setError(result.error || "Could not send the invite");
        return;
      }
      setMessage(`Invite sent to ${result.email}`);
      setEmail("");
      await load();
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
  };

  const cancel = async (pending) => {
    if (!window.confirm(`Cancel the invite to ${pending.email}?`)) return;
    await api(`/api/invites/${pending.id}`, { method: "DELETE" });
    await load();
  };

  return (
    <div className="cashiers">
      {error && <div className="login-error">{error}</div>}
      {message && <div className="message">{message}</div>}

      <section className="report-section">
        <h3>Invite an Organizer</h3>
        <form className="cashier-form" onSubmit={invite}>
          <label>
            Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <button type="submit" className="tab-button">
            Send Invite
          </button>
        </form>
        {invites.map((pending) => (
          <div key={pending.id} className="cashier-row">
            <div>
              <span className="cashier-name">{pending.email}</span>
              <div className="report-note">
                Invited, expires {new Date(pending.expires_at).toLocaleDateString()}
              </div>
            </div>
            <div className="cashier-actions">
              <button className="tab-button" onClick={() => cancel(pending)}>
                Cancel
              </button>
            </div>
          </div>
        ))}
      </section>

      <section className="report-section">
        <h3>Organizers</h3>
        {users.map((user) => (
          <div key={user.id} className="cashier-row">
            <div>
              <span className="cashier-name">{user.name}</span>
              <div className="report-note">
                {user.email}
                {user.is_owner && ", site owner"}, {user.events}{" "}
                {user.events === 1 ? "event" : "events"}
              </div>
            </div>
          </div>
        ))}
      </section>

      <BlockedDevices api={api} />
    </div>
  );
}

export default Accounts;
