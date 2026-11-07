import { useState, useEffect } from "react";

export const REFRESH_SECONDS = 15;

// Returns a number that goes up every REFRESH_SECONDS while the page is visible
// and not paused, and as soon as the page becomes visible again. Add it to an
// effect's dependencies to reload that effect's data.
export function useAutoRefresh(paused) {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (paused) {
      return;
    }
    const refresh = () => {
      if (document.visibilityState === "visible") {
        setTick((count) => count + 1);
      }
    };
    const timer = setInterval(refresh, REFRESH_SECONDS * 1000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [paused]);

  return tick;
}
