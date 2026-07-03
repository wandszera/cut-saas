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

@router.post('/nichos/sugerir')
def suggest_niche_from_page(name: str=Form(...), description: str=Form(''), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    try:
        create_pending_niche(db, name=name, description=description.strip() or None, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), 'warning')
    except Exception as exc:
        return _niche_redirect(f'Não foi possível gerar a sugestão do nicho: {str(exc)}', 'error')
    return _niche_redirect('Sugestão gerada. Revise as palavras-chave e aprove manualmente.', 'success')


@router.post('/nichos/{slug}/aprovar')
def approve_niche_from_page(slug: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    try:
        approve_niche(db, slug, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), 'warning')
    return _niche_redirect('Nicho aprovado e ativado no motor heurístico.', 'success')


@router.post('/nichos/{slug}/rejeitar')
def reject_niche_from_page(slug: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    try:
        reject_niche(db, slug, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), 'warning')
    return _niche_redirect('Sugestão de nicho rejeitada.', 'success')


@router.post('/nichos/{slug}/excluir')
def archive_niche_from_page(slug: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    try:
        archive_niche(db, slug, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), 'warning')
    return _niche_redirect('Nicho removido da lista ativa.', 'success')


@router.post('/web/jobs/create')
def create_job_from_form(background_tasks: BackgroundTasks, url: str=Form(...), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    try:
        metadata = fetch_youtube_metadata(url)
        ensure_workspace_can_start_job(db, workspace.id)
        ensure_workspace_can_create_job(db, workspace.id, duration_seconds=float(metadata.get('duration_seconds') or 0.0))
    except HTTPException as exc:
        if _is_billing_activation_message(str(exc.detail)):
            target_url = '/billing'
        else:
            has_jobs = db.query(Job.id).filter(Job.workspace_id == workspace.id).first() is not None
            target_url = '/dashboard' if has_jobs else '/onboarding'
        return RedirectResponse(url=f"{target_url}?{urlencode({'message': str(exc.detail), 'level': 'warning'})}", status_code=303)
    except RuntimeError as exc:
        has_jobs = db.query(Job.id).filter(Job.workspace_id == workspace.id).first() is not None
        target_url = '/dashboard' if has_jobs else '/onboarding'
        return RedirectResponse(url=f"{target_url}?{urlencode({'message': str(exc), 'level': 'error'})}", status_code=303)
    job = Job(workspace_id=workspace.id, source_type='youtube', source_value=url, status='pending')
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue_pipeline_job(background_tasks, job.id)
    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post('/web/jobs/create-local')
def create_local_job_from_form(background_tasks: BackgroundTasks, video_file: UploadFile=File(...), title: str=Form(''), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    if not video_file.filename:
        raise HTTPException(status_code=400, detail='Arquivo de video nao informado')
    original_name = Path(video_file.filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
        raise HTTPException(status_code=400, detail='Formato de video nao suportado')
    stored_path = get_storage().path_for(normalize_storage_key('uploads', f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{original_name}"))
    with stored_path.open('wb') as buffer:
        shutil.copyfileobj(video_file.file, buffer)
    get_storage().sync_path(stored_path)
    try:
        ensure_workspace_can_start_job(db, workspace.id)
        ensure_workspace_can_create_job(db, workspace.id, duration_seconds=probe_video_duration_seconds(stored_path))
    except HTTPException as exc:
        if _is_billing_activation_message(str(exc.detail)):
            target_url = '/billing'
        else:
            has_jobs = db.query(Job.id).filter(Job.workspace_id == workspace.id).first() is not None
            target_url = '/dashboard' if has_jobs else '/onboarding'
        return RedirectResponse(url=f"{target_url}?{urlencode({'message': str(exc.detail), 'level': 'warning'})}", status_code=303)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        has_jobs = db.query(Job.id).filter(Job.workspace_id == workspace.id).first() is not None
        target_url = '/dashboard' if has_jobs else '/onboarding'
        return RedirectResponse(url=f"{target_url}?{urlencode({'message': str(exc), 'level': 'error'})}", status_code=303)
    resolved_title = title.strip() or Path(original_name).stem
    job = Job(workspace_id=workspace.id, source_type='local', source_value=str(stored_path), status='pending', title=resolved_title, video_path=str(stored_path))
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue_pipeline_job(background_tasks, job.id)
    video_file.file.close()
    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post('/jobs/{job_id}/view/retry')
def retry_job_from_page(job_id: int, force: str | None=Form(None), background_tasks: BackgroundTasks=None, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    force_bool = force is not None
    job.status = 'pending'
    job.error_message = None
    db.commit()
    enqueue_pipeline_job(background_tasks, job.id, force=force_bool)
    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post('/jobs/{job_id}/view/steps/{step_name}/retry')
def retry_job_step_from_page(job_id: int, step_name: str, force: str | None=Form(None), background_tasks: BackgroundTasks=None, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    force_bool = force is not None
    from app.services.pipeline import reset_pipeline_state_from_step, validate_step_name
    normalized_step = validate_step_name(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=False)
    enqueue_pipeline_job(background_tasks, job.id, force=force_bool, start_step=normalized_step)
    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post('/jobs/{job_id}/view/steps/{step_name}/reset')
def reset_job_step_from_page(job_id: int, step_name: str, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    from app.services.pipeline import reset_pipeline_state_from_step, validate_step_name
    normalized_step = validate_step_name(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=True)
    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post('/jobs/{job_id}/view/feedback/recalibrate')
def recalibrate_feedback_from_page(job_id: int, mode: str=Form('short'), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    normalized_mode = _normalize_mode(mode)
    learn_keywords_for_niche(db, niche=(job.detected_niche or 'geral').lower().strip())
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, message='Aprendizado recalibrado.'), status_code=303)


@router.post('/jobs/{job_id}/view/analyze-without-llm')
def analyze_without_llm_from_page(job_id: int, mode: str=Form('short'), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job nÃ£o encontrado')
    if not job.video_path or not job.transcript_path:
        raise HTTPException(status_code=400, detail='Job precisa de video e transcricao para concluir a analise sem LLM')
    normalized_mode = _normalize_mode(mode)
    complete_analysis_without_llm(db, job, force=True)
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, message='Analise concluida sem LLM.'), status_code=303)


@router.post('/jobs/{job_id}/view/cancel')
def cancel_job_from_page(job_id: int, mode: str=Form('short'), render_preset: str=Form(DEFAULT_PRESET), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    request_job_cancellation(db, job)
    normalized_mode = _normalize_mode(mode)
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Cancelamento solicitado. O worker vai encerrar na proxima verificacao segura.', level='warning'), status_code=303)


@router.post('/jobs/{job_id}/view/render-candidate')
def render_candidate_from_page(job_id: int, candidate_id: int=Form(...), mode: str=Form(...), render_preset: str=Form(DEFAULT_PRESET), burn_subtitles: str | None=Form(None), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    if not job.video_path:
        raise HTTPException(status_code=400, detail='Job incompleto')
    normalized_mode = _normalize_mode(mode)
    burn_subtitles_bool = burn_subtitles is not None
    if candidate_id <= 0:
        raise HTTPException(status_code=400, detail='candidate_id inválido')
    candidate = _get_candidate_or_404(db, candidate_id, workspace.id)
    if candidate.job_id != job.id or candidate.mode != normalized_mode:
        raise HTTPException(status_code=400, detail='Candidato não pertence ao job/modo informado')
    clip, _subtitles_path, _output_path = render_candidate_clip(db=db, job=job, candidate=candidate, burn_subtitles=burn_subtitles_bool, render_preset=render_preset)
    db.commit()
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Render concluido com sucesso.'), status_code=303)


@router.post('/jobs/{job_id}/view/candidates/{candidate_id}/status')
def update_candidate_status_from_page(job_id: int, candidate_id: int, mode: str=Form('short'), status: str=Form(...), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    candidate = _get_candidate_or_404(db, candidate_id, workspace.id)
    if candidate.job_id != job.id:
        raise HTTPException(status_code=400, detail='Candidato não pertence ao job informado')
    allowed_statuses = {'pending', 'approved', 'rejected'}
    normalized_status = status.lower().strip()
    if normalized_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail='Status editorial inválido')
    candidate.status = normalized_status
    db.commit()
    return RedirectResponse(url=_job_view_url(job.id, mode=mode, message='Atualizacao salva.'), status_code=303)


@router.post('/jobs/{job_id}/view/candidates/{candidate_id}/favorite')
def toggle_candidate_favorite_from_page(job_id: int, candidate_id: int, mode: str=Form('short'), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    candidate = _get_candidate_or_404(db, candidate_id, workspace.id)
    if candidate.job_id != job.id:
        raise HTTPException(status_code=400, detail='Candidato não pertence ao job informado')
    candidate.is_favorite = not bool(candidate.is_favorite)
    db.commit()
    return RedirectResponse(url=_job_view_url(job.id, mode=mode, message='Atualizacao salva.'), status_code=303)


@router.post('/jobs/{job_id}/view/candidates/{candidate_id}/notes')
def update_candidate_notes_from_page(job_id: int, candidate_id: int, mode: str=Form('short'), editorial_notes: str=Form(''), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    candidate = _get_candidate_or_404(db, candidate_id, workspace.id)
    if candidate.job_id != job.id:
        raise HTTPException(status_code=400, detail='Candidato não pertence ao job informado')
    candidate.editorial_notes = editorial_notes.strip() or None
    db.commit()
    return RedirectResponse(url=_job_view_url(job.id, mode=mode, message='Atualizacao salva.'), status_code=303)


@router.post('/jobs/{job_id}/view/candidates/bulk')
def bulk_update_candidates_from_page(job_id: int, mode: str=Form('short'), bulk_action: str=Form(...), candidate_ids: list[int]=Form([]), render_preset: str=Form(DEFAULT_PRESET), burn_subtitles: str | None=Form(None), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    normalized_mode = _normalize_mode(mode)
    normalized_action = (bulk_action or '').strip().lower()
    selected_ids = [int(candidate_id) for candidate_id in candidate_ids if int(candidate_id) > 0]
    if not selected_ids:
        return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Selecione ao menos um candidato.', level='error'), status_code=303)
    candidates = db.query(Candidate).filter(Candidate.job_id == job.id, Candidate.mode == normalized_mode, Candidate.id.in_(selected_ids)).all()
    if len(candidates) != len(set(selected_ids)):
        return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Alguns candidatos selecionados nao pertencem ao job ou modo atual.', level='error'), status_code=303)
    if normalized_action in {'approve', 'reject', 'reset'}:
        target_status = {'approve': 'approved', 'reject': 'rejected', 'reset': 'pending'}[normalized_action]
        for candidate in candidates:
            candidate.status = target_status
        db.commit()
        return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Candidatos atualizados em lote.'), status_code=303)
    if normalized_action in {'favorite_on', 'favorite_off'}:
        favorite_value = normalized_action == 'favorite_on'
        for candidate in candidates:
            candidate.is_favorite = favorite_value
        db.commit()
        return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Favoritos atualizados em lote.'), status_code=303)
    if normalized_action == 'render':
        if not job.video_path:
            raise HTTPException(status_code=400, detail='Job incompleto')
        subtitles_requested = burn_subtitles is not None
        burn_subtitles_bool = subtitles_requested and bool(job.transcript_path)
        for candidate in sorted(candidates, key=lambda row: (not bool(row.is_favorite), -(row.score or 0), row.created_at)):
            render_candidate_clip(db=db, job=job, candidate=candidate, burn_subtitles=burn_subtitles_bool, render_preset=render_preset)
        db.commit()
        flash_message = 'Selecao renderizada com sucesso.'
        flash_level = 'success'
        if subtitles_requested and (not job.transcript_path):
            flash_message = 'Selecao renderizada sem legenda embutida porque este job ainda nao possui transcricao.'
            flash_level = 'warning'
        return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message=flash_message, level=flash_level), status_code=303)
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Acao em lote invalida.', level='error'), status_code=303)


@router.post('/jobs/{job_id}/view/clips/{clip_id}/publication')
def update_clip_publication_status_from_page(job_id: int, clip_id: int, mode: str=Form('short'), render_preset: str=Form(DEFAULT_PRESET), status: str=Form(...), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    clip = db.query(Clip).filter(Clip.id == clip_id, Clip.job_id == job.id).first()
    if not clip:
        raise HTTPException(status_code=404, detail='Clip não encontrado')
    try:
        normalized_status = normalize_publication_status(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    clip.publication_status = normalized_status
    db.commit()
    return RedirectResponse(url=_job_view_url(job.id, mode=mode, render_preset=render_preset, message='Status de publicacao atualizado.'), status_code=303)


@router.post('/jobs/{job_id}/view/render-approved')
def render_approved_from_page(job_id: int, mode: str=Form('short'), render_preset: str=Form(DEFAULT_PRESET), burn_subtitles: str | None=Form(None), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    if not job.video_path:
        raise HTTPException(status_code=400, detail='Job incompleto')
    normalized_mode = _normalize_mode(mode)
    subtitles_requested = burn_subtitles is not None
    burn_subtitles_bool = subtitles_requested and bool(job.transcript_path)
    approved_candidates = db.query(Candidate).filter(Candidate.job_id == job.id, Candidate.mode == normalized_mode, Candidate.status == 'approved').order_by(Candidate.is_favorite.desc(), Candidate.score.desc(), Candidate.created_at.asc()).all()
    for candidate in approved_candidates:
        render_candidate_clip(db=db, job=job, candidate=candidate, burn_subtitles=burn_subtitles_bool, render_preset=render_preset)
    db.commit()
    flash_message = 'Render concluido com sucesso.'
    flash_level = 'success'
    if subtitles_requested and (not job.transcript_path):
        flash_message = 'Render concluido sem legenda embutida porque este job ainda nao possui transcricao.'
        flash_level = 'warning'
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message=flash_message, level=flash_level), status_code=303)


@router.post('/jobs/{job_id}/view/render-manual')
def render_manual_from_page(job_id: int, start: str=Form(''), end: str=Form(''), start_hours: str=Form(''), start_minutes: str=Form(''), start_seconds: str=Form(''), end_hours: str=Form(''), end_minutes: str=Form(''), end_seconds: str=Form(''), mode: str=Form(...), render_preset: str=Form(DEFAULT_PRESET), burn_subtitles: str | None=Form(None), db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    if not job.video_path:
        raise HTTPException(status_code=400, detail='Job incompleto')
    normalized_mode = _normalize_mode(mode)
    start_value = start.strip() or _build_timecode_from_parts(start_hours, start_minutes, start_seconds)
    end_value = end.strip() or _build_timecode_from_parts(end_hours, end_minutes, end_seconds)
    try:
        start_seconds = parse_timecode_to_seconds(start_value)
        end_seconds = parse_timecode_to_seconds(end_value)
    except ValueError:
        return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message='Tempo invalido. Use segundos, mm:ss ou hh:mm:ss.', level='error'), status_code=303)
    if end_seconds <= start_seconds:
        raise HTTPException(status_code=400, detail='end deve ser maior que start')
    subtitles_requested = burn_subtitles is not None
    burn_subtitles_bool = subtitles_requested and bool(job.transcript_path)
    clip, _subtitles_path, _output_path = render_manual_clip(db=db, job=job, start=start_seconds, end=end_seconds, mode=normalized_mode, burn_subtitles=burn_subtitles_bool, render_preset=render_preset, clip_index=9999, reason='Render manual via interface web')
    db.commit()
    flash_message = 'Render concluido com sucesso.'
    flash_level = 'success'
    if subtitles_requested and (not job.transcript_path):
        flash_message = 'Render concluido sem legenda embutida porque este job ainda nao possui transcricao.'
        flash_level = 'warning'
    return RedirectResponse(url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message=flash_message, level=flash_level), status_code=303)


@router.post('/jobs/{job_id}/view/delete')
def delete_job_from_page(job_id: int, db: Session=Depends(get_db), workspace: Workspace=Depends(require_current_workspace)):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail='Job não encontrado')
    job.status = 'deleted'
    storage = get_storage()
    for path in [job.video_path, job.audio_path, job.transcript_path, job.result_path]:
        if path:
            storage.delete(path)
    clips = db.query(Clip).filter(Clip.job_id == job.id).all()
    for clip in clips:
        if clip.output_path:
            storage.delete(clip.output_path)
    for export in list_job_export_bundles(job.id):
        if export.get("path"):
            storage.delete(export["path"])
    db.commit()
    return RedirectResponse(url="/dashboard?message=Job+excluido+com+sucesso.+Espaco+liberado+no+armazenamento.", status_code=303)
