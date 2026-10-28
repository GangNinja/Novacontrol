"""Cross-phase probe: a REAL NF-25 task report into the NF-26 recorder.

The NF-26 tests build report dicts by hand; this drives the real `TaskRunner`
(over the NF-25 test gateway) and feeds the payload the manager would hand over,
so the field names have to agree for real.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tests").resolve()))

from test_novaforge_nf25 import SESSION, FakeGateway, run_payload, spec, verified_outcome

from novacontrol.novaforge.learning import LearningService
from novacontrol.novaforge.tasks import TaskRunner


def show(title: str, value: object) -> None:
    print(f"\n--- {title} ---")
    print(json.dumps(value, indent=2, default=str)[:2600])


async def main() -> int:
    run = run_payload(outcomes=(verified_outcome(),))
    gateway = FakeGateway(
        start_answer={"started": True, "run_id": "run-1"},
        wait_answer={"finished": True, "status": run},
    )
    report = await TaskRunner(gateway).run(spec(), SESSION, approved=True)
    payload = report.to_dict()
    show("real report keys", sorted(payload))
    show("real report", payload)

    service = LearningService()
    record = service.record_task_report(
        payload,
        run=run,
        strategy_id="baseline",
        strategy_version=0,
    )
    if record is None:
        print("\nRECORD: refused ->", service.status()["last_error"])
        return 1
    show("record", record.to_dict())
    show("status", service.status())
    print("\nPROBE OK: label", record.label, "valid", record.valid)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
