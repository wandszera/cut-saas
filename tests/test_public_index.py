import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.routes_pages import router as pages_router


class PublicIndexTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = FastAPI()
        cls.app.include_router(pages_router)
        cls.client = TestClient(cls.app)

    def test_public_index_matches_clipforge_landing(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("ClipForge", response.text)
        self.assertIn("DESTRUA", response.text)
        self.assertIn("Gerar meus clips agora", response.text)
        self.assertIn("Como funciona", response.text)
        self.assertIn("SEM ENROLACAO", response.text)
        self.assertNotIn("/jobs/dashboard/monitor", response.text)
        self.assertNotIn("/web/jobs/create", response.text)
        self.assertNotIn("Status do sistema", response.text)
