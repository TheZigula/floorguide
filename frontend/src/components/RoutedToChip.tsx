// RoutedToChip.tsx — the "Routed to: Safety procedures" label that sits on every answer.
// Owns: turning the contract's routed_to code into the plain English a supervisor reads, and its colour.

import type { RoutedTo } from "../types";

/** The exact wording required by CLAUDE.md, instance 2. One entry per routed_to value. */
const LABELS: Record<RoutedTo, string> = {
  safety: "Safety procedures",
  maintenance: "Maintenance manuals",
  quality: "Quality standards",
  rule: "Service-due rule",
  work_order: "Work order",
  memory: "Memory",
  refused: "Refused",
};

function routedToLabel(routedTo: RoutedTo): string {
  return LABELS[routedTo] ?? routedTo;
}

export function RoutedToChip({ routedTo }: { routedTo: RoutedTo }) {
  return (
    <span className={"chip chip--" + routedTo} title={"routed_to = " + routedTo}>
      Routed to: {routedToLabel(routedTo)}
    </span>
  );
}
