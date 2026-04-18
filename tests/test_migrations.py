import unittest
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

from app.db import migrations


class MigrationsTestCase(unittest.TestCase):
    def setUp(self):
        self.test_artifacts_dir = Path("tests/.tmp")
        self.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_artifacts_dir / f"migrations_{uuid4().hex}.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        self.original_engine = migrations.engine
        migrations.engine = self.engine

    def tearDown(self):
        migrations.engine = self.original_engine
        self.engine.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

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


if __name__ == "__main__":
    unittest.main()
