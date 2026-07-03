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


from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.web.security import validate_csrf_request

templates = build_templates()

PIPELINE_STEP_SEQUENCE = [
    'downloading',
    'extracting_audio',
    'transcribing',
    'analyzing',
    'llm_enrichment',
]

STEP_STALE_HEARTBEAT_SECONDS = 900

JOB_AUTO_REFRESH_STATUSES = {
    'pending',
    'downloading',
    'extracting_audio',
    'transcribing',
    'analyzing',
    'llm_enrichment',
    'cancel_requested',
}

JOB_AUTO_REFRESH_INTERVAL_MS = 4000

router = APIRouter(include_in_schema=False)

def has_active_jobs(jobs: list[Job]) -> bool:
    active_statuses = {'pending', 'downloading', 'extracting_audio', 'transcribing', 'analyzing', 'llm_enrichment', 'rendering', 'cancel_requested'}
    return any((job.status in active_statuses for job in jobs))


def _normalize_mode(mode: str) -> str:
    normalized = mode.lower().strip()
    return normalized if normalized in {'short', 'long'} else 'short'


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


def _build_timecode_from_parts(hours: str | None, minutes: str | None, seconds: str | None) -> str:
    hour_value = (hours or '').strip() or '0'
    minute_value = (minutes or '').strip() or '0'
    second_value = (seconds or '').strip() or '0'
    return f'{hour_value}:{minute_value}:{second_value}'


def _get_candidate_or_404(db: Session, candidate_id: int, workspace_id: int) -> Candidate:
    candidate = db.query(Candidate).join(Job, Candidate.job_id == Job.id).filter(Candidate.id == candidate_id, Job.workspace_id == workspace_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail='Candidato não encontrado')
    return candidate


def _get_job_for_workspace_or_404(db: Session, job_id: int, workspace: Workspace) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not job:
        raise HTTPException(status_code=404, detail='Job nÃ£o encontrado')
    return job


def _job_view_url(job_id: int, *, mode: str | None=None, render_preset: str | None=None, message: str | None=None, level: str='success') -> str:
    params: dict[str, str] = {}
    if mode:
        params['mode'] = _normalize_mode(mode)
    if render_preset:
        params['render_preset'] = render_preset
    if message:
        params['message'] = message
        params['message_level'] = level
    query = urlencode(params)
    return f'/jobs/{job_id}/view?{query}' if query else f'/jobs/{job_id}/view'


def _dashboard_url(message: str | None=None, level: str='success') -> str:
    params: dict[str, str] = {}
    if message:
        params['message'] = message
        params['message_level'] = level
    query = urlencode(params)
    return f'/dashboard?{query}' if query else '/dashboard'


def _billing_activation_url() -> str:
    params = urlencode({'message': f'Seu workspace pode testar 1 video de ate {TRIAL_MAX_VIDEO_MINUTES} minutos sem cartao. Depois disso, cadastre um cartao para continuar.', 'level': 'warning'})
    return f'/billing?{params}'


def _is_billing_activation_message(message: str) -> bool:
    normalized = (message or '').lower()
    return any((token in normalized for token in ('cartao', 'teste gratis', '30 minutos', 'videos maiores')))


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


def _ensure_page_candidates(db: Session, job: Job, mode: str) -> list[Candidate]:
    saved_candidates = get_candidates_for_job(db, job.id, mode)
    if saved_candidates:
        return saved_candidates
    return regenerate_candidates_for_job(db, job, mode=mode)


def _get_candidates_for_job_view(db: Session, job: Job, mode: str) -> list[Candidate]:
    saved_candidates = get_candidates_for_job(db, job.id, mode)
    if saved_candidates:
        return saved_candidates
    if job.status == 'done' and job.transcript_path and Path(job.transcript_path).exists():
        return regenerate_candidates_for_job(db, job, mode=mode)
    return []


def format_seconds_to_mmss(seconds: float | int | None) -> str:
    if seconds is None:
        return '--:--'
    total = int(round(float(seconds)))
    minutes = total // 60
    secs = total % 60
    return f'{minutes:02}:{secs:02}'


