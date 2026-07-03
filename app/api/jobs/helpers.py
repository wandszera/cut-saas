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


from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.api.deps import require_current_workspace
from app.web.security import validate_csrf_request

router = APIRouter(tags=["jobs"], dependencies=[Depends(validate_csrf_request)])

def _get_job_or_404(db: Session, job_id: int, workspace: Workspace | None=None) -> Job:
    query = db.query(Job).filter(Job.id == job_id)
    if workspace is not None:
        query = query.filter(Job.workspace_id == workspace.id)
    job = query.first()
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    return job


def _get_candidate_or_404(db: Session, candidate_id: int, workspace_id: int) -> Candidate:
    candidate = db.query(Candidate).join(Job, Candidate.job_id == Job.id).filter(Candidate.id == candidate_id, Job.workspace_id == workspace_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail='Candidato não encontrado')
    return candidate


def _get_candidate_for_workspace_or_404(db: Session, candidate_id: int, workspace: Workspace) -> Candidate:
    candidate = db.query(Candidate).join(Job, Candidate.job_id == Job.id).filter(Candidate.id == candidate_id, Job.workspace_id == workspace.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail='Candidato nÃ£o encontrado')
    return candidate


def _get_clip_for_workspace_or_404(db: Session, clip_id: int, workspace: Workspace) -> Clip:
    clip = db.query(Clip).join(Job, Clip.job_id == Job.id).filter(Clip.id == clip_id, Job.workspace_id == workspace.id).first()
    if not clip:
        raise HTTPException(status_code=404, detail='Clip nÃ£o encontrado')
    return clip


def _normalize_mode(mode: str) -> str:
    normalized = mode.lower().strip()
    if normalized not in {'short', 'long'}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")
    return normalized


