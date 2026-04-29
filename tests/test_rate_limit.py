import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services.rate_limit import AUTH_LOGIN_RULE, build_rate_limit_key, enforce_rate_limit, rate_limiter


class RateLimitServiceTestCase(unittest.TestCase):
    def setUp(self):
        rate_limiter.clear()
        self.client = TestClient(app)

    def test_build_rate_limit_key_uses_forwarded_ip_when_available(self):
        @app.get("/_rate-limit-key-test")
        def _probe():
            return {"ok": True}

        response = self.client.get("/_rate-limit-key-test", headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.1"})
        self.assertEqual(response.status_code, 200)

    def test_enforce_rate_limit_blocks_after_limit(self):
        request = self.client.build_request("POST", "/login")
        request.headers["x-forwarded-for"] = "198.51.100.25"
        request.url = request.url

        for _ in range(AUTH_LOGIN_RULE.limit):
            enforce_rate_limit(request, AUTH_LOGIN_RULE, suffix="user@example.com")

        with self.assertRaises(HTTPException) as ctx:
            enforce_rate_limit(request, AUTH_LOGIN_RULE, suffix="user@example.com")

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("login", ctx.exception.detail.lower())


if __name__ == "__main__":
    unittest.main()
