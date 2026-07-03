import json
from datetime import UTC, datetime
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.api.deps import require_current_workspace
from app.db.database import get_db
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.job_step import JobStep
from app.models.niche_keyword import NicheKeyword
from app.models.workspace import Workspace
from app.schemas.job import AnalyzeRequest, CandidateNotesRequest, JobCreateLocalVideo, JobCreateYouTube, JobResponse, ManualRenderRequest, NicheCreateRequest, RenderCandidateRequest, RenderRequest
from app.services.access import ensure_workspace_can_create_job
from app.services.candidates import get_candidates_for_job, regenerate_candidates_for_job
from app.services.audio import extract_audio_from_video
from app.services.exports import build_job_export_bundle, list_job_export_bundles
from app.services.niche_learning import get_feedback_profile_for_niche, get_learned_keywords_for_niche, learn_keywords_for_niche
from app.services.analysis_calibration import build_analysis_calibration_profile
from app.services.niche_registry import approve_niche, archive_niche, create_pending_niche, get_niche_profile, list_niche_definitions, reject_niche
from app.services.render_presets import list_render_presets
from app.services.render_workflow import render_candidate_clip, render_manual_clip as execute_manual_render, render_ranked_candidate_clip
from app.services.serializers import serialize_candidate, serialize_clip
from app.services.pipeline import MAX_STEP_ATTEMPTS, get_exhausted_steps, get_job_steps, request_job_cancellation, reset_pipeline_state_from_step, validate_step_name
from app.services.publication import PUBLICATION_STATUS_LABELS, build_clip_publication_package, normalize_publication_status
from app.services.queue import enqueue_pipeline_job
from app.services.media import probe_video_duration_seconds
from app.services.quota import ensure_workspace_can_start_job, get_workspace_quota_status
from app.services.scoring import score_candidates
from app.services.segmentation import build_candidate_windows, load_segments
from app.services.transcription import transcribe_audio
from app.services.youtube import download_youtube_media, fetch_youtube_metadata
from app.utils.media_urls import build_static_url
from app.utils.timecodes import parse_timecode_to_seconds
from app.utils.runtime_env import detect_node
from app.web.security import validate_csrf_request
from app.api.jobs.helpers import *


from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.api.deps import require_current_workspace
from app.web.security import validate_csrf_request

router = APIRouter(tags=["jobs"], dependencies=[Depends(validate_csrf_request)])