def _normalize_pipeline_step(step_name: str) -> str:
    try:
        return validate_step_name(step_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _niche_service_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    status_code = 404 if 'não encontrado' in detail.lower() or 'nao encontrado' in detail.lower() else 400
    return HTTPException(status_code=status_code, detail=detail)


def _ensure_job_ready_for_render(job: Job) -> None:
    if not job.video_path:
        raise HTTPException(status_code=400, detail='Job não possui vídeo')
    if not job.transcript_path:
        raise HTTPException(status_code=400, detail='Job não possui transcrição')


def _ensure_job_ready_for_manual_render(job: Job) -> None:
    if not job.video_path:
        raise HTTPException(status_code=400, detail='Job nÃ£o possui vÃ\xaddeo')


def _get_ranked_candidates(db: Session, job: Job, mode: str) -> list[dict]:
    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    niche = job.detected_niche or 'geral'
    niche_profile = get_niche_profile(db, niche, workspace_id=job.workspace_id)
    learned_keywords = get_learned_keywords_for_niche(db, niche, workspace_id=job.workspace_id)
    feedback_profile = get_feedback_profile_for_niche(db, niche, mode, workspace_id=job.workspace_id)
    transcript_insights = json.loads(job.transcript_insights) if job.transcript_insights else None
    calibration_profile = build_analysis_calibration_profile(db, niche=niche, mode=mode)
    return score_candidates(candidates, mode=mode, niche=niche, niche_profile=niche_profile, learned_keywords=learned_keywords, feedback_profile=feedback_profile, transcript_insights=transcript_insights, calibration_profile=calibration_profile)


def _parse_step_details(raw_details: str | None) -> dict:
    if not raw_details:
        return {}
    try:
        payload = json.loads(raw_details)
    except json.JSONDecodeError:
        return {'raw_details': raw_details}
    return payload if isinstance(payload, dict) else {'value': payload}


def _heartbeat_age_seconds(raw_value: str | None) -> float | None:
    if not raw_value:
        return None
    try:
        heartbeat_dt = datetime.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if heartbeat_dt.tzinfo is not None:
        heartbeat_dt = heartbeat_dt.astimezone(UTC).replace(tzinfo=None)
    return (datetime.utcnow() - heartbeat_dt).total_seconds()


def _serialize_step_response(step) -> dict:
    details_payload = _parse_step_details(step.details)
    duration_seconds = details_payload.get('duration_seconds')
    duration_label = f'{float(duration_seconds):.3f}s' if isinstance(duration_seconds, (int, float)) else None
    heartbeat_at = details_payload.get('heartbeat_at')
    progress_message = details_payload.get('progress_message')
    progress_percent = details_payload.get('progress_percent')
    summary_items = []
    reason = details_payload.get('reason')
    if reason:
        summary_items.append(f'Motivo: {reason}')
    attempt = details_payload.get('attempt')
    if attempt is not None:
        summary_items.append(f'Tentativa registrada: {attempt}')
    if duration_label:
        summary_items.append(f'Duração: {duration_label}')
    if details_payload.get('forced') is True:
        summary_items.append('Execução forçada')
    if progress_message and step.status == 'running':
        summary_items.append(f'Atividade: {progress_message}')
    if heartbeat_at and step.status == 'running':
        summary_items.append(f'Ultima atividade: {heartbeat_at}')
    return {'id': step.id, 'step_name': step.step_name, 'status': step.status, 'attempts': step.attempts, 'max_attempts': MAX_STEP_ATTEMPTS, 'can_retry': step.status in {'failed', 'pending'}, 'can_force_retry': step.status == 'exhausted', 'is_exhausted': step.status == 'exhausted', 'error_message': step.error_message, 'details': step.details, 'details_payload': details_payload, 'summary_items': summary_items, 'duration_seconds': duration_seconds, 'duration_label': duration_label, 'progress_message': progress_message, 'progress_percent': progress_percent, 'heartbeat_at': heartbeat_at, 'started_at': step.started_at, 'completed_at': step.completed_at}


def _serialize_feedback_profile(profile: dict | None) -> dict:
    profile = profile or {}
    return {'niche': profile.get('niche'), 'mode': profile.get('mode'), 'positive_count': profile.get('positive_count', 0), 'negative_count': profile.get('negative_count', 0), 'sample_count': profile.get('sample_count', 0), 'min_samples_reached': profile.get('min_samples_reached', False), 'successful_keywords': profile.get('successful_keywords', []), 'positive_means': profile.get('positive_means', {}), 'negative_means': profile.get('negative_means', {}), 'hybrid_weight_profile': profile.get('hybrid_weight_profile', {})}


def _build_pipeline_health_payload(db: Session, workspace_id: int) -> dict:
    jobs = db.query(Job).filter(Job.workspace_id == workspace_id).all()
    job_ids = [job.id for job in jobs]
    if job_ids:
        steps = db.query(JobStep).filter(JobStep.job_id.in_(job_ids)).all()
    else:
        steps = []
    queued_jobs = [job for job in jobs if job.status == 'pending' and (job.error_message or '').startswith('Aguardando vaga na fila')]
    active_jobs = [job for job in jobs if job.status in {'downloading', 'extracting_audio', 'transcribing', 'analyzing', 'llm_enrichment', 'cancel_requested'}]
    failed_jobs = [job for job in jobs if job.status == 'failed']
    canceled_jobs = [job for job in jobs if job.status == 'canceled']
    duration_by_step: dict[str, list[float]] = {}
    stale_running_steps = 0
    for step in steps:
        payload = _parse_step_details(step.details)
        duration = payload.get('duration_seconds')
        if isinstance(duration, (int, float)):
            duration_by_step.setdefault(step.step_name, []).append(float(duration))
        age_seconds = _heartbeat_age_seconds(payload.get('heartbeat_at'))
        if step.status == 'running' and age_seconds is not None and (age_seconds >= 900):
            stale_running_steps += 1
    average_step_duration_seconds = {step_name: round(sum(values) / len(values), 3) for step_name, values in duration_by_step.items() if values}
    return {'jobs': {'total': len(jobs), 'active': len(active_jobs), 'queued': len(queued_jobs), 'failed': len(failed_jobs), 'canceled': len(canceled_jobs), 'done': sum((1 for job in jobs if job.status == 'done'))}, 'steps': {'total': len(steps), 'running': sum((1 for step in steps if step.status == 'running')), 'failed': sum((1 for step in steps if step.status in {'failed', 'exhausted'})), 'completed': sum((1 for step in steps if step.status == 'completed')), 'average_duration_seconds': average_step_duration_seconds, 'stale_running': stale_running_steps}}


def _build_dashboard_monitor_payload(db: Session, workspace_id: int) -> dict:
    jobs = db.query(Job).filter(Job.workspace_id == workspace_id).order_by(Job.created_at.desc()).limit(20).all()
    queued_jobs = [job for job in jobs if job.status == 'pending' and (job.error_message or '').startswith('Aguardando vaga na fila')]
    active_jobs = [job for job in jobs if job.status in {'downloading', 'extracting_audio', 'transcribing', 'analyzing', 'llm_enrichment', 'cancel_requested'}]
    health = _build_pipeline_health_payload(db, workspace_id)
    return {'summary': {'total_jobs': len(jobs), 'active_jobs': len(active_jobs), 'queued_jobs': len(queued_jobs), 'jobs_with_clips': len({clip.job_id for clip in db.query(Clip).filter(Clip.job_id.in_([job.id for job in jobs])).all()}) if jobs else 0, 'jobs_ready_to_publish': len({clip.job_id for clip in db.query(Clip).filter(Clip.job_id.in_([job.id for job in jobs]), Clip.publication_status == 'ready').all()}) if jobs else 0, 'jobs_published': len({clip.job_id for clip in db.query(Clip).filter(Clip.job_id.in_([job.id for job in jobs]), Clip.publication_status == 'published').all()}) if jobs else 0, 'jobs_with_exports': sum((1 for job in jobs if list_job_export_bundles(job.id)))}, 'pipeline_health': health, 'jobs': [{'id': job.id, 'status': job.status, 'status_label': job.status_label, 'title': job.title, 'error_message': job.error_message, 'progress': job.progress} for job in jobs]}


def _summarize_numeric_distribution(values: list[float]) -> dict:
    if not values:
        return {'count': 0, 'min': None, 'max': None, 'avg': None, 'p50': None, 'p90': None}
    ordered = sorted((float(value) for value in values))

    def _percentile(ratio: float) -> float:
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
        return round(ordered[index], 2)
    return {'count': len(ordered), 'min': round(ordered[0], 2), 'max': round(ordered[-1], 2), 'avg': round(sum(ordered) / len(ordered), 2), 'p50': _percentile(0.5), 'p90': _percentile(0.9)}


def _build_score_buckets(values: list[float]) -> list[dict]:
    buckets = [{'label': '>= 9', 'min': 9.0, 'max': None, 'count': 0}, {'label': '8 - 8.99', 'min': 8.0, 'max': 8.99, 'count': 0}, {'label': '7 - 7.99', 'min': 7.0, 'max': 7.99, 'count': 0}, {'label': '< 7', 'min': None, 'max': 6.99, 'count': 0}]
    for value in values:
        score = float(value)
        if score >= 9.0:
            buckets[0]['count'] += 1
        elif score >= 8.0:
            buckets[1]['count'] += 1
        elif score >= 7.0:
            buckets[2]['count'] += 1
        else:
            buckets[3]['count'] += 1
    return buckets


def _build_duration_buckets(values: list[float]) -> list[dict]:
    buckets = [{'label': '< 30s', 'min_seconds': None, 'max_seconds': 29.99, 'count': 0}, {'label': '30s - 59s', 'min_seconds': 30.0, 'max_seconds': 59.99, 'count': 0}, {'label': '60s - 89s', 'min_seconds': 60.0, 'max_seconds': 89.99, 'count': 0}, {'label': '>= 90s', 'min_seconds': 90.0, 'max_seconds': None, 'count': 0}]
    for value in values:
        duration = float(value)
        if duration < 30.0:
            buckets[0]['count'] += 1
        elif duration < 60.0:
            buckets[1]['count'] += 1
        elif duration < 90.0:
            buckets[2]['count'] += 1
        else:
            buckets[3]['count'] += 1
    return buckets


def _build_ranking_insights_payload(*, job: Job, mode: str, feedback_profile: dict | None, candidates: list[Candidate]) -> dict:
    feedback_profile = feedback_profile or {}
    hybrid_weight_profile = feedback_profile.get('hybrid_weight_profile', {}) or {}
    candidate_payloads = [_build_api_candidate_payload(candidate, feedback_profile) for candidate in candidates]
    divergences = [item for item in candidate_payloads if item.get('divergence_score') is not None]
    strong_divergences = [item for item in divergences if float(item['divergence_score']) >= 2.2]
    moderate_or_stronger_divergences = [item for item in divergences if float(item['divergence_score']) >= 1.2]
    llm_favored = [item for item in moderate_or_stronger_divergences if (item.get('llm_score') or 0.0) > (item.get('heuristic_score') or 0.0)]
    heuristic_favored = [item for item in moderate_or_stronger_divergences if (item.get('heuristic_score') or 0.0) > (item.get('llm_score') or 0.0)]
    final_scores = [float(candidate.score or 0.0) for candidate in candidates]
    heuristic_scores = [float(candidate.heuristic_score) for candidate in candidates if candidate.heuristic_score is not None]
    llm_scores = [float(candidate.llm_score) for candidate in candidates if candidate.llm_score is not None]
    durations = [float(candidate.duration or 0.0) for candidate in candidates]
    status_counts: dict[str, int] = {}
    for candidate in candidates:
        status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1
    top_divergent_candidates = sorted(moderate_or_stronger_divergences, key=lambda item: float(item['divergence_score']), reverse=True)[:5]
    return {'job_id': job.id, 'title': job.title, 'niche': job.detected_niche or 'geral', 'mode': mode, 'weights': {'preferred_source': hybrid_weight_profile.get('preferred_source', 'balanced'), 'heuristic_weight': round(float(hybrid_weight_profile.get('heuristic_weight', 0.65) or 0.65), 2), 'llm_weight': round(float(hybrid_weight_profile.get('llm_weight', 0.35) or 0.35), 2), 'reviewed_count': int(hybrid_weight_profile.get('reviewed_count', 0) or 0), 'approved_count': int(hybrid_weight_profile.get('approved_count', 0) or 0), 'rejected_count': int(hybrid_weight_profile.get('rejected_count', 0) or 0)}, 'candidate_summary': {'total_candidates': len(candidates), 'llm_scored_count': len(llm_scores), 'divergent_count': len(moderate_or_stronger_divergences), 'strong_divergence_count': len(strong_divergences), 'favorite_count': sum((1 for candidate in candidates if candidate.is_favorite)), 'status_counts': status_counts}, 'divergence_summary': {'compared_candidates': len(divergences), 'moderate_or_higher_count': len(moderate_or_stronger_divergences), 'strong_count': len(strong_divergences), 'llm_favored_count': len(llm_favored), 'heuristic_favored_count': len(heuristic_favored), 'divergence_score_distribution': _summarize_numeric_distribution([float(item['divergence_score']) for item in divergences]), 'top_divergent_candidates': [{'candidate_id': item['candidate_id'], 'start': item['start'], 'end': item['end'], 'score': item['score'], 'heuristic_score': item['heuristic_score'], 'llm_score': item['llm_score'], 'divergence_score': item['divergence_score'], 'divergence_label': item['divergence_label'], 'divergence_summary': item['divergence_summary'], 'status': item['status']} for item in top_divergent_candidates]}, 'distribution': {'final_score': {**_summarize_numeric_distribution(final_scores), 'buckets': _build_score_buckets(final_scores)}, 'heuristic_score': _summarize_numeric_distribution(heuristic_scores), 'llm_score': _summarize_numeric_distribution(llm_scores), 'duration_seconds': {**_summarize_numeric_distribution(durations), 'buckets': _build_duration_buckets(durations)}}}


def _build_api_candidate_payload(candidate, feedback_profile: dict | None=None) -> dict:
    base_payload = serialize_candidate(candidate)
    heuristic_score = float(base_payload.get('heuristic_score', 0.0) or 0.0)
    llm_score_raw = base_payload.get('llm_score')
    llm_score = round(float(llm_score_raw), 2) if llm_score_raw is not None else None
    divergence_score = round(abs(heuristic_score - llm_score), 2) if llm_score is not None else None
    divergence_label = None
    divergence_summary = None
    if divergence_score is not None:
        if divergence_score >= 2.2:
            divergence_label = 'divergência forte'
        elif divergence_score >= 1.2:
            divergence_label = 'divergência moderada'
        if divergence_label:
            if llm_score > heuristic_score:
                divergence_summary = 'LLM gostou mais do corte do que o heurístico'
            elif heuristic_score > llm_score:
                divergence_summary = 'Heurístico gostou mais do corte do que a LLM'
            else:
                divergence_summary = 'Heurístico e LLM quase empatados'
    hybrid_weight_profile = (feedback_profile or {}).get('hybrid_weight_profile', {}) or {}
    preferred_source = hybrid_weight_profile.get('preferred_source', 'balanced')
    heuristic_weight = round(float(hybrid_weight_profile.get('heuristic_weight', 0.65) or 0.65), 2)
    llm_weight = round(float(hybrid_weight_profile.get('llm_weight', 0.35) or 0.35), 2)
    adaptive_blend_explanation = None
    if divergence_score is not None and divergence_score >= 1.2:
        if preferred_source == 'heuristic':
            adaptive_blend_explanation = f'Este corte subiu com mais apoio da heurística porque, neste nicho, divergências recentes estão favorecendo o heurístico ({heuristic_weight} vs {llm_weight}).'
        elif preferred_source == 'llm':
            adaptive_blend_explanation = f'Este corte recebeu mais peso da LLM porque, neste nicho, divergências recentes estão favorecendo a revisão da LLM ({llm_weight} vs {heuristic_weight}).'
        else:
            adaptive_blend_explanation = f'Este corte ficou equilibrado porque o nicho ainda mantém pesos híbridos estáveis ({heuristic_weight} heurístico / {llm_weight} LLM).'
    return {**base_payload, 'llm_score': llm_score_raw, 'divergence_score': divergence_score, 'divergence_label': divergence_label, 'divergence_summary': divergence_summary, 'adaptive_blend_explanation': adaptive_blend_explanation}


@router.get('/debug/node')
def debug_node(workspace: Workspace=Depends(require_current_workspace)):
    return detect_node()


__all__ = ['_get_job_or_404', '_get_candidate_or_404', '_get_candidate_for_workspace_or_404', '_get_clip_for_workspace_or_404', '_normalize_mode', '_normalize_pipeline_step', '_niche_service_error', '_ensure_job_ready_for_render', '_ensure_job_ready_for_manual_render', '_get_ranked_candidates', '_parse_step_details', '_heartbeat_age_seconds', '_serialize_step_response', '_serialize_feedback_profile', '_build_pipeline_health_payload', '_build_dashboard_monitor_payload', '_summarize_numeric_distribution', '_build_score_buckets', '_build_duration_buckets', '_build_ranking_insights_payload', '_build_api_candidate_payload', 'debug_node']
