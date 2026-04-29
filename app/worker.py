import argparse
import json
import logging
import time

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import Base, SessionLocal, engine
from app.db.migrations import (
    ensure_candidate_editorial_columns,
    ensure_clip_editorial_columns,
    ensure_job_insights_columns,
    ensure_job_workspace_columns,
    ensure_niche_definition_columns,
    ensure_niche_keyword_workspace_columns,
    ensure_saas_account_tables,
    ensure_usage_event_table,
)
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.job_step import JobStep
from app.models.niche_definition import NicheDefinition
from app.models.niche_keyword import NicheKeyword
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.niche_registry import sync_builtin_niches
from app.services.pipeline import PIPELINE_WORKER_ID, process_job_pipeline, recover_stale_pipeline_jobs
from app.services.retention import cleanup_expired_artifacts
from app.utils.file_manager import ensure_directories

logger = logging.getLogger(__name__)


def configure_worker_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def _log_worker_event(event: str, **payload) -> None:
    body = {"event": event, "worker_id": PIPELINE_WORKER_ID}
    body.update(payload)
    logger.info(json.dumps(body, ensure_ascii=False, sort_keys=True))


def initialize_worker_runtime() -> None:
    if not settings.is_deployed_environment:
        Base.metadata.create_all(bind=engine)
        ensure_job_insights_columns()
        ensure_job_workspace_columns()
        ensure_candidate_editorial_columns()
        ensure_clip_editorial_columns()
        ensure_niche_definition_columns()
        ensure_niche_keyword_workspace_columns()
        ensure_saas_account_tables()
        ensure_usage_event_table()
    ensure_directories()
    with SessionLocal() as db:
        sync_builtin_niches(db)


def get_next_pending_job_id(db: Session) -> int | None:
    job = (
        db.query(Job)
        .filter(Job.status == "pending")
        .order_by(Job.created_at.asc(), Job.id.asc())
        .first()
    )
    return job.id if job else None


def run_worker_once() -> bool:
    db = SessionLocal()
    try:
        recovered_jobs = recover_stale_pipeline_jobs(db)
        cleanup_expired_artifacts(db)
        job_id = get_next_pending_job_id(db)
    finally:
        db.close()

    if recovered_jobs:
        _log_worker_event("worker_recovered_stale_jobs", recovered_jobs=recovered_jobs)

    if job_id is None:
        _log_worker_event("worker_idle")
        return False

    _log_worker_event("worker_processing_job", job_id=job_id)
    try:
        process_job_pipeline(job_id, worker_id=PIPELINE_WORKER_ID)
    except Exception as exc:
        _log_worker_event("worker_job_failed", job_id=job_id, error=str(exc))
        raise
    _log_worker_event("worker_job_completed", job_id=job_id)
    return True


def run_worker(*, poll_interval_seconds: float, max_jobs: int | None = None) -> int:
    processed = 0
    _log_worker_event(
        "worker_loop_started",
        poll_interval_seconds=poll_interval_seconds,
        max_jobs=max_jobs,
    )
    while max_jobs is None or processed < max_jobs:
        did_work = run_worker_once()
        if did_work:
            processed += 1
            continue
        if max_jobs is not None:
            break
        _log_worker_event("worker_sleeping", poll_interval_seconds=poll_interval_seconds)
        time.sleep(poll_interval_seconds)
    _log_worker_event("worker_loop_finished", processed_jobs=processed)
    return processed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Processa jobs pendentes fora do servidor web.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Processa no maximo um job pendente e encerra.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Intervalo em segundos entre buscas quando nao ha jobs pendentes.",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Encerra depois de processar esta quantidade de jobs.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_worker_logging()
    initialize_worker_runtime()
    _log_worker_event(
        "worker_started",
        environment=settings.environment,
        queue_backend=settings.pipeline_queue_backend,
        once=args.once,
        poll_interval_seconds=args.poll_interval,
        max_jobs=args.max_jobs,
    )
    if args.once:
        return 0 if run_worker_once() else 1
    run_worker(poll_interval_seconds=args.poll_interval, max_jobs=args.max_jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
