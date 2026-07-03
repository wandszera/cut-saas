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

@router.get('/')
def public_home(request: Request, workspace: Workspace | None=Depends(get_current_workspace)):
    if workspace is not None:
        query_string = request.url.query
        dashboard_url = '/dashboard'
        if query_string:
            dashboard_url = f'{dashboard_url}?{query_string}'
        return RedirectResponse(url=dashboard_url, status_code=303)
    return templates.TemplateResponse(request, 'index.html', {})


@router.get('/dashboard')
def dashboard(request: Request, status_filter: str='all', search_query: str='', message: str | None=None, message_level: str='success', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    recent_jobs = db.query(Job).filter(Job.workspace_id == workspace.id, Job.status != 'deleted').order_by(Job.created_at.desc()).limit(20).all()
    if not recent_jobs and (not message):
        return RedirectResponse(url='/onboarding', status_code=303)
    recent_jobs = enrich_jobs_with_progress(db, recent_jobs)
    filtered_jobs = filter_jobs_for_view(recent_jobs, status_filter)
    filtered_jobs = search_jobs_for_view(filtered_jobs, search_query)
    dashboard_summary = build_dashboard_summary(db, recent_jobs)
    pipeline_health = build_pipeline_health_summary(db, recent_jobs)
    priority_groups = build_job_priority_groups(db, filtered_jobs)
    publication_board = build_publication_board(db, recent_jobs)
    quota_status = get_workspace_quota_status(db, workspace.id)
    runtime_readiness = build_runtime_readiness()
    return templates.TemplateResponse(request, 'dashboard.html', {'recent_jobs': filtered_jobs, 'status_filter': status_filter, 'search_query': search_query, 'dashboard_summary': dashboard_summary, 'pipeline_health': pipeline_health, 'priority_groups': priority_groups, 'publication_board': publication_board, 'quota_status': quota_status, 'runtime_readiness': runtime_readiness, 'flash': {'message': message, 'level': message_level} if message else None, 'now': datetime.now(), 'auto_refresh': False})


@router.get('/onboarding')
def onboarding(request: Request, message: str | None=None, message_level: str='success', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    first_job = db.query(Job).filter(Job.workspace_id == workspace.id).order_by(Job.created_at.asc()).first()
    if first_job and not message:
        return RedirectResponse(url=_job_view_url(first_job.id), status_code=303)
    quota_status = get_workspace_quota_status(db, workspace.id)
    return templates.TemplateResponse(request, 'onboarding.html', {'quota_status': quota_status, 'flash': {'message': message, 'level': message_level} if message else None})


@router.get('/nichos')
def niche_admin_page(request: Request, message: str | None=None, level: str | None=None, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    niches = list_niche_definitions(db, include_inactive=True, workspace_id=workspace.id)
    active_niches = [niche for niche in niches if niche['status'] == 'active']
    pending_niches = [niche for niche in niches if niche['status'] == 'pending']
    inactive_niches = [niche for niche in niches if niche['status'] in {'archived', 'rejected'}]
    return templates.TemplateResponse(request, 'nicho.html', {'active_niches': active_niches, 'pending_niches': pending_niches, 'inactive_niches': inactive_niches, 'flash': _build_niche_flash(message, level)})


@router.get('/system')
def system_status_page(request: Request, user: User=Depends(require_current_user)):
    diagnostics = build_system_diagnostics()
    return templates.TemplateResponse(request, 'system.html', {'diagnostics': diagnostics})


@router.get('/account')
def account_profile_page(request: Request, db: Session=Depends(get_db), current_user: User=Depends(require_current_user), workspace: Workspace=Depends(require_current_workspace)):
    membership = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == current_user.id, WorkspaceMember.status == 'active').first()
    jobs_count = db.query(Job).filter(Job.workspace_id == workspace.id).count()
    rendered_clips_count = db.query(Clip).join(Job, Clip.job_id == Job.id).filter(Job.workspace_id == workspace.id).count()
    approved_candidates_count = db.query(Candidate).join(Job, Candidate.job_id == Job.id).filter(Job.workspace_id == workspace.id, Candidate.status == 'approved').count()
    quota_status = get_workspace_quota_status(db, workspace.id)
    return templates.TemplateResponse(request, 'account.html', {'user': current_user, 'workspace': workspace, 'membership': membership, 'jobs_count': jobs_count, 'rendered_clips_count': rendered_clips_count, 'approved_candidates_count': approved_candidates_count, 'quota_status': quota_status})


@router.get('/pricing')
def public_pricing_page(request: Request):
    """Exibe a página pública de precificação dos planos."""
    from app.services.plans import list_plans
    plans = list_plans()
    return templates.TemplateResponse(
        request,
        'pricing.html',
        {
            'plans': plans,
        }
    )

