import unittest
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.candidate import Candidate
from app.models.job import Job
from app.services.niche_learning import get_feedback_profile_for_niche
from app.services.scoring import score_candidates


class FeedbackLearningTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path("tests/.tmp")
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f"feedback_{uuid4().hex}.db"
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False},
        )
        cls.TestingSessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )

    @classmethod
    def tearDownClass(cls):
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
            "source_value": "https://www.youtube.com/watch?v=feedback12345",
            "status": "done",
            "title": "Job de feedback",
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
            "score": 9.0,
            "reason": "gancho forte",
            "opening_text": "abertura",
            "closing_text": "fechamento",
            "full_text": "texto completo com resultado e exemplo",
            "hook_score": 3.0,
            "clarity_score": 2.0,
            "closure_score": 2.0,
            "emotion_score": 1.0,
            "duration_fit_score": 4.0,
            "status": "approved",
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

    def test_get_feedback_profile_for_niche_aggregates_positive_and_negative_signals(self):
        podcast_job_a = self._create_job(detected_niche="podcast")
        podcast_job_b = self._create_job(detected_niche="podcast")
        self._create_job(detected_niche="ciencia")

        self._create_candidate(
            podcast_job_a.id,
            status="approved",
            full_text="resultado prático com exemplo forte",
            hook_score=3.5,
            clarity_score=2.4,
            closure_score=2.1,
            emotion_score=1.2,
            duration_fit_score=4.3,
        )
        self._create_candidate(
            podcast_job_b.id,
            status="rendered",
            full_text="resultado claro com exemplo real",
            hook_score=3.2,
            clarity_score=2.2,
            closure_score=2.0,
            emotion_score=1.1,
            duration_fit_score=4.0,
        )
        self._create_candidate(
            podcast_job_b.id,
            status="rejected",
            full_text="fala vaga e repetitiva sem exemplo",
            hook_score=0.8,
            clarity_score=0.5,
            closure_score=0.4,
            emotion_score=0.2,
            duration_fit_score=1.0,
        )

        db = self._session()
        try:
            profile = get_feedback_profile_for_niche(db, "podcast", "short")
        finally:
            db.close()

        self.assertTrue(profile["min_samples_reached"])
        self.assertEqual(profile["positive_count"], 2)
        self.assertEqual(profile["negative_count"], 1)
        self.assertGreater(profile["positive_means"]["hook_score"], profile["negative_means"]["hook_score"])
        self.assertIn("resultado", profile["successful_keywords"])
        self.assertIn("exemplo", profile["successful_keywords"])

    def test_score_candidates_uses_feedback_profile_to_prefer_historically_successful_shape(self):
        feedback_profile = {
            "min_samples_reached": True,
            "positive_means": {
                "hook_score": 3.2,
                "clarity_score": 2.3,
                "closure_score": 2.0,
                "emotion_score": 1.0,
                "duration_fit_score": 4.1,
                "duration": 62.0,
            },
            "negative_means": {
                "hook_score": 0.8,
                "clarity_score": 0.6,
                "closure_score": 0.5,
                "emotion_score": 0.2,
                "duration_fit_score": 1.2,
                "duration": 135.0,
            },
            "successful_keywords": ["resultado", "exemplo", "prático"],
        }

        candidates = [
            {
                "start": 0.0,
                "end": 62.0,
                "duration": 62.0,
                "text": "O resultado prático aparece nesse exemplo e eu vou explicar por que isso funciona no fim.",
                "opening_text": "O resultado prático aparece nesse exemplo",
                "middle_text": "eu vou explicar por que isso funciona",
                "closing_text": "no fim.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 70.0,
                "end": 180.0,
                "duration": 110.0,
                "text": "É tipo assim cara, uma fala vaga e repetitiva que não mostra resultado nem exemplo nenhum.",
                "opening_text": "É tipo assim cara",
                "middle_text": "uma fala vaga e repetitiva",
                "closing_text": "que não mostra resultado nem exemplo nenhum.",
                "segments_count": 3,
                "pause_before": 0.0,
                "pause_after": 0.0,
                "starts_clean": False,
                "ends_clean": False,
            },
        ]

        ranked = score_candidates(
            candidates,
            mode="short",
            niche="podcast",
            feedback_profile=feedback_profile,
        )

        self.assertGreater(ranked[0]["feedback_alignment_score"], ranked[1]["feedback_alignment_score"])
        self.assertIn("alinhado com feedback positivo", ranked[0]["reason"])
        self.assertEqual(ranked[0]["start"], 0.0)


if __name__ == "__main__":
    unittest.main()
