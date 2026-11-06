import { useState, useEffect } from "react";

function Cashiers({ api }) {
  const [cashiers, setCashiers] = useState([]);
  const [error, setError] = useState("");
  const [newName, setNewName] = useState("");
  const [newPin, setNewPin] = useState("");
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const [editPin, setEditPin] = useState("");

  // Send a change to the server, then reload the list. Returns true if it worked.
  const request = async (path, options) => {
    setError("");
    try {
      const response = await api(path, options);
      if (!response.ok) {
        const result = await response.json();
        setError(result.error || "Something went wrong");
        return false;
      }
      const list = await api("/api/cashiers");
      setCashiers(await list.json());
      return true;
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
      return false;
    }
  };

  const sendJson = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  useEffect(() => {
    api("/api/cashiers")
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Status ${response.status}`);
        }
        return response.json();
      })
      .then(setCashiers)
      .catch((error) => {
        console.error("Error:", error);
        setError("Could not load cashiers");
      });
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const addCashier = async (e) => {
    e.preventDefault();
    const added = await request(
      "/api/cashiers",
      sendJson("POST", { name: newName, pin: newPin }),
    );
    if (added) {
      setNewName("");
      setNewPin("");
    }
  };

  const startEditing = (cashier) => {
    setError("");
    setEditingId(cashier.id);
    setEditName(cashier.name);
    setEditPin("");
  };

  const saveEdit = async (e) => {
    e.preventDefault();
    // A blank PIN keeps the current one
    const changes = editPin ? { name: editName, pin: editPin } : { name: editName };
    const saved = await request(
      `/api/cashiers/${editingId}`,
      sendJson("PUT", changes),
    );
    if (saved) {
      setEditingId(null);
    }
  };

  const removeCashier = (cashier) => {
    if (
      window.confirm(
        `Remove ${cashier.name}? Their past sales will stay in the reports.`,
      )
    ) {
      request(`/api/cashiers/${cashier.id}`, { method: "DELETE" });
    }
  };

  return (
    <div className="cashiers">
      {error && <div className="login-error">{error}</div>}

      <section className="report-section">
        <h3>Add Cashier</h3>
        <form className="cashier-form" onSubmit={addCashier}>
          <label>
            Name
            <input
              value={newName}
              maxLength={50}
              onChange={(e) => setNewName(e.target.value)}
            />
          </label>
          <label>
            PIN (4 to 8 digits)
            <input
              type="password"
              inputMode="numeric"
              autoComplete="new-password"
              maxLength={8}
              value={newPin}
              onChange={(e) => setNewPin(e.target.value)}
            />
          </label>
          <button type="submit" className="tab-button">
            Add Cashier
          </button>
        </form>
      </section>

      <section className="report-section">
        <h3>Cashiers</h3>
        {cashiers.length === 0 && <p>No cashiers yet</p>}
        {cashiers.map((cashier) =>
          editingId === cashier.id ? (
            <form key={cashier.id} className="cashier-row" onSubmit={saveEdit}>
              <div className="cashier-form">
                <label>
                  Name
                  <input
                    value={editName}
                    maxLength={50}
                    onChange={(e) => setEditName(e.target.value)}
                  />
                </label>
                <label>
                  New PIN (blank keeps current)
                  <input
                    type="password"
                    inputMode="numeric"
                    autoComplete="new-password"
                    maxLength={8}
                    value={editPin}
                    onChange={(e) => setEditPin(e.target.value)}
                  />
                </label>
              </div>
              <div className="cashier-actions">
                <button type="submit" className="tab-button">
                  Save
                </button>
                <button
                  type="button"
                  className="tab-button"
                  onClick={() => setEditingId(null)}
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <div key={cashier.id} className="cashier-row">
              <span className="cashier-name">{cashier.name}</span>
              <div className="cashier-actions">
                <button
                  className="tab-button"
                  onClick={() => startEditing(cashier)}
                >
                  Edit
                </button>
                <button
                  className="tab-button"
                  onClick={() => removeCashier(cashier)}
                >
                  Remove
                </button>
              </div>
            </div>
          ),
        )}
      </section>
    </div>
  );
}

export default Cashiers;
