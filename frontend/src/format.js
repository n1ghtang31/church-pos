export const TENDERS = ["cash", "check", "venmo"];

export const money = (amount) =>
  `${amount < 0 ? "-" : ""}$${Math.abs(amount).toFixed(2)}`;

export const capitalize = (text) =>
  text.charAt(0).toUpperCase() + text.slice(1);

// Today's date in the browser's timezone, as YYYY-MM-DD
export const today = () => new Date().toLocaleDateString("en-CA");
