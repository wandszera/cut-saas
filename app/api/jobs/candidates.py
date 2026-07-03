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

@router.post('/{job_id}/analyze')
def analyze_job(job_id: int, payload: AnalyzeRequest, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    if not job.transcript_path:
        raise HTTPException(status_code=400, detail='Job ainda não possui transcrição')
    mode = _normalize_mode(payload.mode)
    feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or 'geral', mode, workspace_id=job.workspace_id)
    saved_candidates = regenerate_candidates_for_job(db, job, mode=mode)
    return {'job_id': job.id, 'title': job.title, 'mode': mode, 'feedback_profile': _serialize_feedback_profile(feedback_profile), 'total_candidates': len(saved_candidates), 'segments': [_build_api_candidate_payload(c, feedback_profile) for c in saved_candidates[:payload.top_n]]}


@router.get('/{job_id}/candidates')
def list_candidates(job_id: int, mode: str='short', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    mode = _normalize_mode(mode)
    feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or 'geral', mode, workspace_id=job.workspace_id)
    candidates = get_candidates_for_job(db, job_id=job.id, mode=mode)
    return {'job_id': job.id, 'title': job.title, 'mode': mode, 'feedback_profile': _serialize_feedback_profile(feedback_profile), 'total_candidates': len(candidates), 'candidates': [_build_api_candidate_payload(c, feedback_profile) for c in candidates]}


@router.post('/{job_id}/render-candidate-id/{candidate_id}')
def render_candidate_by_id(job_id: int, candidate_id: int, burn_subtitles: bool=False, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id, Candidate.job_id == job_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail='Candidato não encontrado')
    _ensure_job_ready_for_manual_render(job)
    clip, _subtitles_path, output_path = render_candidate_clip(db=db, job=job, candidate=candidate, burn_subtitles=burn_subtitles, render_preset='clean')
    db.commit()
    db.refresh(clip)
    return {'clip_id': clip.id, 'candidate_id': candidate.id, 'job_id': job.id, 'mode': candidate.mode, 'start': candidate.start_time, 'end': candidate.end_time, 'duration': candidate.duration, 'score': candidate.score, 'reason': candidate.reason, 'subtitles_burned': burn_subtitles, 'render_preset': 'clean', 'headline': clip.headline, 'description': clip.description, 'hashtags': clip.hashtags, 'suggested_filename': clip.suggested_filename, 'output_path': output_path}


