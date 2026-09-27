from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
import hashlib
from typing import Protocol
from uuid import uuid4
from urllib.parse import urlencode, urlsplit
import time

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.subscription import Subscription
from app.models.billing_event import BillingEvent
from app.models.workspace import Workspace
from app.services.plans import ACTIVE_SUBSCRIPTION_STATUSES, PLANS, get_plan, list_plans, select_current_subscription, subscription_has_access
from app.services.quota import get_workspace_quota_status
from app.services.billing_emails import send_billing_activation_email, send_billing_cancellation_email
from app.services.billing_mercado_pago import MercadoPagoBillingAdapter


CHECKOUT_PENDING_STATUS = "checkout_pending"
ACTIVE_STATUS = "active"
PAST_DUE_STATUS = "past_due"
CANCELED_STATUS = "canceled"
SUBSCRIPTION_STATUS_LABELS = {
    "inactive": "Sem assinatura ativa",
    CHECKOUT_PENDING_STATUS: "Checkout pendente",
    ACTIVE_STATUS: "Ativa",
    "trialing": "Em periodo de teste",
    PAST_DUE_STATUS: "Pagamento pendente",
    CANCELED_STATUS: "Cancelada",
}


@dataclass(frozen=True)
class CheckoutSession:
    checkout_id: str
    checkout_url: str
    plan_slug: str
    provider: str


@dataclass(frozen=True)
class ProviderCheckout:
    checkout_id: str
    checkout_url: str
    plan_slug: str
    provider_customer_id: str | None = None
    provider_subscription_id: str | None = None


@dataclass(frozen=True)
class ProviderWebhookResult:
    event_type: str
    checkout_id: str | None
    workspace_id: int | None
    plan_slug: str | None
    provider_customer_id: str | None = None
    provider_subscription_id: str | None = None
    current_period_end: datetime | None = None
    current_period_start: datetime | None = None
    status: str | None = None


class BillingProviderAdapter(Protocol):
    provider: str

    def create_checkout(
        self,
        *,
        workspace_id: int,
        plan_slug: str,
        success_url: str,
        cancel_url: str,
    ) -> ProviderCheckout:
        ...

    def parse_webhook(self, payload: dict) -> ProviderWebhookResult:
        ...

    def verify_webhook_signature(self, raw_body: bytes, headers: dict[str, str]) -> None:
        ...

    def cancel_subscription(self, provider_subscription_id: str) -> None:
        ...


class MockBillingAdapter:
    provider = "mock"

    def create_checkout(
        self,
        *,
        workspace_id: int,
        plan_slug: str,
        success_url: str,
        cancel_url: str,
    ) -> ProviderCheckout:
        checkout_id = f"mock_cs_{uuid4().hex}"
        return ProviderCheckout(
            checkout_id=checkout_id,
            checkout_url=(
                "/billing/checkout/complete?" + urlencode({"session_id": checkout_id})
            ),
            plan_slug=plan_slug,
        )

    def parse_webhook(self, payload: dict) -> ProviderWebhookResult:
        event_type = str(payload.get("type") or "").strip()
        data = payload.get("data") or {}
        if isinstance(data, dict) and "object" in data and isinstance(data["object"], dict):
            data = data["object"]
        if not isinstance(data, dict):
            raise ValueError("Payload de webhook invalido")

        checkout_id = data.get("checkout_id") or data.get("id") or data.get("provider_checkout_id")
        workspace_id = data.get("workspace_id")
        return ProviderWebhookResult(
            event_type=event_type,
            checkout_id=str(checkout_id) if checkout_id else None,
            workspace_id=int(workspace_id) if workspace_id is not None else None,
            plan_slug=data.get("plan") or data.get("plan_slug") or "starter",
            provider_customer_id=data.get("customer"),
            provider_subscription_id=data.get("subscription"),
            current_period_end=_datetime_from_timestamp(data.get("current_period_end")),
        )

    def verify_webhook_signature(self, raw_body: bytes, headers: dict[str, str]) -> None:
        if settings.is_deployed_environment:
            raise ValueError("Webhooks simulados estao desativados neste ambiente")
        return None

    def cancel_subscription(self, provider_subscription_id: str) -> None:
        return None


