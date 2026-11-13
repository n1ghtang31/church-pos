// All amounts from the server are integer cents
export const money = (cents) =>
  `${cents < 0 ? "-" : ""}$${(Math.abs(cents) / 100).toFixed(2)}`;

// "12.5" -> 1250. Returns NaN for anything that isn't dollars and cents.
export const toCents = (text) => {
  const match = /^(\d*)(?:\.(\d{0,2}))?$/.exec(text);
  if (!match || (!match[1] && !match[2])) return NaN;
  return Number(match[1] || 0) * 100 + Number((match[2] || "").padEnd(2, "0"));
};

export const MODE_LABELS = { charity: "Charity drive", yard_sale: "Yard sale" };

// What a paid event costs. The real prices are set in Stripe, so keep these and the
// landing page in step with it.
export const PRICES = { yard_sale: "$10", charity: "$30" };

// Today's date in the browser's timezone, as YYYY-MM-DD
export const today = () => new Date().toLocaleDateString("en-CA");

// Same split as the server's allocate_discount, so the register can show
// each seller's share of an offer before the sale is made
export const allocateDiscount = (lineCents, totalCents) => {
  const subtotal = lineCents.reduce((sum, cents) => sum + cents, 0);
  if (totalCents >= subtotal || subtotal === 0) return [...lineCents];
  const shares = lineCents.map((cents) => Math.floor((cents * totalCents) / subtotal));
  const order = lineCents
    .map((cents, index) => ({ index, cents, remainder: (cents * totalCents) % subtotal }))
    .sort((a, b) => b.remainder - a.remainder || b.cents - a.cents || a.index - b.index);
  let leftover = totalCents - shares.reduce((sum, cents) => sum + cents, 0);
  for (const { index } of order) {
    if (leftover-- <= 0) break;
    shares[index] += 1;
  }
  return shares;
};

export const sendJson = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
