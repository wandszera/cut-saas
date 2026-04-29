import unittest
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.models.job import Job
from app.models.clip import Clip
from app.models.candidate import Candidate
from app.models.job_step import JobStep
from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token
from app.web.routes_auth import router as auth_router
from app.web.routes_billing import router as billing_pages_router
from app.web.security import (
    CSRF_COOKIE_NAME,
    apply_security_headers,
    attach_csrf_cookie,
    get_or_create_csrf_token,
)


class WebSecurityTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()

        @app.middleware("http")
        async def web_security_middleware(request: Request, call_next):
            token, token_created = get_or_create_csrf_token(request)
            request.state.csrf_token = token
            response = await call_next(request)
            apply_security_headers(request, response)
            if token_created:
                attach_csrf_cookie(response, token)
            return response

        @app.get("/security-probe")
        def security_probe():
            return HTMLResponse("<html><body>ok</body></html>")

        app.include_router(auth_router)
        app.include_router(billing_pages_router)
        app.dependency_overrides[get_db] = override_get_db

        cls.app = app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.client.cookies.clear()
        self.user_id, self.workspace_id = self._create_user_workspace()

    def _create_user_workspace(self) -> tuple[int, int]:
        db = self.TestingSessionLocal()
        try:
            user, workspace, _membership = create_user_with_workspace(
                db,
                email=f"security-{uuid4().hex}@example.com",
                password_hash="hashed-password",
                workspace_name="Security Workspace",
            )
            db.commit()
            db.refresh(user)
            db.refresh(workspace)
            return user.id, workspace.id
        finally:
            db.close()

    def test_get_request_sets_csrf_cookie_and_security_headers(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.cookies.get(CSRF_COOKIE_NAME))
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["referrer-policy"], "same-origin")

    def test_register_requires_valid_csrf_token(self):
        self.client.get("/register")
        csrf_token = self.client.cookies.get(CSRF_COOKIE_NAME)

        rejected = self.client.post(
            "/register",
            data={
                "email": "csrf@example.com",
                "password": "senha-segura",
                "display_name": "Usuario CSRF",
                "workspace_name": "Workspace CSRF",
            },
            follow_redirects=False,
        )
        accepted = self.client.post(
            "/register",
            data={
                "email": "csrf@example.com",
                "password": "senha-segura",
                "display_name": "Usuario CSRF",
                "workspace_name": "Workspace CSRF",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 303)
        self.assertIn("/billing", accepted.headers["location"])

    def test_billing_cancel_rejects_post_without_csrf_token(self):
        self.client.cookies.set("cut_saas_session", create_session_token(self.user_id))
        self.client.get("/billing")
        csrf_token = self.client.cookies.get(CSRF_COOKIE_NAME)

        rejected = self.client.post("/billing/cancel", follow_redirects=False)
        accepted = self.client.post(
            "/billing/cancel",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 303)
        self.assertIn("/billing", accepted.headers["location"])


if __name__ == "__main__":
    unittest.main()
