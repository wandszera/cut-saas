import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.accounts import create_user_with_workspace


class AccountsTestCase(unittest.TestCase):
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

    def test_create_user_with_workspace_creates_owner_membership(self):
        db = self._session()
        try:
            user, workspace, membership = create_user_with_workspace(
                db,
                email="Owner@Example.com",
                password_hash="hashed-password",
                display_name="Owner Name",
                workspace_name="Agencia de Cortes",
            )

            self.assertEqual(user.email, "owner@example.com")
            self.assertEqual(workspace.name, "Agencia de Cortes")
            self.assertEqual(workspace.slug, "agencia-de-cortes")
            self.assertEqual(workspace.owner_user_id, user.id)
            self.assertEqual(membership.workspace_id, workspace.id)
            self.assertEqual(membership.user_id, user.id)
            self.assertEqual(membership.role, "owner")
            self.assertEqual(membership.status, "active")
        finally:
            db.close()

    def test_create_user_with_workspace_rejects_duplicate_email(self):
        db = self._session()
        try:
            create_user_with_workspace(
                db,
                email="same@example.com",
                password_hash="hashed-password",
                workspace_name="Primeiro",
            )

            with self.assertRaises(ValueError):
                create_user_with_workspace(
                    db,
                    email="SAME@example.com",
                    password_hash="hashed-password",
                    workspace_name="Segundo",
                )
        finally:
            db.close()

    def test_create_user_with_workspace_generates_unique_workspace_slug(self):
        db = self._session()
        try:
            first_user, first_workspace, _ = create_user_with_workspace(
                db,
                email="first@example.com",
                password_hash="hashed-password",
                workspace_name="Studio Viral",
            )
            second_user, second_workspace, _ = create_user_with_workspace(
                db,
                email="second@example.com",
                password_hash="hashed-password",
                workspace_name="Studio Viral",
            )

            self.assertNotEqual(first_user.id, second_user.id)
            self.assertEqual(first_workspace.slug, "studio-viral")
            self.assertEqual(second_workspace.slug, "studio-viral-2")
        finally:
            db.close()

    def test_models_are_registered_in_metadata(self):
        tables = Base.metadata.tables

        self.assertIn(User.__tablename__, tables)
        self.assertIn(Workspace.__tablename__, tables)
        self.assertIn(WorkspaceMember.__tablename__, tables)
