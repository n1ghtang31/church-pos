import { useState, useEffect } from "react";
import { TENDERS, capitalize, money, today } from "./format";

function Reports({ api }) {
  const [start, setStart] = useState(today);
  const [end, setEnd] = useState(today);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let ignore = false;
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);

    api(`/api/reports/summary?${params}`)
      .then(async (response) => {
        const result = await response.json();
        if (ignore) return;
        if (response.ok) {
          setReport(result);
          setError("");
        } else {
          setReport(null);
          setError(result.error || "Could not load report");
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        if (!ignore) {
          setReport(null);
          setError("Error connecting to server");
        }
      });

    return () => {
      ignore = true;
    };
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, end]);

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

      {report && (
        <>
          <h2>Sales Report: {rangeLabel()}</h2>
          <p className="report-overall">
            {money(report.overall.total)} from {report.overall.count}{" "}
            {report.overall.count === 1 ? "sale" : "sales"}
            {report.refunds.count > 0 &&
              `, after ${report.refunds.count} ${
                report.refunds.count === 1 ? "refund" : "refunds"
              } (${money(report.refunds.total)})`}
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
                  {TENDERS.map((tender) => (
                    <th key={tender} className="num">
                      {capitalize(tender)}
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
                    {TENDERS.map((tender) => (
                      <td key={tender} className="num">
                        {money(day.tenders[tender])}
                      </td>
                    ))}
                    <td className="num">{money(day.refunds)}</td>
                    <td className="num">{money(day.total)}</td>
                  </tr>
                ))}
                {report.by_day.length === 0 && (
                  <tr>
                    <td colSpan={TENDERS.length + 4}>No sales in this period</td>
                  </tr>
                )}
              </tbody>
              <tfoot>
                <tr>
                  <td>Total</td>
                  <td className="num">{report.overall.count}</td>
                  {TENDERS.map((tender) => (
                    <td key={tender} className="num">
                      {money(report.by_tender[tender].total)}
                    </td>
                  ))}
                  <td className="num">{money(report.refunds.total)}</td>
                  <td className="num">{money(report.overall.total)}</td>
                </tr>
              </tfoot>
            </table>
          </section>

          <section className="report-section">
            <h3>Payment Method Totals</h3>
            <table className="report-table">
              <thead>
                <tr>
                  <th>Payment Method</th>
                  <th className="num">Sales</th>
                  <th className="num">Total</th>
                </tr>
              </thead>
              <tbody>
                {TENDERS.map((tender) => (
                  <tr key={tender}>
                    <td>{capitalize(tender)}</td>
                    <td className="num">{report.by_tender[tender].count}</td>
                    <td className="num">
                      {money(report.by_tender[tender].total)}
                    </td>
                  </tr>
                ))}
              </tbody>
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
                    <td className="num">{money(cashier.refunds)}</td>
                    <td className="num">{money(cashier.total)}</td>
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
                    <td>{department.department}</td>
                    <td className="num">{department.quantity}</td>
                    <td className="num">{money(department.total)}</td>
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

          <section className="report-section">
            <h3>Single Item Totals</h3>
            <table className="report-table">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Department</th>
                  <th className="num">Quantity</th>
                  <th className="num">Total</th>
                </tr>
              </thead>
              <tbody>
                {report.items.map((item) => (
                  <tr key={`${item.id}-${item.name}`}>
                    <td>{item.name}</td>
                    <td>{item.department}</td>
                    <td className="num">{item.quantity}</td>
                    <td className="num">{money(item.total)}</td>
                  </tr>
                ))}
                {report.items.length === 0 && (
                  <tr>
                    <td colSpan="4">No sales in this period</td>
                  </tr>
                )}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}

export default Reports;
