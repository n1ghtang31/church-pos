import { useState, useEffect } from "react";

function BlockedDevices({ api }) {
  const [blocks, setBlocks] = useState([]);
  const [error, setError] = useState("");

  const loadBlocks = async () => {
    const response = await api("/api/login-blocks");
    if (!response.ok) {
      throw new Error(`Status ${response.status}`);
    }
    setBlocks(await response.json());
  };

  useEffect(() => {
    loadBlocks().catch((error) => {
      console.error("Error:", error);
      setError("Could not load blocked devices");
    });
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const unblock = async (block) => {
    setError("");
    try {
      const response = await api(
        `/api/login-blocks/${encodeURIComponent(block.ip)}`,
        { method: "DELETE" },
      );
      if (!response.ok && response.status !== 404) {
        const result = await response.json();
        setError(result.error || "Could not unblock that device");
      }
      await loadBlocks();
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
  };

  return (
    <section className="report-section">
      <h3>Blocked Devices</h3>
      {error && <div className="login-error">{error}</div>}
      <p className="report-note">
        A device is blocked after 10 wrong codes in a row. Devices on a
        private network are only locked out for 5 minutes.
      </p>
      {blocks.length === 0 && <p>No blocked devices</p>}
      {blocks.map((block) => (
        <div key={block.ip} className="cashier-row">
          <div>
            <span className="cashier-name">{block.ip}</span>
            <div className="report-note">
              {block.banned
                ? "Blocked"
                : `Locked until ${new Date(block.locked_until).toLocaleTimeString()}`}
              , {block.failures} wrong codes, last{" "}
              {new Date(block.last_failure).toLocaleString()}
            </div>
          </div>
          <div className="cashier-actions">
            <button className="tab-button" onClick={() => unblock(block)}>
              Unblock
            </button>
          </div>
        </div>
      ))}
    </section>
  );
}

export default BlockedDevices;
