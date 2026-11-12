import { useState, useEffect } from "react";
import { ReportTables } from "./Reports";
import { SettlementTables } from "./Settlement";

// The read-only report linked from the email sent when an event closes
function SharedReport({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`/api/shared/${token}`)
      .then(async (response) => {
        const result = await response.json();
        if (response.ok) setData(result);
        else setError(result.error || "This link isn't valid");
      })
      .catch(() => setError("Error connecting to server"));
  }, [token]);

  if (error) {
    return (
      <div className="login-container">
        <h1>Sale POS</h1>
        <p className="login-error">{error}</p>
        <p className="report-note">
          Event reports are deleted 6 months after the event closes.
        </p>
      </div>
    );
  }
  if (!data) return null;

  const { event } = data;
  return (
    <div className="pos-container reports">
      <h1>{event.name}</h1>
      <p className="report-note">
        Closed {new Date(event.closed_at).toLocaleDateString()}. This report and every
        sale in it will be deleted on {new Date(event.purge_after).toLocaleDateString()}.
      </p>
      <div className="report-controls">
        <a className="tab-button" href={`/api/shared/${token}/sales.csv`}>
          Download Sales CSV
        </a>
        {data.settlement && (
          <a className="tab-button" href={`/api/shared/${token}/payouts.csv`}>
            Download Payouts CSV
          </a>
        )}
        <button className="tab-button" onClick={() => window.print()}>
          Print
        </button>
      </div>
      {data.settlement && <SettlementTables result={data.settlement} name={event.name} />}
      <ReportTables report={data.report} event={event} title="Sales Report" />
    </div>
  );
}

export default SharedReport;
