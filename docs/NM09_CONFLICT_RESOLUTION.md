# NM-09 Conflict Resolution

Signals are grouped by portfolio and instrument. Duplicate same-direction BUY signals are consolidated by strategy priority (higher wins, then creation time); the selected proposal records the conflict id. Opposing signals are conflicts, and different signal step horizons are reported as `horizon_mismatch`.

Policies:

- `review` (default): opposing signals produce no proposal and a `review_required` conflict.
- `priority`: the highest-priority signal may proceed when it is strictly above the alternative; a tie requires review.
- `block`: every conflicting group is suppressed, including duplicates that would otherwise consolidate.

Every conflict stores its type, participants, policy-independent outcome, selected signal/strategy and rationale. Conflict ids are stable for the same run moment and signal set, and repeated identical runs do not duplicate the conflict row. Conflict records are append-only and exposed through `GET /market/strategies/conflicts`.