def filter_jobs_for_view(jobs: list[Job], view_filter: str) -> list[Job]:
    normalized = (view_filter or 'all').strip().lower()
    if normalized == 'active':
        return [job for job in jobs if job.status not in {'done', 'failed'}]
    if normalized == 'done':
        return [job for job in jobs if job.status == 'done']
    if normalized == 'failed':
        return [job for job in jobs if job.status == 'failed']
    return jobs


def search_jobs_for_view(jobs: list[Job], search_query: str) -> list[Job]:
    normalized = (search_query or '').strip().lower()
    if not normalized:
        return jobs

    def _matches(job: Job) -> bool:
        title = (job.title or '').lower()
        source = (job.source_value or '').lower()
        return normalized in title or normalized in source or normalized in f'job {job.id}'
    return [job for job in jobs if _matches(job)]


def enrich_jobs_with_progress(db: Session, jobs: list[Job]) -> list[Job]:
    if not jobs:
        return jobs
    step_rows = db.query(JobStep).filter(JobStep.job_id.in_([job.id for job in jobs])).order_by(JobStep.created_at.asc(), JobStep.id.asc()).all()
    grouped_steps: dict[int, dict[str, JobStep]] = {}
    for step in step_rows:
        grouped_steps.setdefault(step.job_id, {})[step.step_name] = step
    total_steps = len(PIPELINE_STEP_SEQUENCE) or 1
    for job in jobs:
        progress_value = 5
        step_map = grouped_steps.get(job.id, {})
        completed_units = 0.0
        active_step_name = None
        for index, step_name in enumerate(PIPELINE_STEP_SEQUENCE):
            step = step_map.get(step_name)
            if not step:
                continue
            if step.status in {'completed', 'skipped'}:
                completed_units += 1.0
                continue
            if step.status == 'running':
                completed_units += 0.55
                active_step_name = step_name
                break
            if step.status in {'failed', 'exhausted'}:
                active_step_name = step_name
                break
            if step.status == 'pending':
                active_step_name = step_name
                break
        if job.status == 'done':
            progress_value = 100
        elif job.status == 'failed':
            progress_value = max(5, min(95, round(completed_units / total_steps * 100)))
        else:
            progress_value = max(5, min(95, round(completed_units / total_steps * 100)))
        setattr(job, 'progress_value', progress_value)
        setattr(job, 'active_step_name', active_step_name or job.status)
    return jobs


def _build_niche_flash(message: str | None, level: str | None) -> dict | None:
    if not message:
        return None
    return {'message': message, 'level': level or 'info'}


def _niche_redirect(message: str, level: str='info') -> RedirectResponse:
    params = urlencode({'message': message, 'level': level})
    return RedirectResponse(url=f'/nichos?{params}', status_code=303)


def build_dashboard_summary(db: Session, jobs: list[Job]) -> dict:
    if not jobs:
        return {'total_jobs': 0, 'active_jobs': 0, 'queued_jobs': 0, 'failed_jobs': 0, 'canceled_jobs': 0, 'jobs_with_approved': 0, 'jobs_with_clips': 0, 'jobs_with_exports': 0, 'jobs_ready_to_publish': 0, 'jobs_published': 0}
    job_ids = [job.id for job in jobs]
    candidates = db.query(Candidate).filter(Candidate.job_id.in_(job_ids)).all()
    clips = db.query(Clip).filter(Clip.job_id.in_(job_ids)).all()
    jobs_with_approved = {candidate.job_id for candidate in candidates if candidate.status == 'approved'}
    jobs_with_clips = {clip.job_id for clip in clips}
    jobs_ready_to_publish = {clip.job_id for clip in clips if clip.publication_status == 'ready'}
    jobs_published = {clip.job_id for clip in clips if clip.publication_status == 'published'}
    jobs_with_exports = {job.id for job in jobs if list_job_export_bundles(job.id)}
    return {'total_jobs': len(jobs), 'active_jobs': sum((1 for job in jobs if job.status not in {'done', 'failed'})), 'queued_jobs': sum((1 for job in jobs if job.status == 'pending' and (job.error_message or '').startswith('Aguardando vaga na fila'))), 'failed_jobs': sum((1 for job in jobs if job.status == 'failed')), 'canceled_jobs': sum((1 for job in jobs if job.status == 'canceled')), 'jobs_with_approved': len(jobs_with_approved), 'jobs_with_clips': len(jobs_with_clips), 'jobs_with_exports': len(jobs_with_exports), 'jobs_ready_to_publish': len(jobs_ready_to_publish), 'jobs_published': len(jobs_published)}


