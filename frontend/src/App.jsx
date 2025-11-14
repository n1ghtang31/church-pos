import { useState, useEffect } from "react";
import "./App.css";

function App() {
  const [items, setItems] = useState([]);
  const [cart, setCart] = useState([]);
  const [tender, setTender] = useState("cash");
  const [message, setMessage] = useState("");

  const API_BASE = "http://192.168.15.165:5000";

  useEffect(() => {
    // Fetch items from backend
    fetch(`${API_BASE}/api/items`)
      .then((response) => response.json())
      .then((data) => setItems(data))
      .catch((error) => console.error("Error fetching items:", error));
  }, []);

  const addToCart = (item) => {
    const existingItem = cart.find((i) => i.id === item.id);
    if (existingItem) {
      setCart(
        cart.map((i) =>
          i.id === item.id ? { ...i, quantity: i.quantity + 1 } : i,
        ),
      );
    } else {
      setCart([...cart, { ...item, quantity: 1 }]);
    }
  };

  const removeFromCart = (itemId) => {
    setCart(cart.filter((i) => i.id !== itemId));
  };

  const updateQuantity = (itemId, newQuantity) => {
    if (newQuantity <= 0) {
      removeFromCart(itemId);
    } else {
      setCart(
        cart.map((i) =>
          i.id === itemId ? { ...i, quantity: newQuantity } : i,
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
    if (cart.length === 0) {
      setMessage("Cart is empty!");
      return;
    }

    const transaction = {
      items: cart.map((item) => ({
        id: item.id,
        name: item.name,
        quantity: item.quantity,
      })),
      tender: tender,
    };

    try {
      const response = await fetch(`${API_BASE}/api/transactions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(transaction),
      });

      if (response.ok) {
        const result = await response.json();
        setMessage(`Transaction completed! Total: $${result.total}`);
        setCart([]);
      } else {
        setMessage("Error processing transaction");
      }
    } catch (error) {
      console.error("Error:", error);
      setMessage("Error connecting to server");
    }
  };

  return (
    <div className="pos-container">
      <h1>Church Charity Drive POS</h1>

      <div className="pos-layout">
        <div className="items-section">
          <h2>Available Items</h2>
          <div className="items-grid">
            {items.map((item) => (
              <div key={item.id} className="item-card">
                <h3>{item.name}</h3>
                <p className="price">${item.price.toFixed(2)}</p>
                <button onClick={() => addToCart(item)}>Add to Cart</button>
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
                <div key={item.id} className="cart-item">
                  <div className="cart-item-info">
                    <span className="cart-item-name">{item.name}</span>
                    <span className="cart-item-price">
                      ${item.price.toFixed(2)}
                    </span>
                  </div>
                  <div className="cart-item-controls">
                    <button
                      onClick={() => updateQuantity(item.id, item.quantity - 1)}
                    >
                      -
                    </button>
                    <span className="quantity">{item.quantity}</span>
                    <button
                      onClick={() => updateQuantity(item.id, item.quantity + 1)}
                    >
                      +
                    </button>
                    <button
                      onClick={() => removeFromCart(item.id)}
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

          <button className="checkout-btn" onClick={checkout}>
            Complete Transaction
          </button>

          {message && <div className="message">{message}</div>}
        </div>
      </div>
    </div>
  );
}

export default App;
