import { useState, useEffect, useCallback } from "react";
import "./App.css";
import Accounts from "./Accounts";
import EventSetup from "./EventSetup";
import Events from "./Events";
import Journal from "./Journal";
import { AcceptInvite, ResetPassword } from "./LinkPages";
import Login from "./Login";
import Register from "./Register";
import Reports from "./Reports";
import Settlement from "./Settlement";
import SharedReport from "./SharedReport";

// Emailed links that open their own page: /invite/<token>, /reset/<token> and /r/<token>
const linkPage = () => {
  const match = /^\/(invite|reset|r)\/([^/]+)\/?$/.exec(window.location.pathname);
  return match ? { page: match[1], token: match[2] } : null;
};

function App() {
  const [link, setLink] = useState(linkPage);
  const [authenticated, setAuthenticated] = useState(null);
  const [user, setUser] = useState(null);
  const [view, setView] = useState("register");
  // The event being worked on. A cashier always has theirs; an organizer picks one.
  const [eventId, setEventId] = useState(null);
  const [event, setEvent] = useState(null);
  const [error, setError] = useState("");

  // Send the user back to the login screen if their session has expired
  const api = useCallback(async (path, options) => {
    const response = await fetch(path, options);
    if (response.status === 401) {
      setAuthenticated(false);
    }
    return response;
  }, []);

  const startSession = (session) => {
    setUser(session);
    setEventId(session.event_id ?? null);
    setView(session.role === "organizer" ? "events" : "register");
    setAuthenticated(session.authenticated);
  };

  useEffect(() => {
    if (link) {
      return;
    }
    fetch("/api/session")
      .then((response) => response.json())
      .then(startSession)
      .catch((error) => {
        console.error("Error checking session:", error);
        setAuthenticated(false);
      });
  }, [link]);

  const loadEvent = useCallback(async () => {
    if (eventId === null) {
      setEvent(null);
      return;
    }
    try {
      const response = await api(`/api/events/${eventId}`);
      if (!response.ok) {
        throw new Error(`Status ${response.status}`);
      }
      setEvent(await response.json());
      setError("");
    } catch (error) {
      console.error("Error loading event:", error);
      setError("Could not load the event from the server");
    }
  }, [api, eventId]);

  // Reload on every screen change so the register picks up new items and sellers
  useEffect(() => {
    if (authenticated) {
      loadEvent();
    }
  }, [authenticated, loadEvent, view]);

  const logout = async () => {
    await fetch("/api/logout", { method: "POST" });
    setEvent(null);
    setAuthenticated(false);
  };

  const openEvent = (id, nextView) => {
    setEvent(null);
    setEventId(id);
    setView(nextView);
  };

  const closeEvent = () => {
    setEvent(null);
    setEventId(null);
    setView("events");
  };

  if (link?.page === "r") {
    return <SharedReport token={link.token} />;
  }
  if (link?.page === "reset") {
    return <ResetPassword token={link.token} />;
  }
  if (link?.page === "invite") {
    return (
      <AcceptInvite
        token={link.token}
        onLogin={(session) => {
          setLink(null);
          startSession(session);
        }}
      />
    );
  }

  if (authenticated === null) {
    return null;
  }

  if (!authenticated) {
    return <Login onLogin={startSession} />;
  }

  const admin = user?.role === "organizer";
  const tab = (name, label) => (
    <button
      key={name}
      className={`tab-button ${view === name ? "active" : ""}`}
      onClick={() => setView(name)}
    >
      {label}
    </button>
  );

  return (
    <div className="pos-container">
      <h1>{event ? event.name : "Sale POS"}</h1>

      <nav className="app-nav">
        {admin && tab("events", "Events")}
        {user?.is_owner && tab("accounts", "Accounts")}
        {event && (
          <>
            {!admin && tab("register", "Register")}
            {admin && tab("setup", "Setup")}
            {tab("journal", "Journal")}
            {tab("reports", "Reports")}
            {event.mode === "yard_sale" && tab("payouts", "Payouts")}
          </>
        )}
        <span className="nav-user">{user?.name}</span>
        <button className="tab-button" onClick={logout}>
          Log Out
        </button>
      </nav>

      {error && <div className="login-error">{error}</div>}

      {view === "events" && <Events api={api} onOpen={openEvent} showOwner={user?.is_owner} />}
      {view === "accounts" && <Accounts api={api} />}

      {event && (
        <>
          {view === "register" && <Register key={event.id} api={api} event={event} />}
          {view === "setup" && (
            <EventSetup
              key={event.id}
              api={api}
              event={event}
              onChanged={loadEvent}
              onDeleted={closeEvent}
            />
          )}
          {view === "journal" && <Journal api={api} event={event} />}
          {view === "reports" && <Reports api={api} event={event} />}
          {view === "payouts" && <Settlement api={api} event={event} />}
        </>
      )}
    </div>
  );
}

export default App;