def build_pipeline_health_summary(db: Session, jobs: list[Job]) -> dict:
    job_ids = [job.id for job in jobs]
    step_rows = []
    if job_ids:
        step_rows = db.query(JobStep).filter(JobStep.job_id.in_(job_ids)).all()
    durations_by_step: dict[str, list[float]] = {}
    stale_running_steps = 0
    for step in step_rows:
        details = _parse_step_details(step.details)
        duration = details.get('duration_seconds')
        if isinstance(duration, (int, float)):
            durations_by_step.setdefault(step.step_name, []).append(float(duration))
        age_seconds = _heartbeat_age_seconds(details.get('heartbeat_at'))
        if step.status == 'running' and age_seconds is not None and (age_seconds >= STEP_STALE_HEARTBEAT_SECONDS):
            stale_running_steps += 1
    average_durations = {step_name: round(sum(values) / len(values), 1) for step_name, values in durations_by_step.items() if values}
    longest_step = None
    if average_durations:
        longest_step = max(average_durations.items(), key=lambda item: item[1])
    queued_jobs = [job for job in jobs if job.status == 'pending' and (job.error_message or '').startswith('Aguardando vaga na fila')]
    return {'queued_jobs': len(queued_jobs), 'active_jobs': sum((1 for job in jobs if job.status in {'downloading', 'extracting_audio', 'transcribing', 'analyzing', 'llm_enrichment', 'cancel_requested'})), 'canceled_jobs': sum((1 for job in jobs if job.status == 'canceled')), 'failed_jobs': sum((1 for job in jobs if job.status == 'failed')), 'avg_transcribing_seconds': average_durations.get('transcribing'), 'avg_analyzing_seconds': average_durations.get('analyzing'), 'avg_llm_seconds': average_durations.get('llm_enrichment'), 'slowest_step_name': longest_step[0] if longest_step else None, 'slowest_step_seconds': longest_step[1] if longest_step else None, 'stale_running_steps': stale_running_steps}


def build_job_priority_groups(db: Session, jobs: list[Job]) -> dict[str, list[Job]]:
    if not jobs:
        return {'stale_jobs': [], 'failed_jobs': [], 'queued_jobs': [], 'active_jobs': [], 'completed_jobs': [], 'canceled_jobs': []}
    job_ids = [job.id for job in jobs]
    step_rows = db.query(JobStep).filter(JobStep.job_id.in_(job_ids)).order_by(JobStep.created_at.asc(), JobStep.id.asc()).all()
    stale_job_ids: set[int] = set()
    for step in step_rows:
        details = _parse_step_details(step.details)
        heartbeat_at = details.get('heartbeat_at')
        if step.status != 'running' or not heartbeat_at:
            continue
        age_seconds = _heartbeat_age_seconds(heartbeat_at)
        if age_seconds is not None and age_seconds >= STEP_STALE_HEARTBEAT_SECONDS:
            stale_job_ids.add(step.job_id)
    stale_jobs = [job for job in jobs if job.id in stale_job_ids]
    failed_jobs = [job for job in jobs if job.status == 'failed' and job.id not in stale_job_ids]
    queued_jobs = [job for job in jobs if job.status == 'pending' and (job.error_message or '').startswith('Aguardando vaga na fila') and (job.id not in stale_job_ids)]
    canceled_jobs = [job for job in jobs if job.status == 'canceled']
    active_jobs = [job for job in jobs if job.status in {'pending', 'downloading', 'extracting_audio', 'transcribing', 'analyzing', 'llm_enrichment', 'cancel_requested', 'rendering'} and job.id not in stale_job_ids and (job.status != 'failed') and (not ((job.error_message or '').startswith('Aguardando vaga na fila') and job.status == 'pending'))]
    completed_jobs = [job for job in jobs if job.status == 'done']
    return {'stale_jobs': stale_jobs, 'failed_jobs': failed_jobs, 'queued_jobs': queued_jobs, 'active_jobs': active_jobs, 'completed_jobs': completed_jobs, 'canceled_jobs': canceled_jobs}