class StripeBillingAdapter:
    provider = "stripe"
    checkout_endpoint = "https://api.stripe.com/v1/checkout/sessions"
    subscriptions_endpoint = "https://api.stripe.com/v1/subscriptions"

    def __init__(self, *, client: httpx.Client | None = None):
        self._client = client

    def retrieve_subscription(self, subscription_id: str) -> dict:
        if not subscription_id.startswith("sub_") or not subscription_id.replace("_", "").isalnum():
            raise ValueError("Identificador de assinatura Stripe invalido")
        url = f"{self.subscriptions_endpoint}/{subscription_id}"
        headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
        if self._client is not None:
            response = self._client.get(url, headers=headers, timeout=20.0)
        else:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def create_portal(self, customer_id: str) -> str:
        endpoint = "https://api.stripe.com/v1/billing_portal/sessions"
        data = {"customer": customer_id, "return_url": billing_return_url("/billing")}
        headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
        if self._client is not None:
            response = self._client.post(endpoint, data=data, headers=headers, timeout=20.0)
        else:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(endpoint, data=data, headers=headers)
        response.raise_for_status()
        url = response.json().get("url")
        if not url or urlsplit(url).scheme != "https":
            raise ValueError("Stripe nao retornou um portal valido")
        return url

    def _price_id_for_plan(self, plan_slug: str) -> str:
        price_ids = {
            "starter": settings.stripe_price_starter,
            "pro": settings.stripe_price_pro,
        }
        price_id = price_ids.get(plan_slug)
        if not price_id:
            raise ValueError(f"Plano '{plan_slug}' nao possui Stripe Price ID configurado")
        return price_id

    def _post_checkout(self, data: dict[str, str]) -> dict:
        headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
        if self._client is not None:
            response = self._client.post(self.checkout_endpoint, data=data, headers=headers, timeout=20.0)
            response.raise_for_status()
            return response.json()
        with httpx.Client(timeout=20.0) as client:
            response = client.post(self.checkout_endpoint, data=data, headers=headers)
            response.raise_for_status()
            return response.json()

    def _delete_subscription(self, provider_subscription_id: str) -> None:
        headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
        url = f"{self.subscriptions_endpoint}/{provider_subscription_id}"
        if self._client is not None:
            response = self._client.delete(url, headers=headers, timeout=20.0)
            response.raise_for_status()
            return None
        with httpx.Client(timeout=20.0) as client:
            response = client.delete(url, headers=headers)
            response.raise_for_status()
            return None

    def create_checkout(
        self,
        *,
        workspace_id: int,
        plan_slug: str,
        success_url: str,
        cancel_url: str,
    ) -> ProviderCheckout:
        data = {
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(workspace_id),
            "metadata[workspace_id]": str(workspace_id),
            "metadata[plan_slug]": plan_slug,
            "payment_method_collection": "always",
        }
        if plan_slug == "free":
            data["mode"] = "setup"
        else:
            price_id = self._price_id_for_plan(plan_slug)
            data["mode"] = "subscription"
            data["line_items[0][price]"] = price_id
            data["line_items[0][quantity]"] = "1"
            data["subscription_data[metadata][workspace_id]"] = str(workspace_id)
            data["subscription_data[metadata][plan_slug]"] = plan_slug
            if settings.trial_days > 0:
                data["subscription_data[trial_period_days]"] = str(settings.trial_days)
        payload = self._post_checkout(data)
        checkout_id = payload.get("id")
        checkout_url = payload.get("url")
        if not checkout_id or not checkout_url:
            raise ValueError("Stripe nao retornou checkout_id ou checkout_url")
        return ProviderCheckout(
            checkout_id=str(checkout_id),
            checkout_url=str(checkout_url),
            plan_slug=plan_slug,
            provider_customer_id=payload.get("customer"),
            provider_subscription_id=payload.get("subscription"),
        )

    def parse_webhook(self, payload: dict) -> ProviderWebhookResult:
        event_type = str(payload.get("type") or "").strip()
        data = payload.get("data") or {}
        obj = data.get("object") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            raise ValueError("Payload de webhook Stripe invalido")

        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        checkout_id = obj.get("id") if event_type.startswith("checkout.session.") else None
        workspace_id = (
            obj.get("client_reference_id")
            or metadata.get("workspace_id")
            or obj.get("workspace_id")
        )
        plan_slug = metadata.get("plan_slug") or obj.get("plan_slug")
        subscription_id = obj.get("subscription")
        if event_type.startswith("customer.subscription."):
            subscription_id = obj.get("id")
        if event_type.startswith("invoice."):
            subscription_id = subscription_id or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        customer_id = obj.get("customer")
        items = (obj.get("items") or {}).get("data") or []
        item = items[0] if items else {}
        price_id = (item.get("price") or {}).get("id")
        if price_id:
            plan_slug = {settings.stripe_price_starter: "starter", settings.stripe_price_pro: "pro"}.get(price_id)
            if plan_slug is None:
                raise ValueError("Stripe Price ID nao pertence aos planos configurados")
        current_period_end = _datetime_from_timestamp(obj.get("current_period_end") or item.get("current_period_end"))

        return ProviderWebhookResult(
            event_type=event_type,
            checkout_id=str(checkout_id) if checkout_id else None,
            workspace_id=int(workspace_id) if workspace_id is not None else None,
            plan_slug=str(plan_slug) if plan_slug else None,
            provider_customer_id=str(customer_id) if customer_id else None,
            provider_subscription_id=str(subscription_id) if subscription_id else None,
            current_period_end=current_period_end,
            current_period_start=_datetime_from_timestamp(obj.get("current_period_start") or item.get("current_period_start")),
            status=obj.get("status") if event_type.startswith("customer.subscription.") else None,
        )

    def verify_webhook_signature(self, raw_body: bytes, headers: dict[str, str]) -> None:
        if not settings.stripe_webhook_secret:
            raise ValueError("STRIPE_WEBHOOK_SECRET e obrigatorio para validar webhooks Stripe")
        signature_header = headers.get("stripe-signature") or headers.get("Stripe-Signature")
        if not signature_header:
            raise ValueError("Webhook Stripe sem assinatura")

        parts: dict[str, list[str]] = {}
        for item in signature_header.split(","):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            parts.setdefault(key.strip(), []).append(value.strip())

        timestamp = parts.get("t", [None])[0]
        signatures = parts.get("v1", [])
        if not timestamp or not signatures:
            raise ValueError("Assinatura Stripe invalida")
        try:
            if abs(time.time() - int(timestamp)) > 300:
                raise ValueError("Assinatura Stripe expirada")
        except (TypeError, ValueError) as exc:
            raise ValueError("Timestamp da assinatura Stripe invalido ou expirado") from exc

        signed_payload = timestamp.encode("utf-8") + b"." + raw_body
        expected = hmac.new(
            settings.stripe_webhook_secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not any(hmac.compare_digest(expected, signature) for signature in signatures):
            raise ValueError("Assinatura Stripe invalida")

    def cancel_subscription(self, provider_subscription_id: str) -> None:
        if not provider_subscription_id:
            raise ValueError("Assinatura Stripe sem provider_subscription_id")
        self._delete_subscription(provider_subscription_id)


PROVIDER_ADAPTERS: dict[str, BillingProviderAdapter] = {
    "mock": MockBillingAdapter(),
    "stripe": StripeBillingAdapter(),
    "mercado_pago": MercadoPagoBillingAdapter(),
}
SUPPORTED_BILLING_PROVIDERS = set(PROVIDER_ADAPTERS)


def get_configured_billing_provider() -> str:
    return settings.billing_provider


def get_billing_adapter() -> BillingProviderAdapter:
    provider = get_configured_billing_provider()
    return get_billing_adapter_for_provider(provider)


def get_billing_adapter_for_provider(provider: str) -> BillingProviderAdapter:
    if settings.environment == "production" and provider != "stripe":
        raise ValueError("Somente Stripe esta habilitado para cobranca em producao")
    adapter = PROVIDER_ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(
            f"Billing provider '{provider}' ainda nao implementado. "
            "Use BILLING_PROVIDER=mock ou implemente o adapter real antes de ativar."
        )
    return adapter


def verify_billing_webhook_signature(raw_body: bytes, headers: dict[str, str]) -> None:
    get_billing_adapter().verify_webhook_signature(raw_body, headers)


def _datetime_from_timestamp(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OSError):
        raise ValueError("current_period_end invalido")


def serialize_subscription(subscription: Subscription | None) -> dict:
    if not subscription:
        return {
            "plan": "free",
            "status": "inactive",
            "status_label": SUBSCRIPTION_STATUS_LABELS["inactive"],
            "provider": None,
            "current_period_end": None,
        }
    return {
        "id": subscription.id,
        "workspace_id": subscription.workspace_id,
        "plan": subscription.plan_slug,
        "status": subscription.status,
        "status_label": subscription_status_label(subscription.status),
        "provider": subscription.provider,
        "provider_customer_id": subscription.provider_customer_id,
        "provider_subscription_id": subscription.provider_subscription_id,
        "provider_checkout_id": subscription.provider_checkout_id,
        "checkout_url": subscription.checkout_url if subscription.status == CHECKOUT_PENDING_STATUS else None,
        "current_period_end": subscription.current_period_end.isoformat() if subscription.current_period_end else None,
    }


def get_current_subscription(db: Session, workspace_id: int) -> Subscription | None:
    return select_current_subscription(db, workspace_id)


def billing_return_url(path: str) -> str:
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("Destino de retorno invalido")
    return settings.public_base_url.rstrip("/") + path


def create_customer_portal(db: Session, workspace_id: int) -> str:
    subscription = get_current_subscription(db, workspace_id)
    if not subscription or subscription.provider != "stripe" or not subscription.provider_customer_id:
        raise ValueError("Nenhuma conta Stripe disponivel para gerenciar")
    return get_billing_adapter_for_provider("stripe").create_portal(subscription.provider_customer_id)


def subscription_status_label(status: str | None) -> str:
    normalized = (status or "inactive").strip().lower()
    return SUBSCRIPTION_STATUS_LABELS.get(normalized, normalized.replace("_", " ").capitalize())


def create_checkout_session(
    db: Session,
    *,
    workspace_id: int,
    plan_slug: str,
    success_url: str = "/billing?checkout=success",
    cancel_url: str = "/billing?checkout=canceled",
) -> CheckoutSession:
    if plan_slug not in PLANS:
        raise ValueError("Plano invalido")
    plan = PLANS[plan_slug]
    # Serialize checkout creation per workspace on PostgreSQL.
    db.query(Workspace).filter(Workspace.id == workspace_id).with_for_update().one()
    current = get_current_subscription(db, workspace_id)
    if current and current.status == CHECKOUT_PENDING_STATUS and current.checkout_url and current.plan_slug == plan_slug:
        return CheckoutSession(current.provider_checkout_id, current.checkout_url, current.plan_slug, current.provider)
    if current and current.plan_slug != "free" and current.status in {"active", "trialing", "past_due", "unpaid", "incomplete", CHECKOUT_PENDING_STATUS}:
        raise ValueError("Ja existe uma assinatura ou checkout pendente. Gerencie o plano existente antes de iniciar outro.")

    adapter = get_billing_adapter()
    provider_checkout = adapter.create_checkout(
        workspace_id=workspace_id,
        plan_slug=plan.slug,
        success_url=billing_return_url(success_url),
        cancel_url=billing_return_url(cancel_url),
    )
    subscription = Subscription(
        workspace_id=workspace_id,
        provider=adapter.provider,
        provider_checkout_id=provider_checkout.checkout_id,
        checkout_url=provider_checkout.checkout_url,
        provider_customer_id=provider_checkout.provider_customer_id,
        provider_subscription_id=provider_checkout.provider_subscription_id,
        plan_slug=provider_checkout.plan_slug,
        status=CHECKOUT_PENDING_STATUS,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    return CheckoutSession(
        checkout_id=provider_checkout.checkout_id,
        checkout_url=provider_checkout.checkout_url,
        plan_slug=plan.slug,
        provider=adapter.provider,
    )


def activate_checkout_session(db: Session, checkout_id: str, *, workspace_id: int) -> Subscription:
    if settings.billing_provider != "mock" or settings.is_deployed_environment:
        raise ValueError("Ativacao simulada indisponivel; aguarde a confirmacao do pagamento")
    subscription = db.query(Subscription).filter(Subscription.provider_checkout_id == checkout_id, Subscription.workspace_id == workspace_id, Subscription.provider == "mock").first()
    if not subscription:
        raise ValueError("Sessao de checkout nao encontrada")
    if subscription.status in ACTIVE_SUBSCRIPTION_STATUSES:
        return subscription
    if subscription.status != CHECKOUT_PENDING_STATUS:
        raise ValueError("Este checkout nao pode mais ser ativado")

    if subscription.plan_slug == "free":
        subscription.status = ACTIVE_STATUS
        subscription.provider_customer_id = subscription.provider_customer_id or f"mock_cus_{subscription.workspace_id}"
        subscription.current_period_end = None
    else:
        subscription.provider_customer_id = subscription.provider_customer_id or f"mock_cus_{subscription.workspace_id}"
        subscription.provider_subscription_id = subscription.provider_subscription_id or f"mock_sub_{uuid4().hex}"
        if settings.trial_days > 0:
            subscription.status = "trialing"
            subscription.current_period_end = datetime.now(UTC) + timedelta(days=settings.trial_days)
        else:
            subscription.status = ACTIVE_STATUS
            subscription.current_period_end = datetime.now(UTC) + timedelta(days=30)
    db.commit()
    db.refresh(subscription)
    try:
        send_billing_activation_email(db, subscription.workspace_id, get_plan(subscription.plan_slug).name)
    except Exception as exc:
        import logging
        logging.getLogger("app.billing").exception("Falha ao enviar email de ativacao: %s", exc)
    return subscription


def cancel_current_subscription(db: Session, workspace_id: int) -> Subscription:
    subscription = get_current_subscription(db, workspace_id)
    if not subscription:
        raise ValueError("Workspace nao possui assinatura para cancelar")
    if subscription.status == CANCELED_STATUS:
        return subscription

    adapter = get_billing_adapter_for_provider(subscription.provider)
    if subscription.provider_subscription_id:
        adapter.cancel_subscription(subscription.provider_subscription_id)

    subscription.status = CANCELED_STATUS
    subscription.current_period_end = datetime.now(UTC)
    db.commit()
    db.refresh(subscription)
    try:
        send_billing_cancellation_email(db, subscription.workspace_id)
    except Exception as exc:
        import logging
        logging.getLogger("app.billing").exception("Falha ao enviar email de cancelamento: %s", exc)
    return subscription


SUPPORTED_WEBHOOK_EVENTS = {
    "checkout.session.completed", "checkout.session.expired",
    "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed",
    "customer.subscription.created", "customer.subscription.updated",
    "customer.subscription.deleted", "customer.subscription.canceled",
    "customer.subscription.past_due", "invoice.paid", "invoice.payment_failed",
}


def apply_billing_webhook(db: Session, payload: dict) -> Subscription | None:
    if not isinstance(payload, dict):
        raise ValueError("Payload de webhook invalido")
    adapter = get_billing_adapter()
    event_type = str(payload.get("type") or "")
    if event_type not in SUPPORTED_WEBHOOK_EVENTS:
        return None
    event_id = str(payload.get("id") or "")
    if adapter.provider == "stripe" and not event_id:
        raise ValueError("Webhook Stripe sem identificador de evento")
    if event_id:
        receipt = db.query(BillingEvent).filter_by(provider=adapter.provider, event_id=event_id).first()
        if receipt:
            return db.get(Subscription, receipt.subscription_id) if receipt.subscription_id else None
    webhook = adapter.parse_webhook(payload)
    subscription = None
    query = db.query(Subscription).filter(Subscription.provider == adapter.provider)
    if webhook.checkout_id:
        subscription = query.filter(Subscription.provider_checkout_id == webhook.checkout_id).first()
    if subscription is None and webhook.provider_subscription_id:
        subscription = query.filter(Subscription.provider_subscription_id == webhook.provider_subscription_id).first()
    if subscription is None and webhook.workspace_id is not None:
        # Only bind to an existing checkout, never invent a paid account from an event.
        subscription = query.filter(
            Subscription.workspace_id == webhook.workspace_id,
            Subscription.status == CHECKOUT_PENDING_STATUS,
            Subscription.provider_subscription_id.is_(None),
        ).order_by(Subscription.id.desc()).first()
    if subscription is None:
        raise ValueError("Webhook sem checkout ou assinatura conhecida")
    if webhook.workspace_id is not None and webhook.workspace_id != subscription.workspace_id:
        raise ValueError("Workspace do webhook nao corresponde ao checkout")
    db.query(Workspace).filter(Workspace.id == subscription.workspace_id).with_for_update().one()
    db.refresh(subscription)
    if event_id:
        receipt = BillingEvent(provider=adapter.provider, event_id=event_id, event_type=event_type, subscription_id=subscription.id)
        db.add(receipt)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            receipt = db.query(BillingEvent).filter_by(provider=adapter.provider, event_id=event_id).first()
            if receipt:
                return db.get(Subscription, receipt.subscription_id)
            raise
    previous_status = subscription.status
    if adapter.provider == "stripe" and webhook.provider_subscription_id:
        if subscription.provider_subscription_id and subscription.provider_subscription_id != webhook.provider_subscription_id:
            raise ValueError("Assinatura do webhook nao corresponde ao checkout")
        # Read the authoritative state under the workspace lock. Reordered events
        # (including events created in the same second) cannot restore stale access.
        live = adapter.retrieve_subscription(webhook.provider_subscription_id)
        if live.get("id") != webhook.provider_subscription_id:
            raise ValueError("Resposta Stripe inconsistente")
        webhook = adapter.parse_webhook({"type": "customer.subscription.updated", "data": {"object": live}})
        if webhook.workspace_id is not None and webhook.workspace_id != subscription.workspace_id:
            raise ValueError("Workspace da assinatura Stripe inconsistente")
        subscription.status = webhook.status or "inactive"
        subscription.current_period_start = webhook.current_period_start
        subscription.current_period_end = webhook.current_period_end
    elif event_type == "checkout.session.expired":
        if subscription.status == CHECKOUT_PENDING_STATUS:
            subscription.status = "expired"
    elif adapter.provider == "stripe":
        obj = payload.get("data", {}).get("object", {})
        if subscription.plan_slug == "free" and obj.get("mode") == "setup" and obj.get("status") == "complete":
            subscription.status = ACTIVE_STATUS
        # Paid checkout redirects/events alone never grant access.
    elif event_type in {"checkout.session.completed", "customer.subscription.created", "customer.subscription.updated", "invoice.paid"}:
        subscription.status = webhook.status or ACTIVE_STATUS
        subscription.current_period_end = webhook.current_period_end or datetime.now(UTC) + timedelta(days=30)
        subscription.current_period_start = webhook.current_period_start
    elif event_type in {"invoice.payment_failed", "customer.subscription.past_due"}:
        subscription.status = PAST_DUE_STATUS
    elif event_type in {"customer.subscription.deleted", "customer.subscription.canceled"}:
        subscription.status = CANCELED_STATUS
    if webhook.plan_slug:
        if webhook.plan_slug not in PLANS:
            raise ValueError("Plano do webhook desconhecido")
        subscription.plan_slug = webhook.plan_slug
    subscription.provider_customer_id = webhook.provider_customer_id or subscription.provider_customer_id
    subscription.provider_subscription_id = webhook.provider_subscription_id or subscription.provider_subscription_id
    db.commit()
    db.refresh(subscription)
    try:
        if subscription.status in ACTIVE_SUBSCRIPTION_STATUSES and previous_status not in ACTIVE_SUBSCRIPTION_STATUSES:
            send_billing_activation_email(db, subscription.workspace_id, get_plan(subscription.plan_slug).name)
        elif subscription.status == CANCELED_STATUS and previous_status != CANCELED_STATUS:
            send_billing_cancellation_email(db, subscription.workspace_id)
    except Exception:
        import logging
        logging.getLogger("app.billing").exception("Falha ao enviar notificacao de billing")
    return subscription


def build_billing_overview(db: Session, workspace_id: int) -> dict:
    subscription = get_current_subscription(db, workspace_id)
    quota_status = get_workspace_quota_status(db, workspace_id)
    return {
        "provider": get_configured_billing_provider(),
        "provider_ready": get_configured_billing_provider() in SUPPORTED_BILLING_PROVIDERS,
        "plans": [
            {
                "slug": plan.slug,
                "name": plan.name,
                "monthly_video_minutes": plan.monthly_video_minutes,
                "monthly_price_cents": plan.monthly_price_cents,
                "storage_limit_bytes": plan.storage_limit_bytes,
                "max_workspaces": plan.max_workspaces,
                "llm_enabled": plan.llm_enabled,
                "priority_queue": plan.priority_queue,
            }
            for plan in list_plans()
        ],
        "quota": quota_status.to_dict(),
        "subscription": serialize_subscription(subscription),
        "billing_activation_required": not workspace_has_billing_access(db, workspace_id),
    }


def workspace_has_billing_access(db: Session, workspace_id: int) -> bool:
    subscription = get_current_subscription(db, workspace_id)
    return subscription_has_access(subscription)
