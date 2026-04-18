import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import patch
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
from app.services.llm_provider import LLMRateLimitError
from app.services.niche_registry import create_pending_niche
from app.services.pipeline import MAX_STEP_ATTEMPTS, process_job_pipeline


class RoutesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path("tests/.tmp")
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f"test_{uuid4().hex}.db"
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False},
        )
        cls.TestingSessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )

        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        if cls.db_path.exists():
            cls.db_path.unlink()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

    def _session(self):
        return self.TestingSessionLocal()

    def _create_job(self, **overrides) -> Job:
        payload = {
            "source_type": "youtube",
            "source_value": "https://www.youtube.com/watch?v=abc123def45",
            "status": "done",
            "title": "Video de teste",
            "video_path": "C:/tmp/video.mp4",
            "audio_path": "C:/tmp/audio.mp3",
            "transcript_path": "C:/tmp/transcript.json",
            "detected_niche": "podcast",
        }
        payload.update(overrides)

        db = self._session()
        try:
            job = Job(**payload)
            db.add(job)
            db.commit()
            db.refresh(job)
            db.expunge(job)
            return job
        finally:
            db.close()

    def _create_candidate(self, job_id: int, **overrides) -> Candidate:
        payload = {
            "job_id": job_id,
            "mode": "short",
            "start_time": 10.0,
            "end_time": 70.0,
            "duration": 60.0,
            "score": 9.2,
            "reason": "gancho forte",
            "opening_text": "abertura",
            "closing_text": "fechamento",
            "full_text": "texto completo",
            "hook_score": 2.0,
            "clarity_score": 1.5,
            "closure_score": 1.0,
            "emotion_score": 0.5,
            "duration_fit_score": 3.0,
            "transcript_context_score": 0.0,
            "llm_score": None,
            "llm_why": None,
            "llm_title": None,
            "llm_hook": None,
            "status": "pending",
        }
        payload.update(overrides)

        db = self._session()
        try:
            candidate = Candidate(**payload)
            db.add(candidate)
            db.commit()
            db.refresh(candidate)
            db.expunge(candidate)
            return candidate
        finally:
            db.close()

    def _create_clip(self, job_id: int, **overrides) -> Clip:
        payload = {
            "job_id": job_id,
            "source": "candidate",
            "mode": "short",
            "start_time": 10.0,
            "end_time": 70.0,
            "duration": 60.0,
            "score": 9.2,
            "reason": "gancho forte",
            "text": "texto do clip",
            "headline": "Titulo sugerido",
            "description": "Descricao curta",
            "hashtags": "#cortes #shorts",
            "suggested_filename": "clip-sugerido.mp4",
            "render_preset": "clean",
            "publication_status": "draft",
            "subtitles_burned": False,
            "output_path": "C:/tmp/clip.mp4",
        }
        payload.update(overrides)

        db = self._session()
        try:
            clip = Clip(**payload)
            db.add(clip)
            db.commit()
            db.refresh(clip)
            db.expunge(clip)
            return clip
        finally:
            db.close()

    def _create_niche_definition(self, **overrides) -> NicheDefinition:
        payload = {
            "name": "Nicho Custom",
            "slug": "nicho-custom",
            "description": "Descricao de teste",
            "keywords_json": '["keyword1","keyword2","keyword3"]',
            "weights_json": '{"hook": 1.1, "clarity": 1.1, "niche_bonus": 1.2}',
            "source": "custom",
            "status": "pending",
            "llm_notes": "Sugestao de teste",
        }
        payload.update(overrides)

        db = self._session()
        try:
            niche = NicheDefinition(**payload)
            db.add(niche)
            db.commit()
            db.refresh(niche)
            db.expunge(niche)
            return niche
        finally:
            db.close()

    def test_health_returns_ok(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "ok"})

    def test_create_youtube_job_success(self):
        with (
            patch(
                "app.api.routes_jobs.download_youtube_media",
                return_value={
                    "video_path": "C:/tmp/job_1.mp4",
                    "title": "Titulo do video",
                    "video_id": "abc123def45",
                },
            ),
            patch("app.api.routes_jobs.extract_audio_from_video", return_value="C:/tmp/job_1.mp3"),
            patch("app.api.routes_jobs.transcribe_audio", return_value="C:/tmp/job_1.json"),
        ):
            response = self.client.post(
                "/jobs/youtube",
                json={"url": "https://www.youtube.com/watch?v=abc123def45"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["title"], "Titulo do video")
        self.assertEqual(data["video_path"], "C:/tmp/job_1.mp4")
        self.assertEqual(data["audio_path"], "C:/tmp/job_1.mp3")
        self.assertEqual(data["transcript_path"], "C:/tmp/job_1.json")

        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.status, "done")
            self.assertEqual(job.title, "Titulo do video")
        finally:
            db.close()

    def test_create_local_video_job_success(self):
        local_video = self.test_artifacts_dir / "video_local.mp4"
        local_video.write_bytes(b"fake-video")

        with (
            patch("app.api.routes_jobs.extract_audio_from_video", return_value="C:/tmp/job_local.mp3"),
            patch("app.api.routes_jobs.transcribe_audio", return_value="C:/tmp/job_local.json"),
        ):
            response = self.client.post(
                "/jobs/local",
                json={"video_path": str(local_video), "title": "Video externo"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["source_type"], "local")
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["title"], "Video externo")
        self.assertEqual(data["video_path"], str(local_video))
        self.assertEqual(data["audio_path"], "C:/tmp/job_local.mp3")
        self.assertEqual(data["transcript_path"], "C:/tmp/job_local.json")

        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.source_type, "local")
            self.assertEqual(job.source_value, str(local_video))
            self.assertEqual(job.video_path, str(local_video))
        finally:
            db.close()

    def test_create_youtube_job_failure_marks_job_as_failed(self):
        with patch(
            "app.api.routes_jobs.download_youtube_media",
            side_effect=RuntimeError("falha simulada no download"),
        ):
            response = self.client.post(
                "/jobs/youtube",
                json={"url": "https://www.youtube.com/watch?v=abc123def45"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertIn("falha simulada no download", response.json()["detail"])

        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.status, "failed")
            self.assertIn("falha simulada no download", job.error_message)
        finally:
            db.close()

    def test_web_job_creation_redirects_and_runs_background_pipeline(self):
        with patch("app.web.routes_pages.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(
                "/web/jobs/create",
                data={"url": "https://www.youtube.com/watch?v=abc123def45"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/jobs/1/view")
        mocked_pipeline.assert_called_once_with(1)

        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.source_value, "https://www.youtube.com/watch?v=abc123def45")
            self.assertEqual(job.status, "pending")
        finally:
            db.close()

    def test_web_local_job_creation_redirects_and_runs_background_pipeline(self):
        with patch("app.web.routes_pages.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(
                "/web/jobs/create-local",
                data={"title": "Upload externo"},
                files={"video_file": ("video_form.mp4", b"fake-video", "video/mp4")},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/jobs/1/view")
        mocked_pipeline.assert_called_once_with(1)

        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.source_type, "local")
            self.assertEqual(job.title, "Upload externo")
            self.assertEqual(job.status, "pending")
            self.assertTrue(job.source_value.endswith("_video_form.mp4"))
            self.assertEqual(job.source_value, job.video_path)
        finally:
            db.close()

    def test_job_detail_page_renders_pipeline_section(self):
        job = self._create_job(status="failed")

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="failed",
                    attempts=2,
                    error_message="erro de transcrição",
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get(f"/jobs/{job.id}/view")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pipeline", response.text)
        self.assertIn("transcribing", response.text)
        self.assertIn("erro de transcrição", response.text)
        self.assertIn("Reprocessar etapa", response.text)

    def test_job_detail_page_renders_step_observability_metadata(self):
        job = self._create_job(status="failed")

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="failed",
                    attempts=2,
                    error_message="falha observada",
                    details=(
                        '{"attempt": 2, "duration_seconds": 1.234, "reason": "audio_missing", '
                        '"audio_path": "C:/tmp/audio.mp3", "forced": true}'
                    ),
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get(f"/jobs/{job.id}/view")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Duração: 1.234s", response.text)
        self.assertIn("Motivo: audio_missing", response.text)
        self.assertIn("Execução forçada", response.text)
        self.assertIn("audio path:", response.text.lower())
        self.assertIn("C:/tmp/audio.mp3", response.text)

    def test_niche_admin_page_renders_builtin_niches(self):
        response = self.client.get("/nichos")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Nichos ativos", response.text)
        self.assertIn("podcast", response.text.lower())
        self.assertIn("religioso", response.text.lower())

    def test_niche_suggestion_flow_creates_pending_niche(self):
        with patch(
            "app.web.routes_pages.create_pending_niche",
            return_value={
                "name": "Empreendedorismo Local",
                "slug": "empreendedorismo-local",
                "description": "Negócios locais, vendas e operação.",
                "keywords": ["vendas", "caixa", "cliente"],
                "status": "pending",
                "source": "custom",
                "llm_notes": "Sugestão consistente",
            },
        ) as mocked_create:
            response = self.client.post(
                "/nichos/sugerir",
                data={
                    "name": "Empreendedorismo Local",
                    "description": "Pequenos negócios, vendas e caixa.",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/nichos?message="))
        mocked_create.assert_called_once()

    def test_create_pending_niche_falls_back_when_llm_is_rate_limited(self):
        db = self._session()
        try:
            with patch(
                "app.services.niche_registry.generate_json_with_llm",
                side_effect=LLMRateLimitError("OpenAI retornou 429 Too Many Requests"),
            ):
                created = create_pending_niche(
                    db,
                    name="Empreendedorismo Local",
                    description="Pequenos negócios, vendas, caixa e atendimento.",
                )

            self.assertEqual(created["status"], "pending")
            self.assertEqual(created["source"], "custom")
            self.assertGreaterEqual(len(created["keywords"]), 5)
            self.assertIn("limite temporário", created["llm_notes"].lower())
        finally:
            db.close()

    def test_approve_pending_niche_from_page_marks_it_active(self):
        niche = self._create_niche_definition(name="Finanças Creator", slug="financas-creator", status="pending")

        response = self.client.post(f"/nichos/{niche.slug}/aprovar", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(NicheDefinition).filter(NicheDefinition.slug == niche.slug).one()
            self.assertEqual(refreshed.status, "active")
        finally:
            db.close()

    def test_archive_niche_from_page_marks_it_archived(self):
        niche = self._create_niche_definition(name="Finanças Creator", slug="financas-creator", status="active")

        response = self.client.post(f"/nichos/{niche.slug}/excluir", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(NicheDefinition).filter(NicheDefinition.slug == niche.slug).one()
            self.assertEqual(refreshed.status, "archived")
        finally:
            db.close()

    def test_job_detail_page_renders_feedback_learning_context(self):
        job = self._create_job(
            status="done",
            transcript_path="C:/tmp/transcript.json",
            detected_niche="podcast",
            transcript_insights=(
                '{"main_topics":["precificacao"],"viral_angles":["erro de margem"],'
                '"priority_keywords":["margem","preco"],"avoid_patterns":["contexto externo"],'
                '"promising_ranges":[{"start_hint_seconds":30,"end_hint_seconds":95,"why":"gancho forte"}]}'
            ),
        )
        reference_job = self._create_job(status="done", detected_niche="podcast")
        self._create_candidate(
            job.id,
            status="approved",
            mode="short",
            full_text="resultado prÃƒÂ¡tico com exemplo claro",
            opening_text="resultado prÃƒÂ¡tico com exemplo claro",
            closing_text="esse ÃƒÂ© o ponto final.",
            reason="gancho forte, alinhado aos tópicos prioritários da transcrição, coincide com trecho promissor da análise global",
            transcript_context_score=1.7,
            llm_score=8.9,
            llm_why="tem clareza, promessa concreta e funciona sem contexto externo",
            llm_title="O erro de margem que derruba seu lucro",
            llm_hook="Se a sua margem parece boa mas o lucro some, esse é o motivo",
        )

        self._create_candidate(
            reference_job.id,
            status="approved",
            mode="short",
            full_text="resultado prÃ¡tico com exemplo forte",
            hook_score=3.4,
            clarity_score=2.2,
            closure_score=2.0,
            emotion_score=1.1,
            duration_fit_score=4.0,
        )
        self._create_candidate(
            reference_job.id,
            status="rendered",
            mode="short",
            full_text="resultado real com exemplo claro",
            hook_score=3.1,
            clarity_score=2.0,
            closure_score=1.9,
            emotion_score=1.0,
            duration_fit_score=4.1,
        )

        ranked_candidates = [
            {
                "start": 12.0,
                "end": 72.0,
                "duration": 60.0,
                "score": 9.6,
                "reason": "gancho forte",
                "text": "resultado prÃ¡tico com exemplo claro",
                "opening_text": "resultado prÃ¡tico com exemplo claro",
                "closing_text": "esse Ã© o ponto final.",
                "feedback_alignment_score": 1.3,
            }
        ]

        response = self.client.get(f"/jobs/{job.id}/view")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Aprendizado", response.text)
        self.assertIn("Contexto da Transcrição", response.text)
        self.assertIn("precificacao", response.text)
        self.assertIn("00:30 -&gt; 01:35", response.text)
        self.assertIn("Base de feedback", response.text)
        self.assertIn("Aprovado", response.text)
        self.assertIn("resultado", response.text.lower())
        self.assertIn("alinhado ao contexto global", response.text)
        self.assertIn("coincide com trecho promissor", response.text)
        self.assertIn("Breakdown do score", response.text)
        self.assertIn("Heurístico", response.text)
        self.assertIn("Contexto", response.text)
        self.assertIn("Final", response.text)
        self.assertIn("LLM muito confiante", response.text)
        self.assertIn("O erro de margem que derruba seu lucro", response.text)
        self.assertIn("Peso híbrido atual", response.text)

    def test_recalibrate_feedback_from_page_redirects_back_to_job(self):
        job = self._create_job(status="done", transcript_path="C:/tmp/transcript.json", detected_niche="podcast")

        with patch("app.web.routes_pages.learn_keywords_for_niche", return_value=[]) as mocked_learn:
            response = self.client.post(
                f"/jobs/{job.id}/view/feedback/recalibrate",
                data={"mode": "short"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job.id}/view?mode=short")
        mocked_learn.assert_called_once()

    def test_candidate_editorial_actions_from_page_update_state(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id, status="pending")

        approve_response = self.client.post(
            f"/jobs/{job.id}/view/candidates/{candidate.id}/status",
            data={"mode": "short", "status": "approved"},
            follow_redirects=False,
        )
        self.assertEqual(approve_response.status_code, 303)

        favorite_response = self.client.post(
            f"/jobs/{job.id}/view/candidates/{candidate.id}/favorite",
            data={"mode": "short"},
            follow_redirects=False,
        )
        self.assertEqual(favorite_response.status_code, 303)

        notes_response = self.client.post(
            f"/jobs/{job.id}/view/candidates/{candidate.id}/notes",
            data={"mode": "short", "editorial_notes": "Abrir 2s antes e manter legenda."},
            follow_redirects=False,
        )
        self.assertEqual(notes_response.status_code, 303)

        db = self._session()
        try:
            refreshed = db.query(Candidate).filter(Candidate.id == candidate.id).one()
            self.assertEqual(refreshed.status, "approved")
            self.assertTrue(refreshed.is_favorite)
            self.assertEqual(refreshed.editorial_notes, "Abrir 2s antes e manter legenda.")
        finally:
            db.close()

    def test_render_approved_from_page_creates_clips_and_marks_candidates_rendered(self):
        job = self._create_job()
        first = self._create_candidate(job.id, status="approved", is_favorite=True, start_time=10.0, end_time=70.0, duration=60.0)
        second = self._create_candidate(job.id, status="approved", start_time=90.0, end_time=150.0, duration=60.0)

        def render_side_effect(**kwargs):
            return f"C:/tmp/page_rendered_{kwargs['clip_index']}.mp4"

        with patch("app.web.routes_pages.render_clip", side_effect=render_side_effect):
            response = self.client.post(
                f"/jobs/{job.id}/view/render-approved",
                data={"mode": "short", "render_preset": "impact"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job.id}/view?mode=short&render_preset=impact")

        db = self._session()
        try:
            refreshed = {
                candidate.id: candidate.status
                for candidate in db.query(Candidate).filter(Candidate.job_id == job.id).all()
            }
            clips = db.query(Clip).filter(Clip.job_id == job.id).all()
            self.assertEqual(refreshed[first.id], "rendered")
            self.assertEqual(refreshed[second.id], "rendered")
            self.assertEqual(len(clips), 2)
        finally:
            db.close()

    def test_render_presets_endpoint_returns_available_presets(self):
        response = self.client.get("/jobs/render-presets")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["default"], "clean")
        self.assertTrue(any(preset["key"] == "impact" for preset in data["presets"]))

    def test_list_rendered_clips_returns_editorial_package(self):
        job = self._create_job()
        clip = self._create_clip(job.id)

        response = self.client.get(f"/jobs/{job.id}/clips")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_clips"], 1)
        self.assertEqual(data["clips"][0]["clip_id"], clip.id)
        self.assertEqual(data["clips"][0]["headline"], "Titulo sugerido")
        self.assertEqual(data["clips"][0]["hashtags"], "#cortes #shorts")
        self.assertEqual(data["clips"][0]["suggested_filename"], "clip-sugerido.mp4")
        self.assertEqual(data["clips"][0]["publication_status"], "draft")

    def test_export_job_bundle_returns_zip_response(self):
        job = self._create_job()
        self._create_clip(job.id)
        export_zip = self.test_artifacts_dir / "job_1_export.zip"
        export_zip.write_bytes(b"fake zip")

        with patch("app.api.routes_jobs.build_job_export_bundle", return_value=str(export_zip)):
            response = self.client.get(f"/jobs/{job.id}/export")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("job_1_export.zip", response.headers["content-disposition"])

    def test_list_job_exports_returns_history(self):
        job = self._create_job()
        export_zip = self.test_artifacts_dir / f"job_{job.id}_export.zip"
        export_zip.write_bytes(b"fake zip")

        with patch(
            "app.api.routes_jobs.list_job_export_bundles",
            return_value=[
                {
                    "name": export_zip.name,
                    "path": str(export_zip),
                    "size_bytes": export_zip.stat().st_size,
                    "modified_at": datetime.now(UTC),
                }
            ],
        ):
            response = self.client.get(f"/jobs/{job.id}/exports")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_exports"], 1)
        self.assertEqual(data["exports"][0]["name"], export_zip.name)
        self.assertIn(f"/jobs/{job.id}/export/files/", data["exports"][0]["download_url"])

    def test_download_existing_export_returns_file(self):
        job = self._create_job()
        export_zip = self.test_artifacts_dir / f"job_{job.id}_export.zip"
        export_zip.write_bytes(b"fake zip")

        with patch(
            "app.api.routes_jobs.list_job_export_bundles",
            return_value=[
                {
                    "name": export_zip.name,
                    "path": str(export_zip),
                    "size_bytes": export_zip.stat().st_size,
                    "modified_at": datetime.now(UTC),
                }
            ],
        ):
            response = self.client.get(f"/jobs/{job.id}/export/files/{export_zip.name}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")

    def test_update_clip_publication_status_endpoint(self):
        job = self._create_job()
        clip = self._create_clip(job.id)

        response = self.client.post(f"/jobs/clips/{clip.id}/publication", params={"status": "ready"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["publication_status"], "ready")

        db = self._session()
        try:
            refreshed = db.query(Clip).filter(Clip.id == clip.id).one()
            self.assertEqual(refreshed.publication_status, "ready")
        finally:
            db.close()

    def test_home_filters_jobs_by_status(self):
        self._create_job(status="done", title="Finalizado")
        self._create_job(status="failed", title="Falhou")

        response = self.client.get("/", params={"status_filter": "failed"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Falhou", response.text)
        self.assertNotIn("Finalizado", response.text)

    def test_home_renders_dashboard_summary_cards(self):
        job_done = self._create_job(status="done", title="Com clip")
        job_active = self._create_job(status="transcribing", title="Ativo")
        self._create_candidate(job_done.id, status="approved")
        self._create_clip(job_done.id)

        with patch(
            "app.web.routes_pages.list_job_export_bundles",
            side_effect=lambda job_id: [{"name": "bundle.zip"}] if job_id == job_done.id else [],
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Jobs monitorados", response.text)
        self.assertIn("Com aprovados pendentes", response.text)
        self.assertIn("Com clips gerados", response.text)
        self.assertIn("Com export pronto", response.text)
        self.assertIn("Ativo", response.text)

    def test_home_renders_publication_board_sections(self):
        ready_job = self._create_job(status="done", title="Pronto")
        published_job = self._create_job(status="done", title="Publicado")
        discarded_job = self._create_job(status="done", title="Descartado")

        self._create_clip(ready_job.id, publication_status="ready", headline="Clip pronto")
        self._create_clip(published_job.id, publication_status="published", headline="Clip publicado")
        self._create_clip(discarded_job.id, publication_status="discarded", headline="Clip descartado")

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Prontos para publicar", response.text)
        self.assertIn("Publicados recentemente", response.text)
        self.assertIn("Descartados", response.text)
        self.assertIn("Clip pronto", response.text)
        self.assertIn("Clip publicado", response.text)
        self.assertIn("Clip descartado", response.text)

    def test_job_detail_filters_candidates_and_exports(self):
        job = self._create_job(status="done", transcript_path="C:/tmp/transcript.json", detected_niche="podcast")
        self._create_candidate(job.id, status="approved", is_favorite=True, full_text="texto favorito")
        self._create_candidate(job.id, status="rejected", full_text="texto rejeitado")
        export_zip = self.test_artifacts_dir / f"job_{job.id}_export.zip"
        export_zip.write_bytes(b"fake zip")

        with patch(
            "app.web.routes_pages.list_job_export_bundles",
            return_value=[
                {
                    "name": export_zip.name,
                    "path": str(export_zip),
                    "size_bytes": export_zip.stat().st_size,
                    "modified_at": datetime.now(UTC),
                }
            ],
        ):
            response = self.client.get(
                f"/jobs/{job.id}/view",
                params={"candidate_filter": "favorite", "export_filter": "latest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("texto favorito", response.text)
        self.assertNotIn("texto rejeitado", response.text)
        self.assertIn(export_zip.name, response.text)

    def test_job_detail_sorts_candidates_by_llm_score(self):
        job = self._create_job(status="done", transcript_path="C:/tmp/transcript.json", detected_niche="podcast")
        self._create_candidate(
            job.id,
            full_text="candidato heuristico mais forte",
            score=9.8,
            heuristic_score=9.8,
            llm_score=7.2,
        )
        self._create_candidate(
            job.id,
            full_text="candidato mais forte para llm",
            score=8.9,
            heuristic_score=8.4,
            llm_score=9.6,
        )

        response = self.client.get(
            f"/jobs/{job.id}/view",
            params={"candidate_sort": "llm"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Confiança da LLM", response.text)
        self.assertLess(
            response.text.index("candidato mais forte para llm"),
            response.text.index("candidato heuristico mais forte"),
        )

    def test_job_detail_filters_and_sorts_divergent_candidates(self):
        job = self._create_job(status="done", transcript_path="C:/tmp/transcript.json", detected_niche="podcast")
        self._create_candidate(
            job.id,
            full_text="candidato com divergencia forte",
            score=8.0,
            heuristic_score=9.5,
            llm_score=6.8,
        )
        self._create_candidate(
            job.id,
            full_text="candidato alinhado",
            score=8.4,
            heuristic_score=8.3,
            llm_score=8.1,
        )

        response = self.client.get(
            f"/jobs/{job.id}/view",
            params={"candidate_filter": "divergent", "candidate_sort": "divergent"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Maior divergência", response.text)
        self.assertIn("divergência forte", response.text)
        self.assertIn("Heurístico gostou mais do corte do que a LLM", response.text)
        self.assertIn("Explicação adaptativa", response.text)
        self.assertIn("candidato com divergencia forte", response.text)
        self.assertNotIn("candidato alinhado", response.text)

    def test_update_clip_publication_status_from_page(self):
        job = self._create_job(status="done")
        clip = self._create_clip(job.id)

        response = self.client.post(
            f"/jobs/{job.id}/view/clips/{clip.id}/publication",
            data={"mode": "short", "render_preset": "clean", "status": "published"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)

        db = self._session()
        try:
            refreshed = db.query(Clip).filter(Clip.id == clip.id).one()
            self.assertEqual(refreshed.publication_status, "published")
        finally:
            db.close()

    def test_retry_job_from_page_redirects_and_schedules_pipeline(self):
        job = self._create_job(status="failed")

        with patch("app.web.routes_pages.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(
                f"/jobs/{job.id}/view/retry",
                data={},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job.id}/view")
        mocked_pipeline.assert_called_once_with(job.id, False)

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertEqual(refreshed_job.status, "pending")
            self.assertIsNone(refreshed_job.error_message)
        finally:
            db.close()

    def test_retry_job_step_from_page_redirects_and_resets_downstream_state(self):
        job = self._create_job(
            status="failed",
            video_path="C:/tmp/video.mp4",
            audio_path="C:/tmp/audio.mp3",
            transcript_path="C:/tmp/transcript.json",
            detected_niche="podcast",
            niche_confidence="alta",
        )

        db = self._session()
        try:
            db.add_all(
                [
                    JobStep(job_id=job.id, step_name="downloading", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="extracting_audio", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="transcribing", status="failed", attempts=2),
                    JobStep(job_id=job.id, step_name="analyzing", status="completed", attempts=1),
                ]
            )
            db.commit()
        finally:
            db.close()

        with patch("app.web.routes_pages.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(
                f"/jobs/{job.id}/view/steps/transcribing/retry",
                data={},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job.id}/view")
        mocked_pipeline.assert_called_once_with(job.id, False, "transcribing")

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {
                step.step_name: step
                for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()
            }
            self.assertEqual(refreshed_job.status, "pending")
            self.assertIsNone(refreshed_job.transcript_path)
            self.assertIsNone(refreshed_job.detected_niche)
            self.assertEqual(steps["transcribing"].status, "pending")
            self.assertEqual(steps["analyzing"].status, "pending")
        finally:
            db.close()

    def test_reset_job_step_from_page_redirects_and_zeros_attempts(self):
        job = self._create_job(
            status="failed",
            video_path="C:/tmp/video.mp4",
            audio_path="C:/tmp/audio.mp3",
            transcript_path="C:/tmp/transcript.json",
            detected_niche="podcast",
            niche_confidence="alta",
        )

        db = self._session()
        try:
            db.add_all(
                [
                    JobStep(job_id=job.id, step_name="downloading", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="extracting_audio", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="transcribing", status="exhausted", attempts=MAX_STEP_ATTEMPTS),
                    JobStep(job_id=job.id, step_name="analyzing", status="failed", attempts=2),
                ]
            )
            db.commit()
        finally:
            db.close()

        response = self.client.post(
            f"/jobs/{job.id}/view/steps/transcribing/reset",
            data={},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job.id}/view")

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {
                step.step_name: step
                for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()
            }
            self.assertEqual(refreshed_job.status, "pending")
            self.assertEqual(steps["transcribing"].attempts, 0)
            self.assertEqual(steps["analyzing"].attempts, 0)
            self.assertEqual(steps["transcribing"].status, "pending")
            self.assertEqual(steps["analyzing"].status, "pending")
        finally:
            db.close()

    def test_analyze_job_returns_ranked_candidates(self):
        job = self._create_job()

        class CandidateStub:
            def __init__(self, candidate_id, start, end, score):
                self.id = candidate_id
                self.start_time = start
                self.end_time = end
                self.duration = round(end - start, 2)
                self.heuristic_score = score + 0.3
                self.score = score
                self.reason = "gancho forte"
                self.opening_text = "abertura"
                self.closing_text = "fechamento"
                self.full_text = "texto completo"
                self.hook_score = 2.0
                self.clarity_score = 1.5
                self.closure_score = 1.0
                self.emotion_score = 0.5
                self.duration_fit_score = 3.0
                self.transcript_context_score = 1.4 if candidate_id == 1 else -0.6
                self.llm_score = 8.8 if candidate_id == 1 else None
                self.llm_why = "tem começo forte e funciona sozinho" if candidate_id == 1 else None
                self.llm_title = "Título editorial" if candidate_id == 1 else None
                self.llm_hook = "Gancho editorial" if candidate_id == 1 else None
                self.status = "pending"

        saved_candidates = [
            CandidateStub(1, 10.0, 70.0, 9.2),
            CandidateStub(2, 90.0, 150.0, 8.4),
        ]

        with patch(
            "app.api.routes_jobs.regenerate_candidates_for_job",
            return_value=saved_candidates,
        ):
            response = self.client.post(
                f"/jobs/{job.id}/analyze",
                json={"mode": "short", "top_n": 1},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(data["mode"], "short")
        self.assertEqual(data["total_candidates"], 2)
        self.assertEqual(len(data["segments"]), 1)
        self.assertEqual(data["segments"][0]["candidate_id"], 1)
        self.assertEqual(data["segments"][0]["score"], 9.2)
        self.assertEqual(data["segments"][0]["transcript_context_score"], 1.4)
        self.assertEqual(data["segments"][0]["llm_score"], 8.8)
        self.assertEqual(data["segments"][0]["llm_title"], "Título editorial")
        self.assertIn("adaptive_blend_explanation", data["segments"][0])

    def test_get_job_returns_expected_payload(self):
        job = self._create_job(
            status="done",
            title="Job detalhado",
            video_path="C:/tmp/video.mp4",
            audio_path="C:/tmp/audio.mp3",
            transcript_path="C:/tmp/transcript.json",
        )

        response = self.client.get(f"/jobs/{job.id}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], job.id)
        self.assertEqual(data["title"], "Job detalhado")
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["video_path"], "C:/tmp/video.mp4")
        self.assertEqual(data["audio_path"], "C:/tmp/audio.mp3")
        self.assertEqual(data["transcript_path"], "C:/tmp/transcript.json")
        self.assertIsNone(data["video_url"])
        self.assertIsNone(data["audio_url"])
        self.assertIsNone(data["transcript_url"])
        self.assertFalse(data["can_retry"])
        self.assertFalse(data["can_force_retry"])
        self.assertFalse(data["has_exhausted_steps"])
        self.assertEqual(data["max_step_attempts"], MAX_STEP_ATTEMPTS)
        self.assertEqual(data["steps"], [])

    def test_get_job_returns_404_for_missing_job(self):
        response = self.client.get("/jobs/9999")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Job não encontrado")

    def test_list_candidates_returns_only_requested_mode_sorted_by_score(self):
        job = self._create_job()
        high = self._create_candidate(job.id, mode="short", score=9.8, start_time=10.0, end_time=70.0)
        low = self._create_candidate(job.id, mode="short", score=8.1, start_time=80.0, end_time=140.0)
        self._create_candidate(job.id, mode="long", score=9.9, start_time=150.0, end_time=450.0, duration=300.0)

        response = self.client.get(f"/jobs/{job.id}/candidates", params={"mode": "short"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(data["mode"], "short")
        self.assertEqual(data["total_candidates"], 2)
        self.assertEqual([row["candidate_id"] for row in data["candidates"]], [high.id, low.id])

    def test_list_candidates_rejects_invalid_mode(self):
        job = self._create_job()

        response = self.client.get(f"/jobs/{job.id}/candidates", params={"mode": "invalid"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "mode deve ser 'short' ou 'long'")

    def test_list_clips_returns_rendered_clips_sorted_by_created_at_desc(self):
        job = self._create_job()
        first = self._create_clip(job.id, output_path="C:/tmp/clip_a.mp4", score=7.5)
        second = self._create_clip(job.id, output_path="C:/tmp/clip_b.mp4", score=9.1)

        db = self._session()
        try:
            older = datetime.now(UTC) - timedelta(minutes=5)
            newer = datetime.now(UTC)
            db.query(Clip).filter(Clip.id == first.id).update({"created_at": older})
            db.query(Clip).filter(Clip.id == second.id).update({"created_at": newer})
            db.commit()
        finally:
            db.close()

        response = self.client.get(f"/jobs/{job.id}/clips")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(data["total_clips"], 2)
        self.assertEqual(data["clips"][0]["clip_id"], second.id)
        self.assertEqual(data["clips"][1]["clip_id"], first.id)
        self.assertEqual(data["clips"][0]["output_path"], "C:/tmp/clip_b.mp4")
        self.assertIsNone(data["clips"][0]["output_url"])

    def test_get_job_includes_persisted_pipeline_steps(self):
        job = self._create_job()

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="downloading",
                    status="completed",
                    attempts=1,
                    details='{"video_path":"C:/tmp/video.mp4"}',
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get(f"/jobs/{job.id}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(data["steps"][0]["step_name"], "downloading")
        self.assertEqual(data["steps"][0]["status"], "completed")
        self.assertFalse(data["steps"][0]["is_exhausted"])

    def test_get_job_returns_parsed_step_observability_fields(self):
        job = self._create_job(status="failed")

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="failed",
                    attempts=2,
                    error_message="falha observada",
                    details=(
                        '{"attempt": 2, "duration_seconds": 1.234, "reason": "audio_missing", '
                        '"audio_path": "C:/tmp/audio.mp3", "forced": true}'
                    ),
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get(f"/jobs/{job.id}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["steps"]), 1)
        step = data["steps"][0]
        self.assertEqual(step["details_payload"]["attempt"], 2)
        self.assertEqual(step["details_payload"]["reason"], "audio_missing")
        self.assertEqual(step["details_payload"]["audio_path"], "C:/tmp/audio.mp3")
        self.assertTrue(step["details_payload"]["forced"])
        self.assertEqual(step["duration_seconds"], 1.234)
        self.assertEqual(step["duration_label"], "1.234s")
        self.assertIn("Motivo: audio_missing", step["summary_items"])
        self.assertIn("Tentativa registrada: 2", step["summary_items"])
        self.assertIn("Duração: 1.234s", step["summary_items"])
        self.assertIn("Execução forçada", step["summary_items"])

    def test_get_job_feedback_profile_returns_learning_summary(self):
        target_job = self._create_job(status="done", detected_niche="podcast")
        reference_job = self._create_job(status="done", detected_niche="podcast")

        self._create_candidate(
            reference_job.id,
            status="approved",
            mode="short",
            full_text="resultado prÃ¡tico com exemplo forte",
            hook_score=3.5,
            clarity_score=2.4,
            closure_score=2.1,
            emotion_score=1.2,
            duration_fit_score=4.3,
        )
        self._create_candidate(
            reference_job.id,
            status="rendered",
            mode="short",
            full_text="resultado claro com exemplo real",
            hook_score=3.2,
            clarity_score=2.2,
            closure_score=2.0,
            emotion_score=1.1,
            duration_fit_score=4.0,
        )
        self._create_candidate(
            reference_job.id,
            status="rejected",
            mode="short",
            full_text="fala vaga e repetitiva sem exemplo",
            hook_score=0.8,
            clarity_score=0.5,
            closure_score=0.4,
            emotion_score=0.2,
            duration_fit_score=1.0,
        )

        response = self.client.get(f"/jobs/{target_job.id}/feedback-profile", params={"mode": "short"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], target_job.id)
        self.assertEqual(data["niche"], "podcast")
        self.assertEqual(data["mode"], "short")
        self.assertTrue(data["feedback_profile"]["min_samples_reached"])
        self.assertEqual(data["feedback_profile"]["positive_count"], 2)
        self.assertEqual(data["feedback_profile"]["negative_count"], 1)
        self.assertIn("resultado", data["feedback_profile"]["successful_keywords"])
        self.assertIn("hybrid_weight_profile", data["feedback_profile"])
        self.assertIn("heuristic_weight", data["feedback_profile"]["hybrid_weight_profile"])

    def test_recalibrate_job_feedback_profile_returns_updated_summary(self):
        target_job = self._create_job(status="done", detected_niche="podcast")

        with (
            patch("app.api.routes_jobs.learn_keywords_for_niche", return_value=[object(), object()]) as mocked_learn,
            patch(
                "app.api.routes_jobs.get_feedback_profile_for_niche",
                return_value={
                    "niche": "podcast",
                    "mode": "short",
                    "positive_count": 3,
                    "negative_count": 1,
                    "sample_count": 4,
                    "min_samples_reached": True,
                    "successful_keywords": ["resultado", "exemplo"],
                    "positive_means": {"hook_score": 3.1},
                    "negative_means": {"hook_score": 0.7},
                    "hybrid_weight_profile": {
                        "reviewed_count": 3,
                        "approved_count": 2,
                        "rejected_count": 1,
                        "preferred_source": "heuristic",
                        "heuristic_weight": 0.7,
                        "llm_weight": 0.3,
                    },
                },
            ) as mocked_profile,
        ):
            response = self.client.post(f"/jobs/{target_job.id}/feedback-profile/recalibrate", params={"mode": "short"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], target_job.id)
        self.assertEqual(data["learned_keywords_count"], 2)
        self.assertTrue(data["feedback_profile"]["min_samples_reached"])
        self.assertEqual(data["feedback_profile"]["successful_keywords"], ["resultado", "exemplo"])
        self.assertEqual(data["feedback_profile"]["hybrid_weight_profile"]["heuristic_weight"], 0.7)
        mocked_learn.assert_called_once()
        mocked_profile.assert_called_once()

    def test_process_job_pipeline_persists_steps_and_skips_existing_artifacts(self):
        job = self._create_job(
            status="pending",
            video_path="C:/tmp/existing_video.mp4",
            audio_path="C:/tmp/existing_audio.mp3",
            transcript_path="C:/tmp/existing_transcript.json",
            detected_niche="podcast",
            niche_confidence="alta",
        )

        with (
            patch("app.services.pipeline.SessionLocal", self.TestingSessionLocal),
            patch("app.services.pipeline._path_exists", return_value=True),
        ):
            process_job_pipeline(job.id)

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = db.query(JobStep).filter(JobStep.job_id == job.id).order_by(JobStep.id.asc()).all()

            self.assertEqual(refreshed_job.status, "done")
            self.assertEqual([step.step_name for step in steps], ["downloading", "extracting_audio", "transcribing", "analyzing"])
            self.assertTrue(all(step.status == "skipped" for step in steps))
        finally:
            db.close()

    def test_process_job_pipeline_persists_duration_and_attempt_metadata(self):
        job = self._create_job(
            status="pending",
            video_path="C:/tmp/existing_video.mp4",
            audio_path="C:/tmp/existing_audio.mp3",
            transcript_path=None,
            detected_niche=None,
            niche_confidence=None,
        )

        with (
            patch("app.services.pipeline.SessionLocal", self.TestingSessionLocal),
            patch(
                "app.services.pipeline._path_exists",
                side_effect=lambda value: value in {"C:/tmp/existing_video.mp4", "C:/tmp/existing_audio.mp3"},
            ),
            patch("app.services.pipeline.transcribe_audio", return_value="C:/tmp/generated_transcript.json"),
            patch("app.services.pipeline.load_transcript", return_value={"text": "texto teste"}),
            patch("app.services.pipeline.detect_niche", return_value={"niche": "podcast", "confidence": "alta"}),
            patch(
                "app.services.pipeline.analyze_transcript_context",
                return_value={"priority_keywords": ["resultado"], "promising_ranges": []},
            ),
        ):
            process_job_pipeline(job.id, start_from_step="transcribing")

        db = self._session()
        try:
            transcribing_step = (
                db.query(JobStep)
                .filter(JobStep.job_id == job.id, JobStep.step_name == "transcribing")
                .one()
            )
            details = transcribing_step.details or ""
            self.assertIn('"attempt": 1', details)
            self.assertIn('"duration_seconds":', details)
            self.assertIn('"transcript_path": "C:/tmp/generated_transcript.json"', details)

            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertIn("priority_keywords", refreshed_job.transcript_insights)
        finally:
            db.close()

    def test_process_job_pipeline_retries_failed_step_and_preserves_attempt_count(self):
        job = self._create_job(
            status="pending",
            video_path="C:/tmp/existing_video.mp4",
            audio_path="C:/tmp/existing_audio.mp3",
            transcript_path=None,
            detected_niche=None,
            niche_confidence=None,
        )

        with (
            patch("app.services.pipeline.SessionLocal", self.TestingSessionLocal),
            patch(
                "app.services.pipeline._path_exists",
                side_effect=lambda value: value in {"C:/tmp/existing_video.mp4", "C:/tmp/existing_audio.mp3"},
            ),
            patch("app.services.pipeline.transcribe_audio", side_effect=RuntimeError("falha temporária na transcrição")),
        ):
            process_job_pipeline(job.id)

        db = self._session()
        try:
            failed_job = db.query(Job).filter(Job.id == job.id).one()
            failed_step = (
                db.query(JobStep)
                .filter(JobStep.job_id == job.id, JobStep.step_name == "transcribing")
                .one()
            )
            self.assertEqual(failed_job.status, "failed")
            self.assertIn("falha temporária na transcrição", failed_job.error_message)
            self.assertEqual(failed_step.status, "failed")
            self.assertEqual(failed_step.attempts, 1)
        finally:
            db.close()

        with (
            patch("app.services.pipeline.SessionLocal", self.TestingSessionLocal),
            patch(
                "app.services.pipeline._path_exists",
                side_effect=lambda value: value in {
                    "C:/tmp/existing_video.mp4",
                    "C:/tmp/existing_audio.mp3",
                    "C:/tmp/recovered_transcript.json",
                },
            ),
            patch("app.services.pipeline.transcribe_audio", return_value="C:/tmp/recovered_transcript.json"),
            patch(
                "app.services.pipeline.load_transcript",
                return_value={"text": "texto recuperado"},
            ),
            patch(
                "app.services.pipeline.detect_niche",
                return_value={"niche": "podcast", "confidence": "media"},
            ),
        ):
            process_job_pipeline(job.id)

        db = self._session()
        try:
            recovered_job = db.query(Job).filter(Job.id == job.id).one()
            steps = (
                db.query(JobStep)
                .filter(JobStep.job_id == job.id)
                .order_by(JobStep.id.asc())
                .all()
            )
            steps_by_name = {step.step_name: step for step in steps}

            self.assertEqual(recovered_job.status, "done")
            self.assertIsNone(recovered_job.error_message)
            self.assertEqual(recovered_job.transcript_path, "C:/tmp/recovered_transcript.json")
            self.assertEqual(recovered_job.detected_niche, "podcast")
            self.assertEqual(steps_by_name["downloading"].status, "skipped")
            self.assertEqual(steps_by_name["extracting_audio"].status, "skipped")
            self.assertEqual(steps_by_name["transcribing"].status, "completed")
            self.assertEqual(steps_by_name["transcribing"].attempts, 2)
            self.assertEqual(steps_by_name["analyzing"].status, "completed")
        finally:
            db.close()

    def test_retry_job_endpoint_requeues_failed_job(self):
        job = self._create_job(status="failed")

        with patch("app.api.routes_jobs.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(f"/jobs/{job.id}/retry")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(data["status"], "pending")
        self.assertFalse(data["force"])
        mocked_pipeline.assert_called_once_with(job.id, False)

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertEqual(refreshed_job.status, "pending")
            self.assertIsNone(refreshed_job.error_message)
        finally:
            db.close()

    def test_retry_job_endpoint_rejects_done_job(self):
        job = self._create_job(status="done")

        response = self.client.post(f"/jobs/{job.id}/retry")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Apenas jobs com status 'failed' ou 'pending' podem ser reprocessados",
        )

    def test_retry_job_endpoint_blocks_exhausted_steps_without_force(self):
        job = self._create_job(status="failed")

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="exhausted",
                    attempts=MAX_STEP_ATTEMPTS,
                    error_message="falha persistente",
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.post(f"/jobs/{job.id}/retry")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Uma ou mais etapas excederam o limite de tentativas. Use force=true para tentar novamente.",
        )

    def test_retry_job_endpoint_allows_force_for_exhausted_steps(self):
        job = self._create_job(status="failed")

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="exhausted",
                    attempts=MAX_STEP_ATTEMPTS,
                    error_message="falha persistente",
                )
            )
            db.commit()
        finally:
            db.close()

        with patch("app.api.routes_jobs.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(f"/jobs/{job.id}/retry", params={"force": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["force"])
        mocked_pipeline.assert_called_once_with(job.id, True)

    def test_retry_job_step_endpoint_requeues_specific_step(self):
        job = self._create_job(
            status="failed",
            video_path="C:/tmp/video.mp4",
            audio_path="C:/tmp/audio.mp3",
            transcript_path="C:/tmp/transcript.json",
            detected_niche="podcast",
            niche_confidence="alta",
        )

        db = self._session()
        try:
            db.add_all(
                [
                    JobStep(job_id=job.id, step_name="downloading", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="extracting_audio", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="transcribing", status="failed", attempts=1, error_message="erro"),
                    JobStep(job_id=job.id, step_name="analyzing", status="completed", attempts=1),
                ]
            )
            db.commit()
        finally:
            db.close()

        with patch("app.api.routes_jobs.process_job_pipeline") as mocked_pipeline:
            response = self.client.post(f"/jobs/{job.id}/steps/transcribing/retry")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["step_name"], "transcribing")
        self.assertFalse(data["force"])
        mocked_pipeline.assert_called_once_with(job.id, False, "transcribing")

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {
                step.step_name: step
                for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()
            }
            self.assertEqual(refreshed_job.status, "pending")
            self.assertIsNone(refreshed_job.transcript_path)
            self.assertIsNone(refreshed_job.detected_niche)
            self.assertEqual(steps["downloading"].status, "completed")
            self.assertEqual(steps["extracting_audio"].status, "completed")
            self.assertEqual(steps["transcribing"].status, "pending")
            self.assertEqual(steps["transcribing"].attempts, 1)
            self.assertEqual(steps["analyzing"].status, "pending")
        finally:
            db.close()

    def test_retry_job_step_endpoint_blocks_exhausted_step_without_force(self):
        job = self._create_job(status="failed")

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="exhausted",
                    attempts=MAX_STEP_ATTEMPTS,
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.post(f"/jobs/{job.id}/steps/transcribing/retry")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "A etapa 'transcribing' excedeu o limite de tentativas. Use force=true para tentar novamente.",
        )

    def test_reset_job_step_endpoint_resets_attempts_and_downstream_state(self):
        job = self._create_job(
            status="failed",
            video_path="C:/tmp/video.mp4",
            audio_path="C:/tmp/audio.mp3",
            transcript_path="C:/tmp/transcript.json",
            detected_niche="podcast",
            niche_confidence="alta",
        )

        db = self._session()
        try:
            db.add_all(
                [
                    JobStep(job_id=job.id, step_name="downloading", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="extracting_audio", status="completed", attempts=1),
                    JobStep(job_id=job.id, step_name="transcribing", status="exhausted", attempts=MAX_STEP_ATTEMPTS),
                    JobStep(job_id=job.id, step_name="analyzing", status="failed", attempts=2),
                ]
            )
            db.commit()
        finally:
            db.close()

        response = self.client.post(f"/jobs/{job.id}/steps/transcribing/reset")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["step_name"], "transcribing")
        self.assertTrue(data["reset_attempts"])

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {
                step.step_name: step
                for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()
            }
            self.assertEqual(refreshed_job.status, "pending")
            self.assertIsNone(refreshed_job.transcript_path)
            self.assertIsNone(refreshed_job.detected_niche)
            self.assertEqual(steps["transcribing"].status, "pending")
            self.assertEqual(steps["transcribing"].attempts, 0)
            self.assertEqual(steps["analyzing"].status, "pending")
            self.assertEqual(steps["analyzing"].attempts, 0)
            self.assertEqual(steps["extracting_audio"].status, "completed")
        finally:
            db.close()

    def test_process_job_pipeline_marks_step_exhausted_after_max_attempts(self):
        job = self._create_job(
            status="pending",
            video_path="C:/tmp/existing_video.mp4",
            audio_path="C:/tmp/existing_audio.mp3",
            transcript_path=None,
            detected_niche=None,
            niche_confidence=None,
        )

        for _ in range(MAX_STEP_ATTEMPTS):
            with (
                patch("app.services.pipeline.SessionLocal", self.TestingSessionLocal),
                patch(
                    "app.services.pipeline._path_exists",
                    side_effect=lambda value: value in {"C:/tmp/existing_video.mp4", "C:/tmp/existing_audio.mp3"},
                ),
                patch("app.services.pipeline.transcribe_audio", side_effect=RuntimeError("falha persistente")),
            ):
                process_job_pipeline(job.id)

        response = self.client.get(f"/jobs/{job.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["can_retry"])
        self.assertTrue(data["can_force_retry"])
        self.assertTrue(data["has_exhausted_steps"])

        transcribing = next(step for step in data["steps"] if step["step_name"] == "transcribing")
        self.assertEqual(transcribing["status"], "exhausted")
        self.assertTrue(transcribing["is_exhausted"])
        self.assertTrue(transcribing["can_force_retry"])
        self.assertEqual(transcribing["attempts"], MAX_STEP_ATTEMPTS)

    def test_process_job_pipeline_force_allows_retry_after_exhaustion(self):
        job = self._create_job(
            status="failed",
            video_path="C:/tmp/existing_video.mp4",
            audio_path="C:/tmp/existing_audio.mp3",
            transcript_path=None,
            detected_niche=None,
            niche_confidence=None,
        )

        db = self._session()
        try:
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="exhausted",
                    attempts=MAX_STEP_ATTEMPTS,
                    error_message="falha persistente",
                )
            )
            db.commit()
        finally:
            db.close()

        with (
            patch("app.services.pipeline.SessionLocal", self.TestingSessionLocal),
            patch(
                "app.services.pipeline._path_exists",
                side_effect=lambda value: value in {
                    "C:/tmp/existing_video.mp4",
                    "C:/tmp/existing_audio.mp3",
                    "C:/tmp/forced_recovery.json",
                },
            ),
            patch("app.services.pipeline.transcribe_audio", return_value="C:/tmp/forced_recovery.json"),
            patch("app.services.pipeline.load_transcript", return_value={"text": "texto recuperado com force"}),
            patch("app.services.pipeline.detect_niche", return_value={"niche": "podcast", "confidence": "alta"}),
        ):
            process_job_pipeline(job.id, force=True)

        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            transcribing_step = (
                db.query(JobStep)
                .filter(JobStep.job_id == job.id, JobStep.step_name == "transcribing")
                .one()
            )
            self.assertEqual(refreshed_job.status, "done")
            self.assertEqual(transcribing_step.status, "completed")
            self.assertEqual(transcribing_step.attempts, MAX_STEP_ATTEMPTS + 1)
        finally:
            db.close()

    def test_render_manual_creates_clip(self):
        job = self._create_job()

        with (
            patch("app.api.routes_jobs.generate_ass_for_clip", return_value="C:/tmp/clip.ass"),
            patch("app.api.routes_jobs.render_clip", return_value="C:/tmp/clip_1.mp4"),
        ):
            response = self.client.post(
                f"/jobs/{job.id}/render-manual",
                json={
                    "start": 12.0,
                    "end": 45.0,
                    "burn_subtitles": True,
                    "mode": "short",
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(data["source"], "manual")
        self.assertEqual(data["duration"], 33.0)
        self.assertTrue(data["subtitles_burned"])
        self.assertEqual(data["output_path"], "C:/tmp/clip_1.mp4")
        self.assertIn("headline", data)
        self.assertIn("hashtags", data)
        self.assertIn("suggested_filename", data)

        db = self._session()
        try:
            clips = db.query(Clip).all()
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].job_id, job.id)
            self.assertEqual(clips[0].source, "manual")
            self.assertEqual(clips[0].output_path, "C:/tmp/clip_1.mp4")
        finally:
            db.close()

    def test_render_manual_rejects_invalid_time_range(self):
        job = self._create_job()

        response = self.client.post(
            f"/jobs/{job.id}/render-manual",
            json={
                "start": 45.0,
                "end": 12.0,
                "burn_subtitles": False,
                "mode": "short",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "end deve ser maior que start")

    def test_candidate_status_endpoints_update_status(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id)

        approve_response = self.client.post(f"/jobs/candidates/{candidate.id}/approve")
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()["status"], "approved")

        reject_response = self.client.post(f"/jobs/candidates/{candidate.id}/reject")
        self.assertEqual(reject_response.status_code, 200)
        self.assertEqual(reject_response.json()["status"], "rejected")

        reset_response = self.client.post(f"/jobs/candidates/{candidate.id}/reset")
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.json()["status"], "pending")

        db = self._session()
        try:
            refreshed = db.query(Candidate).filter(Candidate.id == candidate.id).one()
            self.assertEqual(refreshed.status, "pending")
        finally:
            db.close()

    def test_list_approved_candidates_returns_only_approved_for_mode(self):
        job = self._create_job()
        approved = self._create_candidate(
            job.id,
            status="approved",
            score=9.5,
            transcript_context_score=1.1,
            llm_score=8.2,
            llm_why="bom equilíbrio entre gancho e clareza",
            llm_title="Título aprovado pela LLM",
            llm_hook="Gancho aprovado pela LLM",
        )
        self._create_candidate(job.id, status="pending", score=8.0)
        self._create_candidate(job.id, status="approved", mode="long", score=9.9)

        response = self.client.get(f"/jobs/{job.id}/approved-candidates", params={"mode": "short"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_approved_candidates"], 1)
        self.assertEqual(data["candidates"][0]["candidate_id"], approved.id)
        self.assertEqual(data["candidates"][0]["status"], "approved")
        self.assertEqual(data["candidates"][0]["transcript_context_score"], 1.1)
        self.assertEqual(data["candidates"][0]["llm_score"], 8.2)
        self.assertEqual(data["candidates"][0]["llm_title"], "Título aprovado pela LLM")
        self.assertIn("adaptive_blend_explanation", data["candidates"][0])

    def test_render_candidate_by_id_creates_clip_and_marks_candidate_rendered(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id, status="approved")

        with patch("app.api.routes_jobs.render_clip", return_value="C:/tmp/candidate_clip.mp4"):
            response = self.client.post(f"/jobs/{job.id}/render-candidate-id/{candidate.id}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["candidate_id"], candidate.id)
        self.assertEqual(data["output_path"], "C:/tmp/candidate_clip.mp4")
        self.assertIn("headline", data)
        self.assertIn("hashtags", data)

        db = self._session()
        try:
            refreshed_candidate = db.query(Candidate).filter(Candidate.id == candidate.id).one()
            clips = db.query(Clip).all()
            self.assertEqual(refreshed_candidate.status, "rendered")
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].source, "candidate")
            self.assertEqual(clips[0].output_path, "C:/tmp/candidate_clip.mp4")
        finally:
            db.close()

    def test_render_candidate_ranked_creates_clip_from_selected_index(self):
        job = self._create_job()
        ranked_candidates = [
            {
                "start": 15.0,
                "end": 75.0,
                "duration": 60.0,
                "score": 9.4,
                "reason": "gancho forte",
                "text": "texto do primeiro candidato",
            },
            {
                "start": 90.0,
                "end": 150.0,
                "duration": 60.0,
                "score": 8.7,
                "reason": "bom fechamento",
                "text": "texto do segundo candidato",
            },
        ]

        with (
            patch("app.api.routes_jobs._get_ranked_candidates", return_value=ranked_candidates),
            patch("app.api.routes_jobs.generate_ass_for_clip", return_value="C:/tmp/ranked.ass"),
            patch("app.api.routes_jobs.render_clip", return_value="C:/tmp/ranked_clip.mp4"),
        ):
            response = self.client.post(
                f"/jobs/{job.id}/render-candidate",
                json={
                    "candidate_index": 1,
                    "burn_subtitles": True,
                    "mode": "short",
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["candidate_index"], 1)
        self.assertEqual(data["start"], 90.0)
        self.assertEqual(data["end"], 150.0)
        self.assertEqual(data["output_path"], "C:/tmp/ranked_clip.mp4")
        self.assertTrue(data["subtitles_burned"])
        self.assertIn("headline", data)
        self.assertIn("suggested_filename", data)

        db = self._session()
        try:
            clips = db.query(Clip).all()
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].source, "candidate")
            self.assertEqual(clips[0].start_time, 90.0)
        finally:
            db.close()

    def test_render_candidate_ranked_rejects_invalid_index(self):
        job = self._create_job()
        ranked_candidates = [
            {
                "start": 15.0,
                "end": 75.0,
                "duration": 60.0,
                "score": 9.4,
                "reason": "gancho forte",
                "text": "texto do primeiro candidato",
            }
        ]

        with patch("app.api.routes_jobs._get_ranked_candidates", return_value=ranked_candidates):
            response = self.client.post(
                f"/jobs/{job.id}/render-candidate",
                json={
                    "candidate_index": 4,
                    "burn_subtitles": False,
                    "mode": "short",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("candidate_index inválido", response.json()["detail"])

    def test_render_top_clips_returns_ranked_rendered_payload(self):
        job = self._create_job()
        ranked_candidates = [
            {
                "start": 10.0,
                "end": 70.0,
                "duration": 60.0,
                "score": 9.8,
                "reason": "abertura muito forte",
                "text": "texto 1",
            },
            {
                "start": 90.0,
                "end": 150.0,
                "duration": 60.0,
                "score": 8.9,
                "reason": "boa retenção",
                "text": "texto 2",
            },
        ]

        def render_side_effect(**kwargs):
            return f"C:/tmp/top_clip_{kwargs['clip_index']}.mp4"

        with (
            patch("app.api.routes_jobs._get_ranked_candidates", return_value=ranked_candidates),
            patch("app.api.routes_jobs.render_clip", side_effect=render_side_effect),
        ):
            response = self.client.post(
                f"/jobs/{job.id}/render",
                json={"top_n": 2, "burn_subtitles": False, "mode": "short"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["rendered_clips_count"], 2)
        self.assertEqual(data["format"], "9:16")
        self.assertEqual(data["clips"][0]["output_path"], "C:/tmp/top_clip_0.mp4")
        self.assertEqual(data["clips"][1]["output_path"], "C:/tmp/top_clip_1.mp4")

    def test_render_top_clips_rejects_invalid_mode(self):
        job = self._create_job()

        response = self.client.post(
            f"/jobs/{job.id}/render",
            json={"top_n": 1, "burn_subtitles": False, "mode": "invalid"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "mode deve ser 'short' ou 'long'")

    def test_render_approved_candidates_renders_all_approved_and_updates_status(self):
        job = self._create_job()
        first = self._create_candidate(job.id, status="approved", score=9.8, start_time=10.0, end_time=70.0, duration=60.0)
        second = self._create_candidate(job.id, status="approved", score=8.9, start_time=90.0, end_time=150.0, duration=60.0)
        self._create_candidate(job.id, status="pending", score=9.7, start_time=160.0, end_time=220.0, duration=60.0)

        def render_side_effect(**kwargs):
            return f"C:/tmp/rendered_{kwargs['clip_index']}.mp4"

        with patch("app.api.routes_jobs.render_clip", side_effect=render_side_effect):
            response = self.client.post(
                f"/jobs/{job.id}/render-approved",
                params={"mode": "short", "burn_subtitles": "false"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["rendered_count"], 2)
        self.assertEqual({clip["candidate_id"] for clip in data["clips"]}, {first.id, second.id})

        db = self._session()
        try:
            candidates = db.query(Candidate).order_by(Candidate.id.asc()).all()
            clips = db.query(Clip).order_by(Clip.id.asc()).all()
            statuses = {candidate.id: candidate.status for candidate in candidates}

            self.assertEqual(statuses[first.id], "rendered")
            self.assertEqual(statuses[second.id], "rendered")
            pending_candidate = next(candidate for candidate in candidates if candidate.id not in {first.id, second.id})
            self.assertEqual(pending_candidate.status, "pending")
            self.assertEqual(len(clips), 2)
        finally:
            db.close()

    def test_ranking_insights_returns_hybrid_weights_divergence_and_distribution(self):
        job = self._create_job(status="done", detected_niche="podcast")
        reference_job = self._create_job(status="done", detected_niche="podcast")

        self._create_candidate(
            reference_job.id,
            mode="short",
            status="approved",
            score=9.4,
            heuristic_score=7.2,
            llm_score=9.5,
            duration=55.0,
            full_text="erro de margem com exemplo pratico",
        )
        self._create_candidate(
            reference_job.id,
            mode="short",
            status="rejected",
            score=7.1,
            heuristic_score=8.8,
            llm_score=6.0,
            duration=95.0,
            full_text="explicacao generica sem foco",
        )

        candidate_a = self._create_candidate(
            job.id,
            mode="short",
            status="pending",
            is_favorite=True,
            score=9.6,
            heuristic_score=7.1,
            llm_score=9.6,
            duration=58.0,
            full_text="erro de margem com gancho forte e exemplo claro",
        )
        candidate_b = self._create_candidate(
            job.id,
            mode="short",
            status="approved",
            score=8.2,
            heuristic_score=8.7,
            llm_score=7.0,
            duration=92.0,
            full_text="passo a passo com contexto bom",
        )
        self._create_candidate(
            job.id,
            mode="short",
            status="rendered",
            score=6.8,
            heuristic_score=6.8,
            llm_score=None,
            duration=28.0,
            full_text="trecho curto complementar",
        )

        response = self.client.get(f"/jobs/{job.id}/ranking-insights?mode=short")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(data["mode"], "short")
        self.assertEqual(data["candidate_summary"]["total_candidates"], 3)
        self.assertEqual(data["candidate_summary"]["llm_scored_count"], 2)
        self.assertEqual(data["candidate_summary"]["favorite_count"], 1)
        self.assertEqual(data["candidate_summary"]["status_counts"]["pending"], 1)
        self.assertEqual(data["candidate_summary"]["status_counts"]["approved"], 1)
        self.assertEqual(data["candidate_summary"]["status_counts"]["rendered"], 1)
        self.assertEqual(data["divergence_summary"]["moderate_or_higher_count"], 2)
        self.assertEqual(data["divergence_summary"]["strong_count"], 1)
        self.assertEqual(data["divergence_summary"]["llm_favored_count"], 1)
        self.assertEqual(data["divergence_summary"]["heuristic_favored_count"], 1)
        self.assertEqual(data["divergence_summary"]["top_divergent_candidates"][0]["candidate_id"], candidate_a.id)
        self.assertEqual(data["divergence_summary"]["top_divergent_candidates"][1]["candidate_id"], candidate_b.id)
        self.assertEqual(data["weights"]["preferred_source"], "balanced")
        self.assertEqual(data["weights"]["heuristic_weight"], 0.6)
        self.assertEqual(data["weights"]["llm_weight"], 0.4)
        self.assertEqual(data["weights"]["reviewed_count"], 3)
        self.assertEqual(data["distribution"]["final_score"]["count"], 3)
        self.assertEqual(data["distribution"]["final_score"]["buckets"][0]["count"], 1)
        self.assertEqual(data["distribution"]["final_score"]["buckets"][1]["count"], 1)
        self.assertEqual(data["distribution"]["final_score"]["buckets"][3]["count"], 1)
        self.assertEqual(data["distribution"]["duration_seconds"]["buckets"][0]["count"], 1)
        self.assertEqual(data["distribution"]["duration_seconds"]["buckets"][1]["count"], 1)
        self.assertEqual(data["distribution"]["duration_seconds"]["buckets"][2]["count"], 0)
        self.assertEqual(data["distribution"]["duration_seconds"]["buckets"][3]["count"], 1)

    def test_ranking_insights_handles_jobs_without_candidates(self):
        job = self._create_job(status="done", detected_niche="podcast")

        response = self.client.get(f"/jobs/{job.id}/ranking-insights?mode=short")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["candidate_summary"]["total_candidates"], 0)
        self.assertEqual(data["candidate_summary"]["llm_scored_count"], 0)
        self.assertEqual(data["divergence_summary"]["compared_candidates"], 0)
        self.assertIsNone(data["distribution"]["final_score"]["avg"])
        self.assertEqual(data["distribution"]["final_score"]["buckets"][0]["count"], 0)
        self.assertEqual(data["distribution"]["duration_seconds"]["buckets"][3]["count"], 0)


if __name__ == "__main__":
    unittest.main()