def build_publication_board(db: Session, jobs: list[Job]) -> dict:
    if not jobs:
        return {'ready_jobs': [], 'published_jobs': [], 'discarded_jobs': []}
    job_map = {job.id: job for job in jobs}
    clips = db.query(Clip).filter(Clip.job_id.in_(job_map.keys())).order_by(Clip.created_at.desc()).all()
    grouped: dict[int, list[Clip]] = {}
    for clip in clips:
        grouped.setdefault(clip.job_id, []).append(clip)

    def _build_rows(target_status: str) -> list[dict]:
        rows = []
        for job_id, job_clips in grouped.items():
            matching = [clip for clip in job_clips if clip.publication_status == target_status]
            if not matching:
                continue
            latest = matching[0]
            rows.append({'job_id': job_id, 'job_title': job_map[job_id].title or f'Job #{job_id}', 'count': len(matching), 'latest_headline': latest.headline or latest.suggested_filename or 'Sem headline', 'updated_at': latest.created_at})
        return rows[:5]
    return {'ready_jobs': _build_rows('ready'), 'published_jobs': _build_rows('published'), 'discarded_jobs': _build_rows('discarded')}


def enrich_candidates_for_view(candidates: list[dict], mode: str, feedback_profile: dict | None=None) -> list[dict]:
    enriched = []
    hybrid_weight_profile = (feedback_profile or {}).get('hybrid_weight_profile', {}) or {}
    preferred_source = hybrid_weight_profile.get('preferred_source', 'balanced')
    heuristic_weight = round(float(hybrid_weight_profile.get('heuristic_weight', 0.65) or 0.65), 2)
    llm_weight = round(float(hybrid_weight_profile.get('llm_weight', 0.35) or 0.35), 2)

    def _build_metric_item(label: str, value: float | None) -> dict:
        numeric_value = round(float(value), 2) if value is not None else None
        if numeric_value is None:
            tone = 'neutral'
        elif numeric_value >= 7:
            tone = 'strong'
        elif numeric_value >= 1:
            tone = 'positive'
        elif numeric_value <= -0.5:
            tone = 'negative'
        else:
            tone = 'neutral'
        return {'label': label, 'value': numeric_value, 'tone': tone}
    for candidate in candidates:
        start = float(candidate.get('start', 0))
        end = float(candidate.get('end', 0))
        duration = float(candidate.get('duration', 0))
        score = float(candidate.get('score', 0))
        heuristic_score = float(candidate.get('heuristic_score', score) or score)
        opening_text = candidate.get('opening_text') or candidate.get('text', '')[:180]
        closing_text = candidate.get('closing_text') or ''
        if score >= 10:
            score_label = 'muito forte'
        elif score >= 7:
            score_label = 'forte'
        elif score >= 4:
            score_label = 'medio'
        else:
            score_label = 'fraco'
        feedback_alignment_score = float(candidate.get('feedback_alignment_score', 0) or 0)
        if feedback_alignment_score >= 1.2:
            feedback_label = 'muito alinhado ao feedback'
        elif feedback_alignment_score >= 0.4:
            feedback_label = 'alinhado ao feedback'
        elif feedback_alignment_score <= -0.4:
            feedback_label = 'fora do padrão aprovado'
        else:
            feedback_label = None
        transcript_context_score = float(candidate.get('transcript_context_score', 0) or 0)
        context_reasons = []
        reason_text = (candidate.get('reason') or '').lower()
        if 'tópicos prioritários da transcrição' in reason_text or 'topicos prioritarios da transcricao' in reason_text:
            context_reasons.append('alinhado aos tópicos prioritários')
        if 'trecho promissor da análise global' in reason_text or 'trecho promissor da analise global' in reason_text:
            context_reasons.append('coincide com trecho promissor')
        if 'padrão a evitar da transcrição' in reason_text or 'padrao a evitar da transcricao' in reason_text:
            context_reasons.append('bate em padrão a evitar')
        if transcript_context_score >= 1.2:
            transcript_context_label = 'muito alinhado ao contexto global'
        elif transcript_context_score > 0:
            transcript_context_label = 'alinhado ao contexto global'
        elif transcript_context_score <= -0.8:
            transcript_context_label = 'desalinhado do contexto global'
        else:
            transcript_context_label = None
        llm_score = candidate.get('llm_score')
        llm_score = round(float(llm_score), 2) if llm_score is not None else None
        if llm_score is not None and llm_score >= 8.5:
            llm_label = 'LLM muito confiante'
        elif llm_score is not None and llm_score >= 7.0:
            llm_label = 'LLM aprovou bem'
        elif llm_score is not None:
            llm_label = 'LLM com ressalvas'
        else:
            llm_label = None
        divergence_score = None
        divergence_label = None
        divergence_summary = None
        if llm_score is not None:
            divergence_score = round(abs(heuristic_score - llm_score), 2)
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
        adaptive_blend_explanation = None
        if llm_score is not None:
            if preferred_source == 'heuristic' and divergence_score and (divergence_score >= 1.2):
                adaptive_blend_explanation = f'Este corte subiu com mais apoio da heurística porque, neste nicho, divergências recentes estão favorecendo o heurístico ({heuristic_weight} vs {llm_weight}).'
            elif preferred_source == 'llm' and divergence_score and (divergence_score >= 1.2):
                adaptive_blend_explanation = f'Este corte recebeu mais peso da LLM porque, neste nicho, divergências recentes estão favorecendo a revisão da LLM ({llm_weight} vs {heuristic_weight}).'
            elif preferred_source == 'balanced' and divergence_score and (divergence_score >= 1.2):
                adaptive_blend_explanation = f'Este corte ficou equilibrado porque o nicho ainda mantém pesos híbridos estáveis ({heuristic_weight} heurístico / {llm_weight} LLM).'
        enriched.append({**candidate, 'candidate_id': candidate.get('candidate_id'), 'status': candidate.get('status', 'pending'), 'is_favorite': bool(candidate.get('is_favorite', False)), 'editorial_notes': candidate.get('editorial_notes') or '', 'start_mmss': format_seconds_to_mmss(start), 'end_mmss': format_seconds_to_mmss(end), 'duration_mmss': format_seconds_to_mmss(duration), 'time_range_label': f'{format_seconds_to_mmss(start)} -> {format_seconds_to_mmss(end)}', 'format_label': '9:16' if mode == 'short' else '16:9', 'opening_preview': opening_text[:220], 'closing_preview': closing_text[:220], 'score_label': score_label, 'heuristic_score': round(heuristic_score, 2), 'feedback_alignment_score': round(feedback_alignment_score, 2), 'feedback_label': feedback_label, 'transcript_context_score': round(transcript_context_score, 2), 'transcript_context_label': transcript_context_label, 'transcript_context_reasons': context_reasons, 'llm_score': llm_score, 'llm_label': llm_label, 'llm_why': candidate.get('llm_why') or '', 'llm_title': candidate.get('llm_title') or '', 'llm_hook': candidate.get('llm_hook') or '', 'divergence_score': divergence_score, 'divergence_label': divergence_label, 'divergence_summary': divergence_summary, 'adaptive_blend_explanation': adaptive_blend_explanation, 'score_breakdown': [_build_metric_item('Final', score), _build_metric_item('Heurístico', heuristic_score), _build_metric_item('Contexto', transcript_context_score), _build_metric_item('LLM', llm_score)]})
    return enriched


