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

@router.get('/{job_id}/feedback-profile')
def get_job_feedback_profile(job_id: int, mode: str='short', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    normalized_mode = _normalize_mode(mode)
    niche = job.detected_niche or 'geral'
    feedback_profile = get_feedback_profile_for_niche(db, niche, normalized_mode, workspace_id=job.workspace_id)
    return {'job_id': job.id, 'title': job.title, 'niche': niche, 'mode': normalized_mode, 'feedback_profile': _serialize_feedback_profile(feedback_profile)}


@router.get('/{job_id}/ranking-insights')
def get_job_ranking_insights(job_id: int, mode: str='short', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    normalized_mode = _normalize_mode(mode)
    niche = job.detected_niche or 'geral'
    feedback_profile = get_feedback_profile_for_niche(db, niche, normalized_mode, workspace_id=job.workspace_id)
    candidates = get_candidates_for_job(db, job_id=job.id, mode=normalized_mode)
    return _build_ranking_insights_payload(job=job, mode=normalized_mode, feedback_profile=feedback_profile, candidates=candidates)


@router.post('/{job_id}/feedback-profile/recalibrate')
def recalibrate_job_feedback_profile(job_id: int, mode: str='short', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    normalized_mode = _normalize_mode(mode)
    niche = (job.detected_niche or 'geral').lower().strip()
    learned = learn_keywords_for_niche(db, niche=niche, workspace_id=job.workspace_id)
    feedback_profile = get_feedback_profile_for_niche(db, niche, normalized_mode, workspace_id=job.workspace_id)
    return {'message': 'Aprendizado recalibrado com sucesso', 'job_id': job.id, 'title': job.title, 'niche': niche, 'mode': normalized_mode, 'learned_keywords_count': len(learned), 'feedback_profile': _serialize_feedback_profile(feedback_profile)}

