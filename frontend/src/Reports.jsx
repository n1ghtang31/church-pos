import { useState, useEffect } from "react";
import { money, today } from "./format";
import { REFRESH_SECONDS, useAutoRefresh } from "./useAutoRefresh";

export function ReportTables({ report, event, title }) {
  const yardSale = event.mode === "yard_sale";
  return (
    <>
      <h2>{title}</h2>
      <p className="report-overall">
        {money(report.overall.total_cents)} from {report.overall.count}{" "}
        {report.overall.count === 1 ? "sale" : "sales"}
        {report.refunds.count > 0 &&
          `, after ${report.refunds.count} ${
            report.refunds.count === 1 ? "refund" : "refunds"
          } (${money(report.refunds.total_cents)})`}
        {report.overall.discount_cents > 0 &&
          `. Offers took ${money(report.overall.discount_cents)} off.`}
      </p>

      <section className="report-section">
        <h3>Daily Multi-Total</h3>
        <p className="report-note">
          Sales, payment methods, and totals are all after refunds.
        </p>
        <table className="report-table">
          <thead>
            <tr>
              <th>Date</th>
              <th className="num">Sales</th>
              {report.by_method.map((method) => (
                <th key={method.id} className="num">
                  {method.label}
                </th>
              ))}
              <th className="num">Refunds</th>
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {report.by_day.map((day) => (
              <tr key={day.date}>
                <td>{day.date}</td>
                <td className="num">{day.count}</td>
                {report.by_method.map((method) => (
                  <td key={method.id} className="num">
                    {money(day.by_method[method.id] ?? 0)}
                  </td>
                ))}
                <td className="num">{money(day.refund_cents)}</td>
                <td className="num">{money(day.total_cents)}</td>
              </tr>
            ))}
            {report.by_day.length === 0 && (
              <tr>
                <td colSpan={report.by_method.length + 4}>No sales in this period</td>
              </tr>
            )}
          </tbody>
          <tfoot>
            <tr>
              <td>Total</td>
              <td className="num">{report.overall.count}</td>
              {report.by_method.map((method) => (
                <td key={method.id} className="num">
                  {money(method.total_cents)}
                </td>
              ))}
              <td className="num">{money(report.refunds.total_cents)}</td>
              <td className="num">{money(report.overall.total_cents)}</td>
            </tr>
          </tfoot>
        </table>
      </section>

      <section className="report-section">
        <h3>Cashier Totals</h3>
        <table className="report-table">
          <thead>
            <tr>
              <th>Cashier</th>
              <th className="num">Sales</th>
              <th className="num">Refunds</th>
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {report.by_cashier.map((cashier) => (
              <tr key={cashier.cashier}>
                <td>{cashier.cashier}</td>
                <td className="num">{cashier.count}</td>
                <td className="num">{money(cashier.refund_cents)}</td>
                <td className="num">{money(cashier.total_cents)}</td>
              </tr>
            ))}
            {report.by_cashier.length === 0 && (
              <tr>
                <td colSpan="4">No sales in this period</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {!yardSale && (
        <section className="report-section">
          <h3>Department Totals</h3>
          <table className="report-table">
            <thead>
              <tr>
                <th>Department</th>
                <th className="num">Quantity</th>
                <th className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {report.by_department.map((department) => (
                <tr key={department.department}>
                  <td>{department.department || "None"}</td>
                  <td className="num">{department.quantity}</td>
                  <td className="num">{money(department.total_cents)}</td>
                </tr>
              ))}
              {report.by_department.length === 0 && (
                <tr>
                  <td colSpan="3">No sales in this period</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <section className="report-section">
        <h3>{yardSale ? "Seller Totals" : "Single Item Totals"}</h3>
        {yardSale && (
          <p className="report-note">
            Totals are after each seller's share of any offers. See Payouts
            for who pays whom.
          </p>
        )}
        <table className="report-table">
          <thead>
            <tr>
              <th>{yardSale ? "Seller" : "Item"}</th>
              {!yardSale && <th>Department</th>}
              <th className="num">Quantity</th>
              {yardSale && <th className="num">Asking</th>}
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {report.lines.map((line) => (
              <tr key={line.id}>
                <td>{line.name}</td>
                {!yardSale && <td>{line.department}</td>}
                <td className="num">{line.quantity}</td>
                {yardSale && <td className="num">{money(line.list_cents)}</td>}
                <td className="num">{money(line.total_cents)}</td>
              </tr>
            ))}
            {report.lines.length === 0 && (
              <tr>
                <td colSpan="4">No sales in this period</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </>
  );
}

function Reports({ api, event }) {
  const [start, setStart] = useState(today);
  const [end, setEnd] = useState(today);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState(null);
  const refreshTick = useAutoRefresh(false);

  useEffect(() => {
    let ignore = false;
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);

    api(`/api/events/${event.id}/report?${params}`)
      .then(async (response) => {
        const result = await response.json();
        if (ignore) return;
        if (response.ok) {
          setReport(result);
          setError("");
          setLastUpdated(new Date());
        } else {
          setReport(null);
          setError(result.error || "Could not load report");
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        // Keep showing the last report if a refresh can't reach the server
        if (!ignore) {
          setError("Error connecting to server");
        }
      });

    return () => {
      ignore = true;
    };
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [event.id, start, end, refreshTick]);

  const showRange = (newStart, newEnd) => {
    setStart(newStart);
    setEnd(newEnd);
  };

  const rangeLabel = () => {
    if (!start && !end) return "All dates";
    if (start === end) return start;
    return `${start || "Beginning"} to ${end || "today"}`;
  };

  return (
    <div className="reports">
      <div className="report-controls">
        <label>
          From
          <input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          To
          <input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        <button
          className="tab-button"
          onClick={() => showRange(today(), today())}
        >
          Today
        </button>
        <button className="tab-button" onClick={() => showRange("", "")}>
          All Dates
        </button>
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

      {report && (
        <ReportTables report={report} event={event} title={`${event.name}: ${rangeLabel()}`} />
      )}
    </div>
  );
}

export default Reports;