def sort_candidates_for_view(candidates: list[dict], candidate_sort: str) -> list[dict]:
    normalized = (candidate_sort or 'hybrid').strip().lower()
    if normalized == 'divergent':
        return sorted(candidates, key=lambda item: (item.get('divergence_score') is not None, item.get('divergence_score') or -1, item.get('score', 0)), reverse=True)
    if normalized == 'heuristic':
        return sorted(candidates, key=lambda item: (item.get('heuristic_score', 0), item.get('score', 0), item.get('llm_score') or -1), reverse=True)
    if normalized == 'llm':
        return sorted(candidates, key=lambda item: (item.get('llm_score') is not None, item.get('llm_score') or -1, item.get('score', 0)), reverse=True)
    return sorted(candidates, key=lambda item: (item.get('score', 0), item.get('heuristic_score', 0), item.get('llm_score') or -1), reverse=True)


def enrich_clips_for_view(clips: list[Clip]) -> list[dict]:
    enriched = []
    for clip in clips:
        base_payload = serialize_clip(clip)
        enriched.append({**base_payload, 'format_label': '9:16' if clip.mode == 'short' else '16:9', 'publication_status_label': PUBLICATION_STATUS_LABELS.get(clip.publication_status, clip.publication_status or 'Rascunho'), 'start_mmss': format_seconds_to_mmss(clip.start_time), 'end_mmss': format_seconds_to_mmss(clip.end_time), 'duration_mmss': format_seconds_to_mmss(clip.duration)})
    return enriched


