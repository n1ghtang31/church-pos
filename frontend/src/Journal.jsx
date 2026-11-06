import { useState, useEffect } from "react";
import { capitalize, money, today } from "./format";

const dateTime = (timestamp) => new Date(timestamp).toLocaleString();

function Receipt({ transaction, printing, refunding, onPrint, onRefund }) {
  const isRefund = transaction.refund_of !== null;
  const isRefunded = transaction.refund_id !== null;

  return (
    <article
      className={`receipt ${isRefund ? "refund" : ""} ${printing ? "printing" : ""}`}
    >
      <h3 className="receipt-title">Church Charity Drive</h3>

      {isRefund && (
        <div className="receipt-banner">
          REFUND OF RECEIPT #{transaction.refund_of}
        </div>
      )}
      {isRefunded && (
        <div className="receipt-banner">
          REFUNDED: receipt #{transaction.refund_id}
          <br />
          {transaction.refund_cashier}, {dateTime(transaction.refund_timestamp)}
        </div>
      )}

      <div className="receipt-details">
        <div className="receipt-line">
          <span>Receipt</span>
          <span>#{transaction.id}</span>
        </div>
        <div className="receipt-line">
          <span>Date</span>
          <span>{dateTime(transaction.timestamp)}</span>
        </div>
        <div className="receipt-line">
          <span>Cashier</span>
          <span>{transaction.cashier}</span>
        </div>
        <div className="receipt-line">
          <span>Payment</span>
          <span>{capitalize(transaction.tender)}</span>
        </div>
      </div>

      {transaction.items.map((item, index) => (
        <div key={index} className="receipt-line">
          <span>
            {item.quantity} x {item.name} @ {money(item.price)}
          </span>
          <span>{money(item.quantity * item.price)}</span>
        </div>
      ))}

      <div className="receipt-line receipt-total">
        <span>Total</span>
        <span>{money(transaction.total)}</span>
      </div>

      <div className="receipt-actions">
        <button className="tab-button" onClick={onPrint}>
          Print
        </button>
        {!isRefund && !isRefunded && (
          <button
            className="tab-button"
            onClick={onRefund}
            disabled={refunding}
          >
            Refund
          </button>
        )}
      </div>
    </article>
  );
}

function Journal({ api }) {
  const [start, setStart] = useState(today);
  const [end, setEnd] = useState(today);
  const [cashier, setCashier] = useState("");
  const [transactions, setTransactions] = useState([]);
  const [error, setError] = useState("");
  const [reloadCount, setReloadCount] = useState(0);
  const [refundingId, setRefundingId] = useState(null);
  const [printingId, setPrintingId] = useState(null);

  useEffect(() => {
    let ignore = false;
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);

    api(`/api/transactions?${params}`)
      .then(async (response) => {
        const result = await response.json();
        if (ignore) return;
        if (response.ok) {
          setTransactions(result);
          setError("");
        } else {
          setTransactions([]);
          setError(result.error || "Could not load the journal");
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        if (!ignore) {
          setTransactions([]);
          setError("Error connecting to server");
        }
      });

    return () => {
      ignore = true;
    };
    // api is recreated on every render, so it must not retrigger the request
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, end, reloadCount]);

  // Print once the chosen receipt has been marked as the only one to show
  useEffect(() => {
    if (printingId === null) {
      return;
    }
    const finish = () => setPrintingId(null);
    window.addEventListener("afterprint", finish);
    window.print();
    return () => window.removeEventListener("afterprint", finish);
  }, [printingId]);

  const refund = async (transaction) => {
    if (
      !window.confirm(
        `Refund receipt #${transaction.id} for ${money(transaction.total)}? This can't be undone.`,
      )
    ) {
      return;
    }
    setRefundingId(transaction.id);
    setError("");
    try {
      const response = await api(`/api/transactions/${transaction.id}/refund`, {
        method: "POST",
      });
      if (!response.ok) {
        const result = await response.json();
        setError(result.error || "Could not refund this sale");
      }
      // Reload either way, since a failure may mean someone else already refunded it
      setReloadCount((count) => count + 1);
    } catch (error) {
      console.error("Error:", error);
      setError("Error connecting to server");
    }
    setRefundingId(null);
  };

  const cashiers = [
    ...new Set([...transactions.map((t) => t.cashier), cashier]),
  ]
    .filter(Boolean)
    .sort();

  const shown = transactions
    .filter((t) => !cashier || t.cashier === cashier)
    .reverse();

  return (
    <div className={`journal ${printingId !== null ? "printing-one" : ""}`}>
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
        <label>
          Cashier
          <select value={cashier} onChange={(e) => setCashier(e.target.value)}>
            <option value="">All cashiers</option>
            {cashiers.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <button
          className="tab-button"
          onClick={() => {
            setStart(today());
            setEnd(today());
          }}
        >
          Today
        </button>
        <button
          className="tab-button"
          onClick={() => {
            setStart("");
            setEnd("");
          }}
        >
          All Dates
        </button>
      </div>

      {error && <div className="login-error">{error}</div>}

      {shown.length === 0 && !error && (
        <p className="journal-empty">No transactions in this period</p>
      )}

      <div className="receipts">
        {shown.map((transaction) => (
          <Receipt
            key={transaction.id}
            transaction={transaction}
            printing={printingId === transaction.id}
            refunding={refundingId === transaction.id}
            onPrint={() => setPrintingId(transaction.id)}
            onRefund={() => refund(transaction)}
          />
        ))}
      </div>
    </div>
  );
}

export default Journal;
