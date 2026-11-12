import { useState, useEffect } from "react";
import { money } from "./format";
import { REFRESH_SECONDS, useAutoRefresh } from "./useAutoRefresh";

function Settlement({ api, event }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState(null);
  const refreshTick = useAutoRefresh(false);

  useEffect(() => {
    let ignore = false;
    api(`/api/events/${event.id}/settlement`)
      .then(async (response) => {
        const data = await response.json();
        if (ignore) return;
        if (response.ok) {
          setResult(data);
          setError("");
          setLastUpdated(new Date());
        } else {
          setError(data.error || "Could not load the settlement");
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        if (!ignore) setError("Error connecting to server");
      });
    return () => {
      ignore = true;
    };
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [event.id, refreshTick]);

  return (
    <div className="reports">
      <div className="report-controls">
        <button className="tab-button" onClick={() => window.print()}>
          Print
        </button>
      </div>

      {error && <div className="login-error">{error}</div>}

      {lastUpdated && (
        <p className="report-note refresh-note">
          Updates every {REFRESH_SECONDS} seconds. Last updated{" "}
          {lastUpdated.toLocaleTimeString()}.
        </p>
      )}

      {result && (
        <>
          <h2>Who Pays Whom: {event.name}</h2>
          {result.status === "open" && (
            <p className="report-note">
              The sale is still open, so these amounts will change with each sale.
            </p>
          )}

          <section className="report-section">
            <h3>Payouts</h3>
            {result.transfers.length === 0 ? (
              <p>Everyone already has what they're owed.</p>
            ) : (
              <table className="report-table">
                <tbody>
                  {result.transfers.map((transfer, index) => (
                    <tr key={index}>
                      <td>
                        <strong>{transfer.from}</strong> pays <strong>{transfer.to}</strong>
                      </td>
                      <td className="num">{money(transfer.cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="report-section">
            <h3>Sellers</h3>
            <p className="report-note">
              Sold is after offers and refunds. Holding is money paid into that
              person's own account, such as their Venmo.
            </p>
            <table className="report-table">
              <thead>
                <tr>
                  <th>Seller</th>
                  <th className="num">Sold</th>
                  <th className="num">Given Off</th>
                  <th className="num">Holding</th>
                </tr>
              </thead>
              <tbody>
                {result.sellers.map((seller) => (
                  <tr key={seller.id}>
                    <td>{seller.name}</td>
                    <td className="num">{money(seller.sold_cents)}</td>
                    <td className="num">{money(seller.discount_cents)}</td>
                    <td className="num">{money(seller.held_cents)}</td>
                  </tr>
                ))}
                <tr>
                  <td>Cash box</td>
                  <td></td>
                  <td></td>
                  <td className="num">{money(result.cash_box_cents)}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section className="report-section">
            <h3>Payment Methods</h3>
            <table className="report-table">
              <tbody>
                {result.payment_methods.map((method) => (
                  <tr key={method.id}>
                    <td>{method.label}</td>
                    <td className="num">{money(method.total_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}

export default Settlement;