def enrich_steps_for_view(steps: list) -> list[dict]:
    enriched = []
    for step in steps:
        status = step.status or 'pending'
        if status == 'completed':
            status_label = 'Concluida'
        elif status == 'skipped':
            status_label = 'Pulada'
        elif status == 'running':
            status_label = 'Executando'
        elif status == 'failed':
            status_label = 'Falhou'
        elif status == 'exhausted':
            status_label = 'Tentativas esgotadas'
        else:
            status_label = 'Pendente'
        if step.step_name == 'llm_enrichment':
            if status == 'completed':
                status_label = 'Enriquecimento concluido'
            elif status == 'running':
                status_label = 'Enriquecendo com LLM'
            elif status == 'skipped':
                status_label = 'LLM pulada'
        try:
            details_payload = json.loads(step.details) if step.details else {}
        except json.JSONDecodeError:
            details_payload = {'raw_details': step.details}
        if not isinstance(details_payload, dict):
            details_payload = {'value': details_payload}
        duration_seconds = details_payload.get('duration_seconds')
        duration_label = f'{float(duration_seconds):.3f}s' if isinstance(duration_seconds, (int, float)) else None
        heartbeat_at = details_payload.get('heartbeat_at')
        progress_message = details_payload.get('progress_message')
        progress_percent = details_payload.get('progress_percent')
        heartbeat_age_seconds = None
        heartbeat_is_stale = False
        if heartbeat_at and status == 'running':
            try:
                heartbeat_dt = datetime.fromisoformat(str(heartbeat_at).replace('Z', '+00:00'))
                now = datetime.now(heartbeat_dt.tzinfo) if heartbeat_dt.tzinfo else datetime.utcnow()
                heartbeat_age_seconds = max(0, round((now - heartbeat_dt).total_seconds()))
                heartbeat_is_stale = heartbeat_age_seconds >= STEP_STALE_HEARTBEAT_SECONDS
            except ValueError:
                heartbeat_age_seconds = None
                heartbeat_is_stale = False
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
        if progress_message and status == 'running':
            summary_items.append(f'Atividade: {progress_message}')
        if heartbeat_at and status == 'running':
            summary_items.append(f'Ultima atividade: {heartbeat_at}')
        if heartbeat_is_stale:
            summary_items.append('Possivel travamento detectado')
        detail_items = []
        for key, value in details_payload.items():
            if key in {'reason', 'attempt', 'duration_seconds', 'forced', 'progress_message', 'heartbeat_at', 'progress_percent'}:
                continue
            if value in (None, '', [], {}):
                continue
            detail_items.append({'label': key.replace('_', ' '), 'value': value})
        enriched.append({'id': step.id, 'step_name': step.step_name, 'status': status, 'status_label': status_label, 'attempts': step.attempts or 0, 'max_attempts': MAX_STEP_ATTEMPTS, 'error_message': step.error_message, 'details': step.details, 'details_payload': details_payload, 'detail_items': detail_items, 'summary_items': summary_items, 'duration_seconds': duration_seconds, 'duration_label': duration_label, 'progress_message': progress_message, 'progress_percent': progress_percent, 'heartbeat_at': heartbeat_at, 'heartbeat_age_seconds': heartbeat_age_seconds, 'heartbeat_is_stale': heartbeat_is_stale, 'started_at': step.started_at, 'completed_at': step.completed_at, 'can_retry': status in {'failed', 'pending'}, 'can_force_retry': status == 'exhausted', 'can_reset': True})
    return enriched


