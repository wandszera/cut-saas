import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.niche_definition import NicheDefinition
from app.services.niche_learning import get_feedback_profile_for_niche
from app.services.niche_registry import approve_niche, archive_niche, list_niche_definitions, reject_niche


class NicheWorkspaceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

    def _session(self):
        return self.TestingSessionLocal()

    def test_list_niche_definitions_includes_global_and_current_workspace_only(self):
        db = self._session()
        try:
            db.add_all(
                [
                    NicheDefinition(name="Global", slug="global", source="builtin", status="active"),
                    NicheDefinition(name="A", slug="a", workspace_id=1, source="custom", status="active"),
                    NicheDefinition(name="B", slug="b", workspace_id=2, source="custom", status="active"),
                ]
            )
            db.commit()

            rows = list_niche_definitions(db, workspace_id=1)
            slugs = {row["slug"] for row in rows}

            self.assertIn("global", slugs)
            self.assertIn("a", slugs)
            self.assertNotIn("b", slugs)
        finally:
            db.close()

    def test_feedback_profile_filters_candidates_by_workspace(self):
        db = self._session()
        try:
            job_a = Job(workspace_id=1, source_type="youtube", source_value="a", status="done", detected_niche="podcast")
            job_b = Job(workspace_id=2, source_type="youtube", source_value="b", status="done", detected_niche="podcast")
            db.add_all([job_a, job_b])
            db.flush()
            db.add_all(
                [
                    Candidate(
                        job_id=job_a.id,
                        mode="short",
                        start_time=0,
                        end_time=60,
                        duration=60,
                        score=9,
                        status="approved",
                        hook_score=4,
                    ),
                    Candidate(
                        job_id=job_b.id,
                        mode="short",
                        start_time=0,
                        end_time=60,
                        duration=60,
                        score=9,
                        status="approved",
                        hook_score=1,
                    ),
                ]
            )
            db.commit()

            profile = get_feedback_profile_for_niche(db, "podcast", "short", min_samples=1, workspace_id=1)

            self.assertEqual(profile["sample_count"], 1)
            self.assertEqual(profile["positive_means"]["hook_score"], 4.0)
        finally:
            db.close()

    def test_niche_moderation_is_scoped_to_workspace(self):
        db = self._session()
        try:
            db.add_all(
                [
                    NicheDefinition(
                        name="Workspace A",
                        slug="workspace-a",
                        workspace_id=1,
                        source="custom",
                        status="pending",
                    ),
                    NicheDefinition(
                        name="Workspace B",
                        slug="workspace-b",
                        workspace_id=2,
                        source="custom",
                        status="pending",
                    ),
                ]
            )
            db.commit()

            approved = approve_niche(db, "workspace-a", workspace_id=1)
            self.assertEqual(approved["status"], "active")

            with self.assertRaises(ValueError):
                approve_niche(db, "workspace-b", workspace_id=1)

            rejected = reject_niche(db, "workspace-b", workspace_id=2)
            self.assertEqual(rejected["status"], "rejected")
        finally:
            db.close()

    def test_workspace_cannot_archive_global_niche(self):
        db = self._session()
        try:
            db.add(
                NicheDefinition(
                    name="Global",
                    slug="global",
                    source="builtin",
                    status="active",
                )
            )
            db.commit()

            with self.assertRaises(ValueError):
                archive_niche(db, "global", workspace_id=1)
        finally:
            db.close()
