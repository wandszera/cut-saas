import unittest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


class ProxySecurityTestCase(unittest.TestCase):
    def _build_app(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["app.example.com", "testserver"])

        @app.get("/probe")
        async def probe(request: Request):
            return JSONResponse(
                {
                    "host": request.url.hostname,
                    "scheme": request.url.scheme,
                }
            )

        return app

    def test_trusted_host_blocks_unknown_host(self):
        client = TestClient(self._build_app())

        response = client.get("/probe", headers={"host": "evil.example.com"})

        self.assertEqual(response.status_code, 400)

    def test_proxy_headers_update_request_scheme_for_trusted_proxy(self):
        client = TestClient(self._build_app())

        response = client.get(
            "/probe",
            headers={
                "host": "app.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "app.example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scheme"], "https")
        self.assertEqual(response.json()["host"], "app.example.com")


if __name__ == "__main__":
    unittest.main()
