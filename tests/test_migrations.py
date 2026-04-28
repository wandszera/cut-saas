import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.db import migrations


class MigrationsTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.original_engine = migrations.engine
        migrations.engine = self.engine

    def tearDown(self):
        migrations.engine = self.original_engine
        self.engine.dispose()

    def test_ensure_niche_definition_columns_creates_table_when_missing(self):
        migrations.ensure_niche_definition_columns()

        inspector = inspect(self.engine)
        self.assertIn("niche_definitions", inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("niche_definitions")}
        self.assertTrue(
            {
                "id",
                "name",
                "slug",
                "description",
                "keywords_json",
                "weights_json",
                "source",
                "status",
                "llm_notes",
                "created_at",
                "updated_at",
            }.issubset(columns)
        )

    def test_ensure_niche_definition_columns_backfills_legacy_table(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE niche_definitions (
                        id INTEGER NOT NULL PRIMARY KEY,
                        name VARCHAR NOT NULL,
                        slug VARCHAR NOT NULL
                    )
                    """
                )
            )

        migrations.ensure_niche_definition_columns()

        inspector = inspect(self.engine)
        columns = {column["name"] for column in inspector.get_columns("niche_definitions")}
        self.assertIn("keywords_json", columns)
        self.assertIn("weights_json", columns)
        self.assertIn("status", columns)
        self.assertIn("llm_notes", columns)

    def test_ensure_saas_account_tables_creates_account_schema(self):
        migrations.ensure_saas_account_tables()

        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()
        self.assertIn("users", table_names)
        self.assertIn("workspaces", table_names)
        self.assertIn("workspace_members", table_names)

        user_columns = {column["name"] for column in inspector.get_columns("users")}
        workspace_columns = {column["name"] for column in inspector.get_columns("workspaces")}
        member_columns = {column["name"] for column in inspector.get_columns("workspace_members")}

        self.assertTrue({"id", "email", "password_hash", "status"}.issubset(user_columns))
        self.assertTrue({"id", "name", "slug", "owner_user_id", "status"}.issubset(workspace_columns))
        self.assertTrue({"id", "workspace_id", "user_id", "role", "status"}.issubset(member_columns))

    def test_ensure_job_workspace_columns_adds_workspace_id(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE jobs (
                        id INTEGER NOT NULL PRIMARY KEY,
                        source_type VARCHAR NOT NULL,
                        source_value TEXT NOT NULL,
                        status VARCHAR NOT NULL
                    )
                    """
                )
            )

        migrations.ensure_job_workspace_columns()

        inspector = inspect(self.engine)
        columns = {column["name"] for column in inspector.get_columns("jobs")}
        self.assertIn("workspace_id", columns)

    def test_ensure_niche_columns_adds_workspace_id(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE niche_definitions (
                        id INTEGER NOT NULL PRIMARY KEY,
                        name VARCHAR NOT NULL,
                        slug VARCHAR NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE niche_keywords (
                        id INTEGER NOT NULL PRIMARY KEY,
                        niche VARCHAR NOT NULL,
                        keyword VARCHAR NOT NULL
                    )
                    """
                )
            )

        migrations.ensure_niche_definition_columns()
        migrations.ensure_niche_keyword_workspace_columns()

        inspector = inspect(self.engine)
        definition_columns = {column["name"] for column in inspector.get_columns("niche_definitions")}
        keyword_columns = {column["name"] for column in inspector.get_columns("niche_keywords")}
        self.assertIn("workspace_id", definition_columns)
        self.assertIn("workspace_id", keyword_columns)


if __name__ == "__main__":
    unittest.main()
