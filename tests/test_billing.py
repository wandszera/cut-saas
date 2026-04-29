import hashlib
import hmac
import json
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes_billing import router as billing_router
from app.db.database import Base, get_db
from app.models.job import Job
from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token
from app.services.billing import MockBillingAdapter, StripeBillingAdapter, create_checkout_session
from app.services import billing as billing_service
from app.services.rate_limit import rate_limiter
from app.web.routes_billing import router as billing_pages_router
from app.services.quota import ensure_workspace_can_start_job, get_workspace_quota_status


class BillingTestCase(unittest.TestCase):
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

        cls.app = FastAPI()
        cls.app.include_router(billing_router)
        cls.app.include_router(billing_pages_router)
        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        rate_limiter.clear()
        self.client.cookies.clear()
        self.user_id, self.workspace_id = self._create_user_workspace()
        self.client.cookies.set("cut_saas_session", create_session_token(self.user_id))

    def _create_user_workspace(self) -> tuple[int, int]:
        db = self.TestingSessionLocal()
        try:
            user, workspace, _membership = create_user_with_workspace(
                db,
                email=f"billing-{uuid4().hex}@example.com",
                password_hash="hashed-password",
                workspace_name="Billing Workspace",
            )
            db.commit()
            db.refresh(user)
            db.refresh(workspace)
            return user.id, workspace.id
        finally:
            db.close()

    def _record_usage(self, minutes: float) -> None:
        db = self.TestingSessionLocal()
        try:
            db.add(
                UsageEvent(
                    workspace_id=self.workspace_id,
                    event_type="video_processed",
                    quantity=minutes,
                    unit="minute",
                    idempotency_key=f"billing-usage:{uuid4().hex}",
                )
            )
            db.commit()
        finally:
            db.close()

    def test_checkout_completion_activates_paid_plan_for_workspace(self):
        response = self.client.post("/api/billing/checkout?plan=starter")
        self.assertEqual(response.status_code, 200)
        checkout_id = response.json()["checkout_id"]
        self.assertEqual(response.json()["provider"], "mock")

        complete_response = self.client.post(f"/api/billing/checkout/{checkout_id}/complete")
        self.assertEqual(complete_response.status_code, 200)
        self.assertEqual(complete_response.json()["plan"], "starter")
        self.assertEqual(complete_response.json()["status"], "active")

        db = self.TestingSessionLocal()
        try:
            quota = get_workspace_quota_status(db, self.workspace_id)
            self.assertEqual(quota.limit_video_minutes, 600)
        finally:
            db.close()

    def test_free_checkout_requires_card_but_activates_without_charging(self):
        response = self.client.post("/api/billing/checkout?plan=free")

        self.assertEqual(response.status_code, 200)
        checkout_id = response.json()["checkout_id"]
        self.assertEqual(response.json()["plan"], "free")

        complete_response = self.client.post(f"/api/billing/checkout/{checkout_id}/complete")
        self.assertEqual(complete_response.status_code, 200)
        self.assertEqual(complete_response.json()["plan"], "free")
        self.assertEqual(complete_response.json()["status"], "active")
        self.assertIsNone(complete_response.json()["current_period_end"])

        db = self.TestingSessionLocal()
        try:
            quota = ensure_workspace_can_start_job(db, self.workspace_id)
            self.assertEqual(quota.limit_video_minutes, 60)
        finally:
            db.close()

    def test_billing_status_includes_quota_cycle_window(self):
        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="stripe",
                    provider_checkout_id="cs_status_cycle",
                    provider_customer_id="cus_status_cycle",
                    provider_subscription_id="sub_status_cycle",
                    plan_slug="starter",
                    status="active",
                    current_period_end=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            db.add(
                UsageEvent(
                    workspace_id=self.workspace_id,
                    event_type="video_processed",
                    quantity=42,
                    unit="minute",
                    idempotency_key=f"billing-status:{uuid4().hex}",
                    created_at=datetime(2029, 12, 10, tzinfo=UTC),
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get("/api/billing/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["quota"]["plan"], "starter")
        self.assertEqual(payload["quota"]["used_video_minutes"], 42.0)
        self.assertEqual(payload["quota"]["period_end"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(payload["subscription"]["status_label"], "Ativa")

    def test_billing_page_shows_subscription_cycle_end(self):
        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="stripe",
                    provider_checkout_id="cs_page_cycle",
                    provider_customer_id="cus_page_cycle",
                    provider_subscription_id="sub_page_cycle",
                    plan_slug="starter",
                    status="active",
                    current_period_end=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            db.add(
                UsageEvent(
                    workspace_id=self.workspace_id,
                    event_type="video_processed",
                    quantity=15,
                    unit="minute",
                    idempotency_key=f"billing-page:{uuid4().hex}",
                    created_at=datetime(2029, 12, 15, tzinfo=UTC),
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get("/billing")

        self.assertEqual(response.status_code, 200)
        self.assertIn("O ciclo atual vai ate 2030-01-01.", response.text)
        self.assertIn("15.0/600", response.text)

    def test_billing_page_shows_calendar_month_for_free_plan(self):
        response = self.client.get("/billing")

        self.assertEqual(response.status_code, 200)
        self.assertIn("1 video de ate 30 minutos sem cartao", response.text)
        self.assertIn("Sem assinatura ativa", response.text)
        self.assertIn("Ativar Free com cartao", response.text)
        self.assertIn("teste gratis", response.text)

    def test_billing_page_uses_human_status_label_for_checkout_pending(self):
        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="mock",
                    provider_checkout_id="cs_pending_label",
                    plan_slug="starter",
                    status="checkout_pending",
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get("/billing")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Checkout pendente", response.text)
        self.assertNotIn(">checkout_pending<", response.text)

    def test_webhook_can_activate_subscription(self):
        db = self.TestingSessionLocal()
        try:
            session = create_checkout_session(db, workspace_id=self.workspace_id, plan_slug="starter")
        finally:
            db.close()

        response = self.client.post(
            "/api/billing/webhook",
            json={
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": session.checkout_id,
                        "plan": "starter",
                        "customer": "cus_test",
                        "subscription": "sub_test",
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["subscription"]
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["provider_customer_id"], "cus_test")

    def test_mock_adapter_parses_nested_webhook_payload(self):
        adapter = MockBillingAdapter()

        result = adapter.parse_webhook(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "mock_cs_nested",
                        "workspace_id": str(self.workspace_id),
                        "plan_slug": "starter",
                        "customer": "cus_nested",
                        "subscription": "sub_nested",
                    }
                },
            }
        )

        self.assertEqual(result.event_type, "checkout.session.completed")
        self.assertEqual(result.checkout_id, "mock_cs_nested")
        self.assertEqual(result.workspace_id, self.workspace_id)
        self.assertEqual(result.provider_customer_id, "cus_nested")

    def test_payment_failure_falls_back_to_free_limits_and_blocks_processing(self):
        db = self.TestingSessionLocal()
        try:
            subscription = Subscription(
                workspace_id=self.workspace_id,
                provider="mock",
                provider_checkout_id="mock_cs_failed",
                plan_slug="starter",
                status="active",
            )
            db.add(subscription)
            db.commit()
        finally:
            db.close()
        self._record_usage(75)

        response = self.client.post(
            "/api/billing/webhook",
            json={
                "type": "invoice.payment_failed",
                "data": {"object": {"id": "mock_cs_failed"}},
            },
        )
        self.assertEqual(response.status_code, 200)

        db = self.TestingSessionLocal()
        try:
            quota = get_workspace_quota_status(db, self.workspace_id)
            self.assertEqual(quota.limit_video_minutes, 60)
            with self.assertRaises(Exception) as ctx:
                ensure_workspace_can_start_job(db, self.workspace_id)
            self.assertEqual(ctx.exception.status_code, 402)
        finally:
            db.close()

    def test_cancel_subscription_returns_workspace_to_free_plan(self):
        db = self.TestingSessionLocal()
        try:
            session = create_checkout_session(db, workspace_id=self.workspace_id, plan_slug="starter")
        finally:
            db.close()
        complete_response = self.client.post(f"/api/billing/checkout/{session.checkout_id}/complete")
        self.assertEqual(complete_response.status_code, 200)

        cancel_response = self.client.post("/api/billing/cancel")

        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_response.json()["status"], "canceled")

        db = self.TestingSessionLocal()
        try:
            quota = get_workspace_quota_status(db, self.workspace_id)
            self.assertEqual(quota.limit_video_minutes, 60)
        finally:
            db.close()

    def test_cancel_subscription_rejects_workspace_without_subscription(self):
        response = self.client.post("/api/billing/cancel")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Workspace nao possui assinatura para cancelar")

    def test_api_cancel_subscription_rate_limits_repeated_attempts(self):
        for _ in range(10):
            response = self.client.post("/api/billing/cancel")
            self.assertIn(response.status_code, {200, 400})

        blocked = self.client.post("/api/billing/cancel")

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(
            blocked.json()["detail"],
            "Muitas alteracoes de billing em pouco tempo. Tente novamente em instantes.",
        )

    def test_mercado_pago_billing_provider_fails_fast_until_adapter_exists(self):
        original_provider = billing_service.settings.billing_provider
        billing_service.settings.billing_provider = "mercado_pago"
        db = self.TestingSessionLocal()
        try:
            with self.assertRaises(ValueError) as ctx:
                create_checkout_session(db, workspace_id=self.workspace_id, plan_slug="starter")
            self.assertIn("ainda nao implementado", str(ctx.exception))
        finally:
            billing_service.settings.billing_provider = original_provider
            db.close()

    def test_stripe_adapter_creates_checkout_with_plan_price_and_metadata(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "id": "cs_test_123",
                    "url": "https://checkout.stripe.test/cs_test_123",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                }

        class FakeClient:
            def __init__(self):
                self.calls = []

            def post(self, url, data, headers, timeout):
                self.calls.append((url, data, headers, timeout))
                return FakeResponse()

        original_secret = billing_service.settings.stripe_secret_key
        original_price = billing_service.settings.stripe_price_starter
        billing_service.settings.stripe_secret_key = "sk_test_123"
        billing_service.settings.stripe_price_starter = "price_starter_123"
        fake_client = FakeClient()
        try:
            adapter = StripeBillingAdapter(client=fake_client)
            checkout = adapter.create_checkout(
                workspace_id=self.workspace_id,
                plan_slug="starter",
                success_url="https://app.test/billing/success",
                cancel_url="https://app.test/billing/cancel",
            )
        finally:
            billing_service.settings.stripe_secret_key = original_secret
            billing_service.settings.stripe_price_starter = original_price

        self.assertEqual(checkout.checkout_id, "cs_test_123")
        self.assertEqual(checkout.checkout_url, "https://checkout.stripe.test/cs_test_123")
        _url, data, headers, _timeout = fake_client.calls[0]
        self.assertEqual(data["line_items[0][price]"], "price_starter_123")
        self.assertEqual(data["metadata[workspace_id]"], str(self.workspace_id))
        self.assertEqual(data["metadata[plan_slug]"], "starter")
        self.assertEqual(data["payment_method_collection"], "always")
        self.assertEqual(headers["Authorization"], "Bearer sk_test_123")

    def test_stripe_adapter_uses_setup_mode_for_free_plan_card_collection(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "id": "cs_test_free",
                    "url": "https://checkout.stripe.test/cs_test_free",
                    "customer": "cus_free",
                }

        class FakeClient:
            def __init__(self):
                self.calls = []

            def post(self, url, data, headers, timeout):
                self.calls.append((url, data, headers, timeout))
                return FakeResponse()

        original_secret = billing_service.settings.stripe_secret_key
        original_price = billing_service.settings.stripe_price_starter
        billing_service.settings.stripe_secret_key = "sk_test_free"
        billing_service.settings.stripe_price_starter = "price_starter_unused"
        fake_client = FakeClient()
        try:
            checkout = StripeBillingAdapter(client=fake_client).create_checkout(
                workspace_id=self.workspace_id,
                plan_slug="free",
                success_url="https://app.test/billing/success",
                cancel_url="https://app.test/billing/cancel",
            )
        finally:
            billing_service.settings.stripe_secret_key = original_secret
            billing_service.settings.stripe_price_starter = original_price

        self.assertEqual(checkout.plan_slug, "free")
        _url, data, headers, _timeout = fake_client.calls[0]
        self.assertEqual(data["mode"], "setup")
        self.assertEqual(data["payment_method_collection"], "always")
        self.assertNotIn("line_items[0][price]", data)
        self.assertEqual(headers["Authorization"], "Bearer sk_test_free")

    def test_stripe_adapter_cancels_provider_subscription(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self):
                self.calls = []

            def delete(self, url, headers, timeout):
                self.calls.append((url, headers, timeout))
                return FakeResponse()

        original_secret = billing_service.settings.stripe_secret_key
        billing_service.settings.stripe_secret_key = "sk_test_cancel"
        fake_client = FakeClient()
        try:
            StripeBillingAdapter(client=fake_client).cancel_subscription("sub_cancel_123")
        finally:
            billing_service.settings.stripe_secret_key = original_secret

        url, headers, _timeout = fake_client.calls[0]
        self.assertEqual(url, "https://api.stripe.com/v1/subscriptions/sub_cancel_123")
        self.assertEqual(headers["Authorization"], "Bearer sk_test_cancel")

    def test_cancel_subscription_uses_subscription_provider_adapter(self):
        class FakeAdapter:
            provider = "stripe"

            def __init__(self):
                self.canceled = []

            def create_checkout(self, **kwargs):
                raise AssertionError("not used")

            def parse_webhook(self, payload):
                raise AssertionError("not used")

            def verify_webhook_signature(self, raw_body, headers):
                raise AssertionError("not used")

            def cancel_subscription(self, provider_subscription_id):
                self.canceled.append(provider_subscription_id)

        fake_adapter = FakeAdapter()
        original_adapter = billing_service.PROVIDER_ADAPTERS["stripe"]
        original_provider = billing_service.settings.billing_provider
        billing_service.PROVIDER_ADAPTERS["stripe"] = fake_adapter
        billing_service.settings.billing_provider = "mock"
        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="stripe",
                    provider_checkout_id="cs_cancel",
                    provider_customer_id="cus_cancel",
                    provider_subscription_id="sub_cancel",
                    plan_slug="starter",
                    status="active",
                )
            )
            db.commit()

            subscription = billing_service.cancel_current_subscription(db, self.workspace_id)
        finally:
            billing_service.PROVIDER_ADAPTERS["stripe"] = original_adapter
            billing_service.settings.billing_provider = original_provider
            db.close()

        self.assertEqual(fake_adapter.canceled, ["sub_cancel"])
        self.assertEqual(subscription.status, "canceled")

    def test_stripe_adapter_parses_checkout_completed_webhook(self):
        result = StripeBillingAdapter().parse_webhook(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_test_completed",
                        "client_reference_id": str(self.workspace_id),
                        "customer": "cus_stripe",
                        "subscription": "sub_stripe",
                        "metadata": {"plan_slug": "starter"},
                        "current_period_end": 1893456000,
                    }
                },
            }
        )

        self.assertEqual(result.event_type, "checkout.session.completed")
        self.assertEqual(result.checkout_id, "cs_test_completed")
        self.assertEqual(result.workspace_id, self.workspace_id)
        self.assertEqual(result.plan_slug, "starter")
        self.assertEqual(result.provider_customer_id, "cus_stripe")
        self.assertEqual(result.current_period_end, datetime(2030, 1, 1, tzinfo=UTC))

    def test_webhook_uses_provider_current_period_end_when_available(self):
        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="stripe",
                    provider_checkout_id="cs_period",
                    plan_slug="starter",
                    status="checkout_pending",
                )
            )
            db.commit()
        finally:
            db.close()

        original_provider = billing_service.settings.billing_provider
        billing_service.settings.billing_provider = "stripe"
        db = self.TestingSessionLocal()
        try:
            subscription = billing_service.apply_billing_webhook(
                db,
                {
                    "type": "checkout.session.completed",
                    "data": {
                        "object": {
                            "id": "cs_period",
                            "client_reference_id": str(self.workspace_id),
                            "customer": "cus_period",
                            "subscription": "sub_period",
                            "metadata": {"plan_slug": "starter"},
                            "current_period_end": 1893456000,
                        }
                    },
                },
            )
        finally:
            billing_service.settings.billing_provider = original_provider
            db.close()

        self.assertEqual(subscription.current_period_end.replace(tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))

    def test_stripe_subscription_deleted_webhook_matches_existing_subscription_id(self):
        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="stripe",
                    provider_checkout_id="cs_original",
                    provider_customer_id="cus_existing",
                    provider_subscription_id="sub_existing",
                    plan_slug="starter",
                    status="active",
                )
            )
            db.commit()
        finally:
            db.close()

        original_provider = billing_service.settings.billing_provider
        billing_service.settings.billing_provider = "stripe"
        db = self.TestingSessionLocal()
        try:
            subscription = billing_service.apply_billing_webhook(
                db,
                {
                    "type": "customer.subscription.deleted",
                    "data": {
                        "object": {
                            "id": "sub_existing",
                            "customer": "cus_existing",
                            "metadata": {"plan_slug": "starter"},
                        }
                    },
                },
            )
        finally:
            billing_service.settings.billing_provider = original_provider
            db.close()

        self.assertEqual(subscription.status, "canceled")
        self.assertEqual(subscription.provider_checkout_id, "cs_original")

    def test_stripe_webhook_route_requires_valid_signature_when_provider_is_stripe(self):
        db = self.TestingSessionLocal()
        try:
            subscription = Subscription(
                workspace_id=self.workspace_id,
                provider="stripe",
                provider_checkout_id="cs_signed",
                plan_slug="starter",
                status="checkout_pending",
            )
            db.add(subscription)
            db.commit()
        finally:
            db.close()

        original_provider = billing_service.settings.billing_provider
        original_secret = billing_service.settings.stripe_webhook_secret
        billing_service.settings.billing_provider = "stripe"
        billing_service.settings.stripe_webhook_secret = "whsec_test_secret"
        payload = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_signed",
                    "client_reference_id": str(self.workspace_id),
                    "customer": "cus_signed",
                    "subscription": "sub_signed",
                    "metadata": {"plan_slug": "starter"},
                }
            },
        }
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = "1710000000"
        signature = hmac.new(
            billing_service.settings.stripe_webhook_secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
        try:
            rejected = self.client.post(
                "/api/billing/webhook",
                content=raw_body,
                headers={"Stripe-Signature": f"t={timestamp},v1=bad_signature"},
            )
            accepted = self.client.post(
                "/api/billing/webhook",
                content=raw_body,
                headers={"Stripe-Signature": f"t={timestamp},v1={signature}"},
            )
        finally:
            billing_service.settings.billing_provider = original_provider
            billing_service.settings.stripe_webhook_secret = original_secret

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["subscription"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
