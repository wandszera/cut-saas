import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.auth import (
    authenticate_user,
    hash_password,
    parse_session_token,
    register_user,
    verify_password,
)
from app.services.rate_limit import rate_limiter
from app.web.routes_auth import router as auth_router
from app.web.security import apply_security_headers, attach_csrf_cookie, get_or_create_csrf_token


class AuthTestCase(unittest.TestCase):
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

        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        cls.app = FastAPI()

        @cls.app.middleware("http")
        async def web_security_middleware(request, call_next):
            token, token_created = get_or_create_csrf_token(request)
            request.state.csrf_token = token
            response = await call_next(request)
            apply_security_headers(request, response)
            if token_created:
                attach_csrf_cookie(response, token)
            return response

        cls.app.include_router(auth_router)
        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.client.cookies.clear()
        rate_limiter.clear()

    def _session(self):
        return self.TestingSessionLocal()

    def _csrf_token_for(self, path: str) -> str:
        self.client.get(path)
        token = self.client.cookies.get("cut_saas_csrf")
        self.assertIsNotNone(token)
        return token

    def test_password_hash_verification(self):
        password_hash = hash_password("senha-segura")

        self.assertTrue(verify_password("senha-segura", password_hash))
        self.assertFalse(verify_password("senha-errada", password_hash))

    def test_register_user_creates_account_with_hashed_password(self):
        db = self._session()
        try:
            user = register_user(
                db,
                email="auth@example.com",
                password="senha-segura",
                display_name="Usuario Auth",
                workspace_name="Workspace Auth",
            )

            self.assertEqual(user.email, "auth@example.com")
            self.assertNotEqual(user.password_hash, "senha-segura")
            self.assertTrue(verify_password("senha-segura", user.password_hash))
            self.assertIsNotNone(db.query(Workspace).first())
            self.assertIsNotNone(db.query(WorkspaceMember).first())
        finally:
            db.close()

    def test_authenticate_user_accepts_valid_credentials(self):
        db = self._session()
        try:
            register_user(
                db,
                email="login@example.com",
                password="senha-segura",
                workspace_name="Workspace Login",
            )

            user = authenticate_user(db, email="LOGIN@example.com", password="senha-segura")
            rejected = authenticate_user(db, email="login@example.com", password="senha-errada")

            self.assertIsNotNone(user)
            self.assertIsNone(rejected)
        finally:
            db.close()

    def test_register_route_sets_session_cookie(self):
        csrf_token = self._csrf_token_for("/register")
        response = self.client.post(
            "/register",
            data={
                "email": "route@example.com",
                "password": "senha-segura",
                "display_name": "Route User",
                "workspace_name": "Route Workspace",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/billing", response.headers["location"])
        cookie = response.cookies.get("cut_saas_session")
        self.assertIsNotNone(cookie)
        self.assertIsNotNone(parse_session_token(cookie))

        db = self._session()
        try:
            self.assertEqual(db.query(User).count(), 1)
            self.assertEqual(db.query(Workspace).count(), 1)
        finally:
            db.close()

    def test_login_route_rejects_invalid_credentials(self):
        db = self._session()
        try:
            register_user(
                db,
                email="reject@example.com",
                password="senha-segura",
                workspace_name="Reject Workspace",
            )
        finally:
            db.close()

        csrf_token = self._csrf_token_for("/login")
        response = self.client.post(
            "/login",
            data={"email": "reject@example.com", "password": "senha-errada", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/login", response.headers["location"])
        self.assertIsNone(response.cookies.get("cut_saas_session"))

    def test_login_route_rate_limits_repeated_attempts(self):
        csrf_token = self._csrf_token_for("/login")
        for _ in range(5):
            response = self.client.post(
                "/login",
                data={"email": "flood@example.com", "password": "senha-errada", "csrf_token": csrf_token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

        blocked = self.client.post(
            "/login",
            data={"email": "flood@example.com", "password": "senha-errada", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Muitas tentativas de login", blocked.text)
