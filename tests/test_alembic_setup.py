import unittest
from pathlib import Path


class AlembicSetupTestCase(unittest.TestCase):
    def test_alembic_project_files_exist(self):
        self.assertTrue(Path("alembic.ini").exists())
        self.assertTrue(Path("alembic/env.py").exists())
        self.assertTrue(Path("alembic/script.py.mako").exists())

    def test_initial_revision_exists(self):
        revisions = list(Path("alembic/versions").glob("*initial_schema.py"))

        self.assertEqual(len(revisions), 1)
        contents = revisions[0].read_text(encoding="utf-8")
        self.assertIn("revision: str = \"20260424_0001\"", contents)
        self.assertIn("op.create_table(", contents)
        self.assertIn("\"jobs\"", contents)
        self.assertIn("\"users\"", contents)
        self.assertIn("\"workspaces\"", contents)


if __name__ == "__main__":
    unittest.main()