@router.post('/{job_id}/render-candidate')
def render_candidate(job_id: int, payload: RenderCandidateRequest, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    _ensure_job_ready_for_manual_render(job)
    mode = _normalize_mode(payload.mode)
    ranked = _get_ranked_candidates(db, job, mode=mode)
    if payload.candidate_index >= len(ranked):
        raise HTTPException(status_code=400, detail=f'candidate_index inválido. Total disponível: {len(ranked)}')
    candidate = ranked[payload.candidate_index]
    clip, subtitles_path, output_path = render_ranked_candidate_clip(db=db, job=job, candidate=candidate, mode=mode, burn_subtitles=payload.burn_subtitles, render_preset=payload.render_preset, clip_index=payload.candidate_index)
    db.commit()
    db.refresh(clip)
    return {'clip_id': clip.id, 'job_id': job.id, 'source': 'candidate', 'candidate_index': payload.candidate_index, 'mode': mode, 'format': '9:16' if mode == 'short' else '16:9', 'start': candidate['start'], 'end': candidate['end'], 'duration': candidate['duration'], 'score': candidate.get('score'), 'reason': candidate.get('reason'), 'subtitles_burned': bool(subtitles_path), 'render_preset': payload.render_preset, 'headline': clip.headline, 'description': clip.description, 'hashtags': clip.hashtags, 'suggested_filename': clip.suggested_filename, 'subtitles_path': subtitles_path, 'subtitles_url': build_static_url(subtitles_path), 'output_path': output_path, 'output_url': build_static_url(output_path)}


@router.post('/candidates/{candidate_id}/approve')
def approve_candidate(candidate_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    candidate = _get_candidate_for_workspace_or_404(db, candidate_id, workspace)
    candidate.status = 'approved'
    db.commit()
    db.refresh(candidate)
    return {'message': 'Candidato aprovado com sucesso', 'candidate_id': candidate.id, 'job_id': candidate.job_id, 'status': candidate.status}


@router.post('/candidates/{candidate_id}/reject')
def reject_candidate(candidate_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    candidate = _get_candidate_for_workspace_or_404(db, candidate_id, workspace)
    candidate.status = 'rejected'
    db.commit()
    db.refresh(candidate)
    return {'message': 'Candidato rejeitado com sucesso', 'candidate_id': candidate.id, 'job_id': candidate.job_id, 'status': candidate.status}


@router.post('/candidates/{candidate_id}/reset')
def reset_candidate_status(candidate_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    candidate = _get_candidate_for_workspace_or_404(db, candidate_id, workspace)
    candidate.status = 'pending'
    db.commit()
    db.refresh(candidate)
    return {'message': 'Status do candidato resetado', 'candidate_id': candidate.id, 'job_id': candidate.job_id, 'status': candidate.status}


@router.post('/candidates/{candidate_id}/favorite')
def toggle_candidate_favorite(candidate_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    candidate = _get_candidate_for_workspace_or_404(db, candidate_id, workspace)
    candidate.is_favorite = not bool(candidate.is_favorite)
    db.commit()
    db.refresh(candidate)
    return {'message': 'Favorito atualizado com sucesso', 'candidate_id': candidate.id, 'job_id': candidate.job_id, 'is_favorite': candidate.is_favorite}


@router.post('/candidates/{candidate_id}/notes')
def update_candidate_notes(candidate_id: int, payload: CandidateNotesRequest, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    candidate = _get_candidate_for_workspace_or_404(db, candidate_id, workspace)
    candidate.editorial_notes = payload.editorial_notes.strip() or None
    db.commit()
    db.refresh(candidate)
    return {'message': 'Notas editoriais atualizadas com sucesso', 'candidate_id': candidate.id, 'job_id': candidate.job_id, 'editorial_notes': candidate.editorial_notes}


@router.get('/{job_id}/approved-candidates')
def list_approved_candidates(job_id: int, mode: str='short', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    mode = _normalize_mode(mode)
    candidates = db.query(Candidate).filter(Candidate.job_id == job.id, Candidate.mode == mode, Candidate.status == 'approved').order_by(Candidate.score.desc(), Candidate.created_at.asc()).all()
    return {'job_id': job.id, 'title': job.title, 'mode': mode, 'total_approved_candidates': len(candidates), 'candidates': [_build_api_candidate_payload(c) for c in candidates]}


@router.post('/{job_id}/render-approved')
def render_approved_candidates(job_id: int, mode: str='short', burn_subtitles: bool=False, render_preset: str='clean', db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    _ensure_job_ready_for_render(job)
    mode = _normalize_mode(mode)
    approved_candidates = db.query(Candidate).filter(Candidate.job_id == job.id, Candidate.mode == mode, Candidate.status == 'approved').order_by(Candidate.score.desc(), Candidate.created_at.asc()).all()
    rendered = []
    for candidate in approved_candidates:
        clip, _subtitles_path, output_path = render_candidate_clip(db=db, job=job, candidate=candidate, burn_subtitles=burn_subtitles, render_preset=render_preset)
        candidate.status = 'rendered'
        rendered.append({'candidate_id': candidate.id, 'clip_output_path': output_path, 'start': candidate.start_time, 'end': candidate.end_time, 'duration': candidate.duration, 'score': candidate.score, 'render_preset': render_preset})
    db.commit()
    return {'job_id': job.id, 'mode': mode, 'burn_subtitles': burn_subtitles, 'render_preset': render_preset, 'rendered_count': len(rendered), 'clips': rendered}


@router.post('/{job_id}/render')
def render_top_clips(job_id: int, payload: RenderRequest, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_or_404(db, job_id, workspace)
    _ensure_job_ready_for_manual_render(job)
    mode = _normalize_mode(payload.mode)
    ranked = _get_ranked_candidates(db, job, mode=mode)
    top_clips = ranked[:payload.top_n]
    rendered = []
    for index, clip in enumerate(top_clips):
        _rendered_clip, subtitles_path, output_path = render_ranked_candidate_clip(db=db, job=job, candidate=clip, mode=mode, burn_subtitles=payload.burn_subtitles, render_preset=payload.render_preset, clip_index=index)
        rendered.append({'clip_number': index + 1, 'start': clip['start'], 'end': clip['end'], 'duration': clip['duration'], 'score': clip['score'], 'reason': clip['reason'], 'text': clip['text'], 'mode': mode, 'format': '9:16' if mode == 'short' else '16:9', 'subtitles_burned': payload.burn_subtitles, 'render_preset': payload.render_preset, 'subtitles_path': subtitles_path, 'output_path': output_path})
    return {'job_id': job.id, 'title': job.title, 'mode': mode, 'format': '9:16' if mode == 'short' else '16:9', 'rendered_clips_count': len(rendered), 'burn_subtitles': payload.burn_subtitles, 'render_preset': payload.render_preset, 'clips': rendered}

