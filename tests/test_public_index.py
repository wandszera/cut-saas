import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.pages import router as pages_router


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
        self.assertIn("UMA CONVERSA.", response.text)
        self.assertIn("Criar meu primeiro corte", response.text)
        self.assertIn("Como funciona", response.text)
        self.assertIn("Um plano para cada ritmo.", response.text)
        self.assertNotIn("/jobs/dashboard/monitor", response.text)
        self.assertNotIn("/web/jobs/create", response.text)
        self.assertNotIn("Status do sistema", response.text)
