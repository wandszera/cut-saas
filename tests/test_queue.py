import unittest
from unittest.mock import Mock

from app.services.queue import enqueue_pipeline_job


class QueueTestCase(unittest.TestCase):
    def test_enqueue_pipeline_job_uses_local_background_tasks(self):
        background_tasks = Mock()

        enqueue_pipeline_job(background_tasks, job_id=123, force=True, start_step="transcribing")

        background_tasks.add_task.assert_called_once()
        task_callable, job_id, force, start_step = background_tasks.add_task.call_args.args
        self.assertEqual(task_callable.__name__, "process_job_pipeline")
        self.assertEqual(job_id, 123)
        self.assertTrue(force)
        self.assertEqual(start_step, "transcribing")

    def test_enqueue_pipeline_job_leaves_job_pending_for_worker_backend(self):
        background_tasks = Mock()

        from app.services import queue

        original_backend = queue.settings.pipeline_queue_backend
        queue.settings.pipeline_queue_backend = "worker"
        try:
            enqueue_pipeline_job(background_tasks, job_id=123)
        finally:
            queue.settings.pipeline_queue_backend = original_backend

        background_tasks.add_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