def enrich_feedback_profile_for_view(profile: dict | None) -> dict | None:
    if not profile:
        return None
    successful_keywords = profile.get('successful_keywords', [])[:6]
    hybrid_weight_profile = profile.get('hybrid_weight_profile', {}) or {}
    preferred_source = hybrid_weight_profile.get('preferred_source', 'balanced')
    if preferred_source == 'heuristic':
        hybrid_summary = 'Quando há divergência, este nicho está favorecendo mais a heurística.'
    elif preferred_source == 'llm':
        hybrid_summary = 'Quando há divergência, este nicho está favorecendo mais a revisão da LLM.'
    else:
        hybrid_summary = 'Quando há divergência, o sistema ainda está equilibrado entre heurística e LLM.'
    return {**profile, 'is_ready': bool(profile.get('min_samples_reached')), 'positive_count': profile.get('positive_count', 0), 'negative_count': profile.get('negative_count', 0), 'sample_count': profile.get('sample_count', 0), 'successful_keywords_preview': successful_keywords, 'hybrid_weight_profile': {**hybrid_weight_profile, 'heuristic_weight': round(float(hybrid_weight_profile.get('heuristic_weight', 0.65) or 0.65), 2), 'llm_weight': round(float(hybrid_weight_profile.get('llm_weight', 0.35) or 0.35), 2), 'reviewed_count': int(hybrid_weight_profile.get('reviewed_count', 0) or 0), 'approved_count': int(hybrid_weight_profile.get('approved_count', 0) or 0), 'rejected_count': int(hybrid_weight_profile.get('rejected_count', 0) or 0), 'preferred_source': preferred_source, 'summary': hybrid_summary}}


def enrich_transcript_insights_for_view(raw_insights: str | None) -> dict | None:
    if not raw_insights:
        return None
    try:
        parsed = json.loads(raw_insights)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    promising_ranges = []
    for item in parsed.get('promising_ranges', [])[:6]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get('start_hint_seconds', 0) or 0)
            end = float(item.get('end_hint_seconds', 0) or 0)
        except (TypeError, ValueError):
            continue
        promising_ranges.append({'start': start, 'end': end, 'label': f'{format_seconds_to_mmss(start)} -> {format_seconds_to_mmss(end)}', 'why': item.get('why') or ''})
    return {'main_topics': parsed.get('main_topics', [])[:6], 'viral_angles': parsed.get('viral_angles', [])[:6], 'priority_keywords': parsed.get('priority_keywords', [])[:8], 'avoid_patterns': parsed.get('avoid_patterns', [])[:8], 'promising_ranges': promising_ranges}


__all__ = ['templates', 'PIPELINE_STEP_SEQUENCE', 'STEP_STALE_HEARTBEAT_SECONDS', 'JOB_AUTO_REFRESH_STATUSES', 'JOB_AUTO_REFRESH_INTERVAL_MS', 'has_active_jobs', '_normalize_mode', '_parse_step_details', '_heartbeat_age_seconds', '_build_timecode_from_parts', '_get_candidate_or_404', '_get_job_for_workspace_or_404', '_job_view_url', '_dashboard_url', '_billing_activation_url', '_is_billing_activation_message', '_get_ranked_candidates', '_ensure_page_candidates', '_get_candidates_for_job_view', 'format_seconds_to_mmss', 'filter_jobs_for_view', 'search_jobs_for_view', 'enrich_jobs_with_progress', '_build_niche_flash', '_niche_redirect', 'build_dashboard_summary', 'build_pipeline_health_summary', 'build_job_priority_groups', 'build_publication_board', 'enrich_candidates_for_view', 'sort_candidates_for_view', 'enrich_clips_for_view', 'enrich_steps_for_view', 'enrich_feedback_profile_for_view', 'enrich_transcript_insights_for_view']
