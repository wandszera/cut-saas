import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.database import Base, get_db
from app.main import app
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.niche_definition import NicheDefinition
from app.models.job_step import JobStep
from app.models.subscription import Subscription
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token
from app.services.llm_provider import LLMRateLimitError
from app.services.niche_registry import create_pending_niche
from app.services.pipeline import MAX_STEP_ATTEMPTS, process_job_pipeline
from app.services.candidates import _get_mode_candidate_limits
from app.services.segmentation import split_segments_into_time_chunks
from tests.routes.base import RoutesTestCase

class TestWebDashboard(RoutesTestCase):
    def test_niche_admin_page_renders_builtin_niches(self):
        response = self.client.get('/nichos')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Nichos ativos', response.text)
        self.assertIn('podcast', response.text.lower())
        self.assertIn('religioso', response.text.lower())


    def test_system_status_page_renders_diagnostics(self):
        response = self.client.get('/system')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Diagnóstico Operacional', response.text)
        self.assertIn('Checks do ambiente', response.text)
        self.assertIn('Configuração carregada', response.text)


    def test_account_profile_page_renders_user_workspace_and_metrics(self):
        job = self._create_job(status='done', title='Conta ativa')
        self._create_candidate(job.id, status='approved')
        self._create_clip(job.id)
        response = self.client.get('/account')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Perfil da conta', response.text)
        self.assertIn('Routes Workspace', response.text)
        self.assertIn('Jobs no workspace', response.text)
        self.assertIn('Candidatos aprovados', response.text)
        self.assertIn('Clips gerados', response.text)


    def test_niche_suggestion_flow_creates_pending_niche(self):
        with patch('app.web.pages.actions.create_pending_niche', return_value={'name': 'Empreendedorismo Local', 'slug': 'empreendedorismo-local', 'description': 'Negócios locais, vendas e operação.', 'keywords': ['vendas', 'caixa', 'cliente'], 'status': 'pending', 'source': 'custom', 'llm_notes': 'Sugestão consistente'}) as mocked_create:
            response = self.client.post('/nichos/sugerir', data={'name': 'Empreendedorismo Local', 'description': 'Pequenos negócios, vendas e caixa.'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers['location'].startswith('/nichos?message='))
        mocked_create.assert_called_once()


    def test_create_pending_niche_falls_back_when_llm_is_rate_limited(self):
        db = self._session()
        try:
            with patch('app.services.niche_registry.generate_json_with_llm', side_effect=LLMRateLimitError('OpenAI retornou 429 Too Many Requests')):
                created = create_pending_niche(db, name='Empreendedorismo Local', description='Pequenos negócios, vendas, caixa e atendimento.')
            self.assertEqual(created['status'], 'pending')
            self.assertEqual(created['source'], 'custom')
            self.assertGreaterEqual(len(created['keywords']), 5)
            self.assertIn('limite temporário', created['llm_notes'].lower())
        finally:
            db.close()


    def test_approve_pending_niche_from_page_marks_it_active(self):
        niche = self._create_niche_definition(name='Finanças Creator', slug='financas-creator', status='pending')
        response = self.client.post(f'/nichos/{niche.slug}/aprovar', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(NicheDefinition).filter(NicheDefinition.slug == niche.slug).one()
            self.assertEqual(refreshed.status, 'active')
        finally:
            db.close()


    def test_archive_niche_from_page_marks_it_archived(self):
        niche = self._create_niche_definition(name='Finanças Creator', slug='financas-creator', status='active')
        response = self.client.post(f'/nichos/{niche.slug}/excluir', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(NicheDefinition).filter(NicheDefinition.slug == niche.slug).one()
            self.assertEqual(refreshed.status, 'archived')
        finally:
            db.close()


    def test_home_filters_jobs_by_status(self):
        self._create_job(status='done', title='Finalizado')
        self._create_job(status='failed', title='Falhou')
        response = self.client.get('/', params={'status_filter': 'failed'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Falhou', response.text)
        self.assertNotIn('Finalizado', response.text)


    def test_home_filters_jobs_by_search_query(self):
        self._create_job(status='done', title='Podcast de vendas')
        self._create_job(status='done', title='Resumo financeiro')
        response = self.client.get('/', params={'search_query': 'vendas'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Podcast de vendas', response.text)
        self.assertNotIn('Resumo financeiro', response.text)


    def test_empty_dashboard_redirects_to_onboarding(self):
        response = self.client.get('/dashboard', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/onboarding')


    def test_onboarding_page_guides_first_job_creation(self):
        response = self.client.get('/onboarding')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Primeiro processamento', response.text)
        self.assertIn('action="/web/jobs/create"', response.text)
        self.assertIn('action="/web/jobs/create-local"', response.text)
        self.assertIn('Pronto', response.text)


    def test_onboarding_redirects_to_first_job_after_creation(self):
        job = self._create_job(status='pending', title='Primeiro job')
        response = self.client.get('/onboarding', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], f'/jobs/{job.id}/view')


    def test_home_renders_dashboard_summary_cards(self):
        job_done = self._create_job(status='done', title='Com clip')
        job_active = self._create_job(status='transcribing', title='Ativo')
        self._create_job(status='pending', title='Na fila', error_message='Aguardando vaga na fila de processamento.')
        self._create_candidate(job_done.id, status='approved')
        self._create_clip(job_done.id)
        with patch('app.web.pages.helpers.list_job_export_bundles', side_effect=lambda job_id: [{'name': 'bundle.zip'}] if job_id == job_done.id else []):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Jobs monitorados', response.text)
        self.assertIn('Com aprovados pendentes', response.text)
        self.assertIn('Com clips gerados', response.text)
        self.assertIn('Com export pronto', response.text)
        self.assertIn('Ativo', response.text)
        self.assertIn('aguardando vaga', response.text)
        self.assertIn('Fila tecnica', response.text)
        self.assertIn('Heartbeat envelhecido', response.text)
        self.assertIn('Falhas e cancelamentos', response.text)


    def test_home_renders_queue_waiting_label_for_pending_slot_job(self):
        self._create_job(status='pending', title='Esperando slot', error_message='Aguardando vaga na fila de processamento.')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Aguardando slot livre', response.text)
        self.assertIn('data-dashboard-monitor="/jobs/dashboard/monitor"', response.text)
        self.assertIn('/assets/scripts/pages/dashboard.js', response.text)
        self.assertIn('Na fila tecnica', response.text)


    def test_home_prioritizes_stale_queue_and_canceled_groups(self):
        stale_job = self._create_job(status='analyzing', title='Travado')
        self._create_job(status='pending', title='Na fila', error_message='Aguardando vaga na fila de processamento.')
        self._create_job(status='canceled', title='Cancelado manualmente')
        self._create_job(status='llm_enrichment', title='LLM rodando')
        db = self._session()
        try:
            db.add(JobStep(job_id=stale_job.id, step_name='analyzing', status='running', attempts=1, details='{"heartbeat_at": "2026-04-18T18:00:00+00:00"}'))
            db.commit()
        finally:
            db.close()
        with patch('app.web.pages.helpers._heartbeat_age_seconds', return_value=3601):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Possivel travamento', response.text)
        self.assertIn('Na fila tecnica', response.text)
        self.assertIn('Cancelados', response.text)
        self.assertIn('Verificar heartbeat', response.text)
        self.assertIn('Reprocessar job', response.text)
        self.assertIn('Cancelar processamento', response.text)
        self.assertIn('Concluir sem LLM', response.text)


    def test_home_renders_publication_board_sections(self):
        ready_job = self._create_job(status='done', title='Pronto')
        published_job = self._create_job(status='done', title='Publicado')
        discarded_job = self._create_job(status='done', title='Descartado')
        self._create_clip(ready_job.id, publication_status='ready', headline='Clip pronto')
        self._create_clip(published_job.id, publication_status='published', headline='Clip publicado')
        self._create_clip(discarded_job.id, publication_status='discarded', headline='Clip descartado')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Prontos para publicar', response.text)
        self.assertIn('Publicados recentemente', response.text)
        self.assertIn('Descartados', response.text)
        self.assertIn('Clip pronto', response.text)
        self.assertIn('Clip publicado', response.text)
        self.assertIn('Clip descartado', response.text)


    def test_system_page_includes_runtime_readiness_section(self):
        response = self.client.get('/system')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Prontidao operacional', response.text)
        self.assertIn('Runtime', response.text)


    def test_dashboard_surfaces_runtime_worker_alert(self):
        with patch('app.web.pages.helpers.build_runtime_readiness', return_value={'ready': False, 'checks_ok': 6, 'checks_total': 8, 'checks': [{'name': 'Worker backlog', 'ok': False, 'status': 'erro', 'detail': 'pending_jobs=2 | active_jobs=1 | running_steps=1 | stale_running_steps=1'}]}):
            response = self.client.get('/dashboard?message=runtime')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Runtime com bloqueios operacionais.', response.text)
        self.assertIn('Worker precisa de atencao imediata', response.text)


    def test_onboarding_does_not_redirect_to_first_job_when_message_is_passed(self):
        job = self._create_job(status='pending', title='Primeiro job')
        response = self.client.get('/onboarding', params={'message': 'Ocorreu um erro no processamento do vídeo', 'message_level': 'error'}, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Primeiro processamento', response.text)
        self.assertIn('Ocorreu um erro no processamento do vídeo', response.text)