@router.post('/youtube', response_model=JobResponse)
def create_youtube_job(payload: JobCreateYouTube, background_tasks: BackgroundTasks, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    metadata = fetch_youtube_metadata(str(payload.url))
    ensure_workspace_can_start_job(db, workspace.id)
    ensure_workspace_can_create_job(db, workspace.id, duration_seconds=float(metadata.get('duration_seconds') or 0.0))
    job = Job(workspace_id=workspace.id, source_type='youtube', source_value=str(payload.url), status='pending')
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue_pipeline_job(background_tasks, job.id)
    return job


@router.post('/local', response_model=JobResponse)
def create_local_video_job(payload: JobCreateLocalVideo, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    ensure_workspace_can_start_job(db, workspace.id)
    video_file = Path(payload.video_path).expanduser().resolve()
    from app.core.config import settings
    data_dir = Path(settings.base_data_dir).resolve()
    if not video_file.is_relative_to(data_dir):
        raise HTTPException(status_code=403, detail='Path traversal detectado: arquivo fora do diretorio permitido')
    if not video_file.exists() or not video_file.is_file():
        raise HTTPException(status_code=400, detail='video_path nao encontrado')
    ensure_workspace_can_create_job(db, workspace.id, duration_seconds=probe_video_duration_seconds(video_file))
    resolved_title = (payload.title or video_file.stem).strip() or video_file.stem
    job = Job(workspace_id=workspace.id, source_type='local', source_value=str(video_file), status='pending', title=resolved_title, video_path=str(video_file))
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        job.status = 'extracting_audio'
        db.commit()
        audio_path = extract_audio_from_video(job.video_path, job.id)
        job.audio_path = audio_path
        job.status = 'transcribing'
        db.commit()
        transcript_path = transcribe_audio(audio_path, job.id)
        job.transcript_path = transcript_path
        job.status = 'done'
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        db.commit()
        raise HTTPException(status_code=500, detail=f'Erro ao processar job local: {e}') from e


@router.post('/web/jobs/create')
def create_job_from_form(background_tasks: BackgroundTasks, url: str=Form(...), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    ensure_workspace_can_start_job(db, workspace.id)
    job = Job(workspace_id=workspace.id, source_type='youtube', source_value=url, status='pending')
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue_pipeline_job(background_tasks, job.id)
    return RedirectResponse(url=f'/jobs/{job.id}/view', status_code=303)


@router.post('/{job_id}/retry')
def retry_job(job_id: int, background_tasks: BackgroundTasks, force: bool=False, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    if job.status not in {'failed', 'pending'}:
        raise HTTPException(status_code=400, detail="Apenas jobs com status 'failed' ou 'pending' podem ser reprocessados")
    exhausted_steps = get_exhausted_steps(db, job.id)
    if exhausted_steps and (not force):
        raise HTTPException(status_code=400, detail='Uma ou mais etapas excederam o limite de tentativas. Use force=true para tentar novamente.')
    job.status = 'pending'
    job.error_message = None
    db.commit()
    enqueue_pipeline_job(background_tasks, job.id, force=force)
    return {'message': 'Reprocessamento agendado com sucesso', 'job_id': job.id, 'status': job.status, 'force': force}


@router.post('/{job_id}/cancel')
def cancel_job(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    request_job_cancellation(db, job)
    db.refresh(job)
    return {'message': 'Cancelamento solicitado', 'job_id': job.id, 'status': job.status}


@router.post('/{job_id}/steps/{step_name}/retry')
def retry_job_step(job_id: int, step_name: str, background_tasks: BackgroundTasks, force: bool=False, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    normalized_step = _normalize_pipeline_step(step_name)
    if job.status not in {'failed', 'pending'}:
        raise HTTPException(status_code=400, detail="Apenas jobs com status 'failed' ou 'pending' podem reprocessar etapas")
    steps = get_job_steps(db, job.id)
    step_map = {step.step_name: step for step in steps}
    target_step = step_map.get(normalized_step)
    if target_step and target_step.status == 'exhausted' and (not force):
        raise HTTPException(status_code=400, detail=f"A etapa '{normalized_step}' excedeu o limite de tentativas. Use force=true para tentar novamente.")
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=False)
    enqueue_pipeline_job(background_tasks, job.id, force=force, start_step=normalized_step)
    return {'message': 'Reprocessamento da etapa agendado com sucesso', 'job_id': job.id, 'step_name': normalized_step, 'status': job.status, 'force': force}


@router.post('/{job_id}/steps/{step_name}/reset')
def reset_job_step(job_id: int, step_name: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    normalized_step = _normalize_pipeline_step(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=True)
    return {'message': 'Etapa resetada com sucesso', 'job_id': job.id, 'step_name': normalized_step, 'status': job.status, 'reset_attempts': True}


@router.get('/{job_id}')
def get_job(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    steps = get_job_steps(db, job.id)
    exhausted_steps = get_exhausted_steps(db, job.id)
    return {'id': job.id, 'workspace_id': job.workspace_id, 'source_type': job.source_type, 'source_value': job.source_value, 'status': job.status, 'title': job.title, 'video_path': job.video_path, 'video_url': build_static_url(job.video_path), 'audio_path': job.audio_path, 'audio_url': build_static_url(job.audio_path), 'transcript_path': job.transcript_path, 'transcript_url': build_static_url(job.transcript_path), 'result_path': job.result_path, 'error_message': job.error_message, 'created_at': job.created_at, 'can_retry': job.status in {'failed', 'pending'} and (not exhausted_steps), 'can_force_retry': job.status in {'failed', 'pending'}, 'has_exhausted_steps': bool(exhausted_steps), 'max_step_attempts': MAX_STEP_ATTEMPTS, 'steps': [_serialize_step_response(step) for step in steps]}


@router.get('/{job_id}/monitor')
def get_job_monitor(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    steps = get_job_steps(db, job.id)
    candidates_count = db.query(Candidate).filter(Candidate.job_id == job.id).count()
    clips_count = db.query(Clip).filter(Clip.job_id == job.id).count()
    exports_count = len(list_job_export_bundles(job.id))
    return {'id': job.id, 'status': job.status, 'error_message': job.error_message, 'video_url': build_static_url(job.video_path), 'audio_url': build_static_url(job.audio_path), 'transcript_url': build_static_url(job.transcript_path), 'video_path': job.video_path, 'audio_path': job.audio_path, 'transcript_path': job.transcript_path, 'overview': {'candidates_count': candidates_count, 'clips_count': clips_count, 'exports_count': exports_count}, 'steps': [_serialize_step_response(step) for step in steps]}

