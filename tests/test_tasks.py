from __future__ import annotations

import unittest

from novacontrol.tasks import TaskCenter, TaskRecordStatus


class TaskCenterTests(unittest.TestCase):
    def test_task_center_tracks_status_and_round_trips(self) -> None:
        center = TaskCenter()
        task = center.create("Demo", kind="test")
        center.update(task.id, TaskRecordStatus.COMPLETED, progress=1.0, result={"ok": True})

        restored = TaskCenter.from_dict(center.to_dict())
        restored_task = restored.get(task.id)

        self.assertEqual(restored_task.status, TaskRecordStatus.COMPLETED)
        self.assertEqual(restored_task.result["ok"], True)


if __name__ == "__main__":
    unittest.main()
