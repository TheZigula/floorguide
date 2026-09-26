// HealthChip.tsx — the header's one-line answer to "what is this screen actually talking to?".
// Owns: the /health probe and its label; it never changes how the app behaves, only what it admits to.

import { useEffect, useState } from "react";
import { API_LABEL, getHealth, USING_MOCK } from "../api/client";
import type { HealthResponse } from "../types";

type Probe =
  | { state: "mock" }
  | { state: "checking" }
  | { state: "up"; health: HealthResponse }
  | { state: "down"; reason: string };

export function HealthChip() {
  const [probe, setProbe] = useState<Probe>(USING_MOCK ? { state: "mock" } : { state: "checking" });

  useEffect(() => {
    if (USING_MOCK) return;
    let cancelled = false;
    getHealth()
      .then((health) => {
        if (!cancelled) setProbe({ state: "up", health });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setProbe({ state: "down", reason: error instanceof Error ? error.message : "no answer" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (probe.state === "mock") {
    return (
      <span className="health health--mock" title="VITE_API_BASE is not set">
        Mock data — backend not connected
      </span>
    );
  }
  if (probe.state === "checking") {
    return (
      <span className="health" title={API_LABEL}>
        Checking backend…
      </span>
    );
  }
  if (probe.state === "down") {
    return (
      <span className="health health--down" title={probe.reason}>
        Backend unreachable
      </span>
    );
  }

  const { service, commit, vector_backend } = probe.health;
  return (
    <span className="health health--up" title={API_LABEL}>
      {service} · commit {commit} · {vector_backend} vectors
    </span>
  );
}
