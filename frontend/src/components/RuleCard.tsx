// RuleCard.tsx — the service-due numbers, shown exactly as the rule function returned them.
// Owns: the display of rule_result. It does no arithmetic of its own — every number here came off the wire.

import type { RuleResult } from "../types";

const STATUS_LABEL: Record<RuleResult["status"], string> = {
  OK: "Not due",
  DUE_SOON: "Due soon",
  OVERDUE: "Overdue",
};

export function RuleCard({ result }: { result: RuleResult }) {
  return (
    <section className="card rulecard" aria-label="Service-due calculation">
      <h2 className="card__title">Service-due calculation</h2>
      <div className="rulecard__body">
        <div className="rulecard__headline">
          <span className="rulecard__asset">{result.asset_id}</span>
          <span className={"badge badge--" + result.status}>{STATUS_LABEL[result.status]}</span>
        </div>

        <dl className="rulecard__grid">
          <div>
            <dt>Hours since service</dt>
            <dd>{result.hours_since_service}</dd>
          </div>
          <div>
            <dt>Hours remaining</dt>
            <dd>{result.hours_remaining}</dd>
          </div>
          <div>
            <dt>Overdue by</dt>
            <dd>{result.overdue_by}</dd>
          </div>
        </dl>

        <p className="rulecard__note">
          The status and these numbers are arithmetic done in code on the machine's meter readings
          and the manual's interval. The model did not produce them and cannot change them.
        </p>
      </div>
    </section>
  );
}
