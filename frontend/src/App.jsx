import { useState, useEffect, useRef } from "react";
import "./App.css";
import Cashiers from "./Cashiers";
import Login from "./Login";
import Reports from "./Reports";

function App() {
  const [authenticated, setAuthenticated] = useState(null);
  const [user, setUser] = useState(null);
  const [view, setView] = useState("register");
  const [submitting, setSubmitting] = useState(false);
  const checkoutInFlight = useRef(false);
  const [items, setItems] = useState([]);
  const [cart, setCart] = useState([]);
  const [tender, setTender] = useState("cash");
  const [message, setMessage] = useState("");
  const [activeTab, setActiveTab] = useState("");
  const [showNumberPad, setShowNumberPad] = useState(false);
  const [selectedItem, setSelectedItem] = useState(null);
  const [priceInput, setPriceInput] = useState("");

  // Send the user back to the login screen if their session has expired
  const api = async (path, options) => {
    const response = await fetch(path, options);
    if (response.status === 401) {
      setAuthenticated(false);
    }
    return response;
  };

  const startSession = (session) => {
    setUser(session);
    setView(session.role === "admin" ? "cashiers" : "register");
    setAuthenticated(session.authenticated);
  };

  useEffect(() => {
    fetch("/api/session")
      .then((response) => response.json())
      .then(startSession)
      .catch((error) => {
        console.error("Error checking session:", error);
        setAuthenticated(false);
      });
  }, []);

  useEffect(() => {
    if (!authenticated) {
      return;
    }
    // Fetch items from backend
    fetch("/api/items")
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Status ${response.status}`);
        }
        return response.json();
      })
      .then((data) => {
        setItems(data);
        // Set first department as active tab
        if (data.length > 0) {
          const departments = [...new Set(data.map((item) => item.department))];
          setActiveTab(departments[0]);
        }
      })
      .catch((error) => {
        console.error("Error fetching items:", error);
        setMessage("Could not load items from the server");
      });
  }, [authenticated]);

  const logout = async () => {
    await fetch("/api/logout", { method: "POST" });
    // The next cashier starts with an empty register
    setCart([]);
    setMessage("");
    setAuthenticated(false);
  };

  // Get unique departments
  const departments = [...new Set(items.map((item) => item.department))];

  // Get items for active tab
  const activeItems = items.filter((item) => item.department === activeTab);

  const openNumberPad = (item) => {
    setSelectedItem(item);
    setPriceInput("");
    setShowNumberPad(true);
  };

  const closeNumberPad = () => {
    setShowNumberPad(false);
    setSelectedItem(null);
    setPriceInput("");
  };

  const handleNumberClick = (num) => {
    // Allow only one decimal point and two decimal places
    setPriceInput((prev) =>
      /^\d*(\.\d{0,2})?$/.test(prev + num) ? prev + num : prev,
    );
  };

  const handleBackspace = () => {
    setPriceInput((prev) => prev.slice(0, -1));
  };

  const handleClear = () => {
    setPriceInput("");
  };

  const addToCart = () => {
    const price = parseFloat(priceInput);
    if (!price || price <= 0) {
      alert("Please enter a valid price");
      return;
    }

    const cartItem = {
      ...selectedItem,
      price: price,
      quantity: 1,
      cartId: Date.now(), // Unique ID for each cart entry
    };

    setCart([...cart, cartItem]);
    closeNumberPad();
  };

  const removeFromCart = (cartId) => {
    setCart(cart.filter((i) => i.cartId !== cartId));
  };

  const updateQuantity = (cartId, newQuantity) => {
    if (newQuantity <= 0) {
      removeFromCart(cartId);
    } else {
      setCart(
        cart.map((i) =>
          i.cartId === cartId ? { ...i, quantity: newQuantity } : i,
        ),
      );
    }
  };

  const calculateTotal = () => {
    return cart
      .reduce((sum, item) => sum + item.price * item.quantity, 0)
      .toFixed(2);
  };

  const checkout = async () => {
    // A ref updates immediately, so a double click can't record the sale twice
    if (checkoutInFlight.current) {
      return;
    }
    if (cart.length === 0) {
      setMessage("Cart is empty!");
      return;
    }

    const transaction = {
      items: cart.map((item) => ({
        id: item.id,
        name: item.name,
        quantity: item.quantity,
        price: item.price,
      })),
      tender: tender,
    };

    checkoutInFlight.current = true;
    setSubmitting(true);
    try {
      const response = await api("/api/transactions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(transaction),
      });

      if (response.ok) {
        const result = await response.json();
        setMessage(`Transaction completed! Total: $${result.total.toFixed(2)}`);
        setCart([]);
      } else {
        setMessage("Error processing transaction");
      }
    } catch (error) {
      console.error("Error:", error);
      setMessage("Error connecting to server");
    } finally {
      checkoutInFlight.current = false;
      setSubmitting(false);
    }
  };

  if (authenticated === null) {
    return null;
  }

  if (!authenticated) {
    return <Login onLogin={startSession} />;
  }

  return (
    <div className="pos-container">
      <h1>Church Charity Drive POS</h1>

      <nav className="app-nav">
        {user?.role === "cashier" && (
          <button
            className={`tab-button ${view === "register" ? "active" : ""}`}
            onClick={() => setView("register")}
          >
            Register
          </button>
        )}
        {user?.role === "admin" && (
          <button
            className={`tab-button ${view === "cashiers" ? "active" : ""}`}
            onClick={() => setView("cashiers")}
          >
            Cashiers
          </button>
        )}
        <button
          className={`tab-button ${view === "reports" ? "active" : ""}`}
          onClick={() => setView("reports")}
        >
          Reports
        </button>
        <span className="nav-user">{user?.name}</span>
        <button className="tab-button" onClick={logout}>
          Log Out
        </button>
      </nav>

      {view === "cashiers" && <Cashiers api={api} />}

      {view === "reports" && <Reports api={api} />}

      {view === "register" && <div className="pos-layout">
        <div className="items-section">
          <h2>Items</h2>

          {/* Department Tabs */}
          <div className="department-tabs">
            {departments.map((dept) => (
              <button
                key={dept}
                className={`tab-button ${activeTab === dept ? "active" : ""}`}
                onClick={() => setActiveTab(dept)}
              >
                {dept}
              </button>
            ))}
          </div>

          {/* Items Grid for Active Department */}
          <div className="items-grid">
            {activeItems.map((item) => (
              <div key={item.id} className="item-card">
                <h3>{item.name}</h3>
                <br></br>
                <button onClick={() => openNumberPad(item)}>Select</button>
              </div>
            ))}
          </div>
        </div>

        <div className="cart-section">
          <h2>Cart</h2>
          {cart.length === 0 ? (
            <p>No items in cart</p>
          ) : (
            <div className="cart-items">
              {cart.map((item) => (
                <div key={item.cartId} className="cart-item">
                  <div className="cart-item-info">
                    <span className="cart-item-name">{item.name}</span>
                    <span className="cart-item-price">
                      ${item.price.toFixed(2)}
                    </span>
                  </div>
                  <div className="cart-item-controls">
                    <button
                      onClick={() =>
                        updateQuantity(item.cartId, item.quantity - 1)
                      }
                    >
                      -
                    </button>
                    <span className="quantity">{item.quantity}</span>
                    <button
                      onClick={() =>
                        updateQuantity(item.cartId, item.quantity + 1)
                      }
                    >
                      +
                    </button>
                    <button
                      onClick={() => removeFromCart(item.cartId)}
                      className="remove-btn"
                    >
                      Remove
                    </button>
                  </div>
                  <div className="cart-item-subtotal">
                    Subtotal: ${(item.price * item.quantity).toFixed(2)}
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="cart-total">
            <h3>Total: ${calculateTotal()}</h3>
          </div>

          <div className="tender-section">
            <h3>Payment Method</h3>
            <div className="tender-options">
              <label>
                <input
                  type="radio"
                  value="cash"
                  checked={tender === "cash"}
                  onChange={(e) => setTender(e.target.value)}
                />
                Cash
              </label>
              <label>
                <input
                  type="radio"
                  value="check"
                  checked={tender === "check"}
                  onChange={(e) => setTender(e.target.value)}
                />
                Check
              </label>
              <label>
                <input
                  type="radio"
                  value="venmo"
                  checked={tender === "venmo"}
                  onChange={(e) => setTender(e.target.value)}
                />
                Venmo
              </label>
            </div>
          </div>

          <button
            className="checkout-btn"
            onClick={checkout}
            disabled={submitting}
          >
            {submitting ? "Processing..." : "Complete Transaction"}
          </button>

          {message && <div className="message">{message}</div>}
        </div>
      </div>}

      {/* Number Pad Modal */}
      {showNumberPad && (
        <div className="modal-overlay" onClick={closeNumberPad}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <h2>Enter Price for {selectedItem?.name}</h2>

            <div className="price-display">
              <span className="dollar-sign">$</span>
              <span className="price-value">{priceInput || "0.00"}</span>
            </div>

            <div className="number-pad">
              <button onClick={() => handleNumberClick("7")}>7</button>
              <button onClick={() => handleNumberClick("8")}>8</button>
              <button onClick={() => handleNumberClick("9")}>9</button>
              <button onClick={() => handleNumberClick("4")}>4</button>
              <button onClick={() => handleNumberClick("5")}>5</button>
              <button onClick={() => handleNumberClick("6")}>6</button>
              <button onClick={() => handleNumberClick("1")}>1</button>
              <button onClick={() => handleNumberClick("2")}>2</button>
              <button onClick={() => handleNumberClick("3")}>3</button>
              <button onClick={handleClear} className="clear-btn">
                C
              </button>
              <button onClick={() => handleNumberClick("0")}>0</button>
              <button onClick={() => handleNumberClick(".")}>.</button>
            </div>

            <div className="modal-actions">
              <button onClick={handleBackspace} className="backspace-btn">
                ← Backspace
              </button>
              <button onClick={closeNumberPad} className="cancel-btn">
                Cancel
              </button>
              <button onClick={addToCart} className="add-btn">
                Add to Cart
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
