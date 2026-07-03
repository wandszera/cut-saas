import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.api.deps import get_current_workspace, require_current_user, require_current_workspace
from app.db.database import get_db
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.job_step import JobStep
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.core.config import settings
from app.services.access import TRIAL_MAX_VIDEO_MINUTES, ensure_workspace_can_create_job
from app.services.candidates import get_candidates_for_job, regenerate_candidates_for_job
from app.services.exports import list_job_export_bundles
from app.services.niche_learning import get_feedback_profile_for_niche, get_learned_keywords_for_niche, learn_keywords_for_niche
from app.services.analysis_calibration import build_analysis_calibration_profile
from app.services.niche_registry import approve_niche, archive_niche, create_pending_niche, get_niche_profile, list_niche_definitions, reject_niche
from app.services.pipeline import MAX_STEP_ATTEMPTS, complete_analysis_without_llm, get_job_steps, request_job_cancellation
from app.services.publication import PUBLICATION_STATUS_LABELS, normalize_publication_status
from app.services.queue import enqueue_pipeline_job
from app.services.quota import ensure_workspace_can_start_job, get_workspace_quota_status
from app.services.render_presets import DEFAULT_PRESET, list_render_presets
from app.services.render_workflow import render_candidate_clip, render_manual_clip
from app.services.serializers import serialize_candidate, serialize_clip
from app.services.system_diagnostics import build_runtime_readiness, build_system_diagnostics
from app.services.scoring import score_candidates
from app.services.segmentation import build_candidate_windows, load_segments
from app.services.media import probe_video_duration_seconds
from app.services.storage import get_storage, normalize_storage_key
from app.services.youtube import fetch_youtube_metadata
from app.web.security import validate_csrf_request
from app.web.template_utils import build_templates
from app.utils.media_urls import build_static_url
from app.utils.timecodes import parse_timecode_to_seconds
from app.web.pages.helpers import *


from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.web.security import validate_csrf_request

router = APIRouter(include_in_schema=False)

@router.get('/jobs/{job_id}/view')
def job_detail(job_id: int, request: Request, mode: str='short', render_preset: str=DEFAULT_PRESET, message: str | None=None, message_level: str='success', candidate_filter: str='all', candidate_sort: str='hybrid', clip_filter: str='all', export_filter: str='all', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    normalized_mode = _normalize_mode(mode)
    candidates = []
    feedback_profile = None
    candidates_missing = False
    transcript_insights = enrich_transcript_insights_for_view(job.transcript_insights)
    candidates_total_count = 0
    if job.transcript_path and job.status in {'analyzing', 'llm_enrichment', 'done'}:
        feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or 'geral', normalized_mode, workspace_id=job.workspace_id)
        saved_candidates = _get_candidates_for_job_view(db, job, normalized_mode)
        candidates_total_count = len(saved_candidates)
        candidates_missing = not bool(saved_candidates)
        if saved_candidates:
            candidates = enrich_candidates_for_view([serialize_candidate(candidate) for candidate in saved_candidates], mode=normalized_mode, feedback_profile=feedback_profile)
            if candidate_filter == 'approved':
                candidates = [candidate for candidate in candidates if candidate['status'] == 'approved']
            elif candidate_filter == 'rejected':
                candidates = [candidate for candidate in candidates if candidate['status'] == 'rejected']
            elif candidate_filter == 'rendered':
                candidates = [candidate for candidate in candidates if candidate['status'] == 'rendered']
            elif candidate_filter == 'favorite':
                candidates = [candidate for candidate in candidates if candidate['is_favorite']]
            elif candidate_filter == 'divergent':
                candidates = [candidate for candidate in candidates if candidate.get('divergence_label')]
            candidates = sort_candidates_for_view(candidates, candidate_sort)
    clips = db.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.created_at.desc()).all()
    if clip_filter == 'short':
        clips = [clip for clip in clips if clip.mode == 'short']
    elif clip_filter == 'long':
        clips = [clip for clip in clips if clip.mode == 'long']
    elif clip_filter == 'subtitled':
        clips = [clip for clip in clips if clip.subtitles_burned]
    elif clip_filter == 'ready':
        clips = [clip for clip in clips if clip.publication_status == 'ready']
    elif clip_filter == 'published':
        clips = [clip for clip in clips if clip.publication_status == 'published']
    exports = list_job_export_bundles(job.id)
    if export_filter == 'latest':
        exports = exports[:1]
    steps = get_job_steps(db, job.id)
    queue_waiting = job.status == 'pending' and (job.error_message or '').startswith('Aguardando vaga na fila')
    return templates.TemplateResponse(request, 'job_detail.html', {'job': job, 'mode': normalized_mode, 'render_preset': render_preset, 'candidate_filter': candidate_filter, 'candidate_sort': candidate_sort, 'candidates_total_count': candidates_total_count, 'clip_filter': clip_filter, 'export_filter': export_filter, 'render_presets': list_render_presets(), 'candidates': candidates, 'clips': enrich_clips_for_view(clips), 'exports': exports, 'steps': enrich_steps_for_view(steps), 'queue_waiting': queue_waiting, 'feedback_profile': enrich_feedback_profile_for_view(feedback_profile), 'candidates_missing': candidates_missing, 'transcript_insights': transcript_insights, 'video_url': build_static_url(job.video_path), 'audio_url': build_static_url(job.audio_path), 'transcript_url': build_static_url(job.transcript_path), 'auto_refresh_enabled': False, 'auto_refresh_interval_ms': JOB_AUTO_REFRESH_INTERVAL_MS, 'flash': {'message': message, 'level': message_level} if message else None, 'build_static_url': build_static_url})

