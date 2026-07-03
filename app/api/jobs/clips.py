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

@router.post('/{job_id}/render-manual')
def render_manual_clip(job_id: int, payload: ManualRenderRequest, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    _ensure_job_ready_for_manual_render(job)
    mode = _normalize_mode(payload.mode)
    try:
        start_seconds = parse_timecode_to_seconds(payload.start)
        end_seconds = parse_timecode_to_seconds(payload.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if end_seconds <= start_seconds:
        raise HTTPException(status_code=400, detail='end deve ser maior que start')
    duration = round(end_seconds - start_seconds, 2)
    clip, subtitles_path, output_path = execute_manual_render(db=db, job=job, start=start_seconds, end=end_seconds, mode=mode, burn_subtitles=payload.burn_subtitles, render_preset=payload.render_preset, clip_index=9999, reason='Render manual')
    db.commit()
    db.refresh(clip)
    return {'clip_id': clip.id, 'job_id': job.id, 'source': 'manual', 'mode': mode, 'format': '9:16' if mode == 'short' else '16:9', 'start': start_seconds, 'end': end_seconds, 'duration': duration, 'subtitles_burned': bool(subtitles_path), 'render_preset': payload.render_preset, 'headline': clip.headline, 'description': clip.description, 'hashtags': clip.hashtags, 'suggested_filename': clip.suggested_filename, 'subtitles_path': subtitles_path, 'subtitles_url': build_static_url(subtitles_path), 'output_path': output_path, 'output_url': build_static_url(output_path)}


@router.get('/{job_id}/clips')
def list_rendered_clips(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    clips = db.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.created_at.desc()).all()
    return {'job_id': job.id, 'title': job.title, 'total_clips': len(clips), 'clips': [serialize_clip(clip) for clip in clips]}


@router.get('/{job_id}/export')
def export_job_bundle(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    clips = db.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.created_at.desc()).all()
    if not clips:
        raise HTTPException(status_code=400, detail='Nenhum clip renderizado para exportar')
    zip_path = build_job_export_bundle(job, clips)
    return FileResponse(path=zip_path, media_type='application/zip', filename=Path(zip_path).name)


@router.post('/clips/{clip_id}/publication')
def update_clip_publication_status(clip_id: int, status: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    clip = _get_clip_for_workspace_or_404(db, clip_id, workspace)
    if not clip:
        raise HTTPException(status_code=404, detail='Clip não encontrado')
    try:
        normalized_status = normalize_publication_status(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    clip.publication_status = normalized_status
    db.commit()
    db.refresh(clip)
    return {'clip_id': clip.id, 'job_id': clip.job_id, 'publication_status': clip.publication_status, 'publication_status_label': PUBLICATION_STATUS_LABELS[clip.publication_status], 'publication': build_clip_publication_package(clip)}


@router.get('/{job_id}/exports')
def list_job_exports(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    exports = list_job_export_bundles(job.id)
    return {'job_id': job.id, 'title': job.title, 'total_exports': len(exports), 'exports': [{'name': row['name'], 'size_bytes': row['size_bytes'], 'created_at': row.get('created_at'), 'modified_at': row['modified_at'], 'download_url': f"/jobs/{job.id}/export/files/{row['name']}"} for row in exports]}


@router.get('/{job_id}/export/files/{filename}')
def download_existing_export(job_id: int, filename: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    _get_job_or_404(db, job_id, workspace)
    exports = {row['name']: row for row in list_job_export_bundles(job_id)}
    target = exports.get(filename)
    if not target:
        raise HTTPException(status_code=404, detail='Pacote de exportação não encontrado')
    return FileResponse(path=target['path'], media_type='application/zip', filename=filename)

