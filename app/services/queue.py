from fastapi import BackgroundTasks

from app.core.config import settings
from app.services.pipeline import process_job_pipeline


def enqueue_pipeline_job(
    background_tasks: BackgroundTasks,
    job_id: int,
    force: bool = False,
    start_step: str | None = None,
) -> None:
    if settings.pipeline_queue_backend == "worker":
        return
    background_tasks.add_task(process_job_pipeline, job_id, force, start_step)
