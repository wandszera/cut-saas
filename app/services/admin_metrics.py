from sqlalchemy import func
from sqlalchemy.orm import Session
from datetime import datetime, UTC, timedelta

from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.models.workspace import Workspace
from app.models.user import User
from app.services.plans import get_plan
from app.services.usage import calculate_workspace_storage_usage


def calculate_admin_financial_metrics(db: Session) -> dict:
    """Calcula e retorna as métricas de faturamento e uso consolidadas da plataforma."""
    # 1. MRR (Monthly Recurring Revenue) e Assinaturas
    active_subscriptions = (
        db.query(Subscription)
        .filter(Subscription.status == "active")
        .all()
    )
    
    mrr_cents = 0
    plan_counts = {"free": 0, "starter": 0, "pro": 0}
    
    for sub in active_subscriptions:
        plan = get_plan(sub.plan_slug)
        mrr_cents += plan.monthly_price_cents
        plan_counts[plan.slug] = plan_counts.get(plan.slug, 0) + 1

    # Obter total de workspaces cadastrados
    total_workspaces = db.query(Workspace).count()
    plan_counts["free"] = max(0, total_workspaces - plan_counts["starter"] - plan_counts["pro"])

    # 2. Churn Estimado (últimos 30 dias)
    thirty_days_ago = datetime.now(UTC) - timedelta(days=30)
    canceled_recent = (
        db.query(Subscription)
        .filter(
            Subscription.status == "canceled",
            Subscription.updated_at >= thirty_days_ago
        )
        .count()
    )
    
    total_active_or_canceled = len(active_subscriptions) + canceled_recent
    churn_rate = 0.0
    if total_active_or_canceled > 0:
        churn_rate = round((canceled_recent / total_active_or_canceled) * 100, 2)

    # 3. Métricas de Uso de Vídeo Processado
    video_events = (
        db.query(UsageEvent)
        .filter(UsageEvent.event_type == "video_processed")
        .all()
    )
    total_minutes_processed = sum(float(event.quantity or 0) for event in video_events)
    
    # 4. Uso de LLM
    llm_events = (
        db.query(UsageEvent)
        .filter(UsageEvent.event_type == "llm_call")
        .count()
    )

    # 5. Custos Estimados de Infraestrutura (Estimativas Operacionais)
    # Ex: Custo estimado de Whisper em GPU = R$ 0.15 por minuto
    # Ex: Custo médio de LLM = R$ 0.05 por chamada
    cost_whisper_per_minute = 0.15
    cost_llm_per_call = 0.05
    
    estimated_whisper_cost = total_minutes_processed * cost_whisper_per_minute
    estimated_llm_cost = llm_events * cost_llm_per_call
    total_estimated_cost = estimated_whisper_cost + estimated_llm_cost

    cost_per_minute = 0.0
    if total_minutes_processed > 0:
        cost_per_minute = round(total_estimated_cost / total_minutes_processed, 4)

    # 6. Storage Consolidador
    workspaces = db.query(Workspace).all()
    total_storage_bytes = 0
    total_files_count = 0
    
    for ws in workspaces:
        try:
            storage_usage = calculate_workspace_storage_usage(db, ws.id)
            total_storage_bytes += storage_usage.total_bytes
            total_files_count += storage_usage.files_count
        except Exception:
            continue

    return {
        "mrr_brl": round(float(mrr_cents) / 100.0, 2),
        "active_starter_count": plan_counts["starter"],
        "active_pro_count": plan_counts["pro"],
        "active_free_count": plan_counts["free"],
        "total_workspaces": total_workspaces,
        "churn_rate_pct": churn_rate,
        "canceled_last_30_days": canceled_recent,
        "total_minutes_processed": round(total_minutes_processed, 1),
        "llm_calls_count": llm_events,
        "total_estimated_cost_brl": round(total_estimated_cost, 2),
        "cost_per_minute_brl": cost_per_minute,
        "total_storage_gb": round(total_storage_bytes / (1024 * 1024 * 1024), 2),
        "total_files_count": total_files_count,
    }


def list_workspaces_usage_reports(db: Session) -> list[dict]:
    """Retorna um relatório detalhado de uso por workspace para a tabela administrativa."""
    workspaces = db.query(Workspace).all()
    reports = []
    
    for ws in workspaces:
        # Obter a assinatura atual
        sub = (
            db.query(Subscription)
            .filter(Subscription.workspace_id == ws.id)
            .order_by(Subscription.updated_at.desc(), Subscription.id.desc())
            .first()
        )
        plan_name = "Free"
        status_label = "Inativo"
        if sub:
            plan_name = get_plan(sub.plan_slug).name
            from app.services.billing import subscription_status_label
            status_label = subscription_status_label(sub.status)
            
        # Minutos de vídeo
        video_events = (
            db.query(UsageEvent)
            .filter(UsageEvent.workspace_id == ws.id, UsageEvent.event_type == "video_processed")
            .all()
        )
        minutes_used = sum(float(event.quantity or 0) for event in video_events)
        
        # Renders
        renders = (
            db.query(UsageEvent)
            .filter(UsageEvent.workspace_id == ws.id, UsageEvent.event_type == "render")
            .count()
        )
        
        # Storage
        storage_bytes = 0
        try:
            storage_usage = calculate_workspace_storage_usage(db, ws.id)
            storage_bytes = storage_usage.total_bytes
        except Exception:
            pass

        reports.append({
            "workspace_id": ws.id,
            "workspace_name": ws.name,
            "plan_name": plan_name,
            "status_label": status_label,
            "minutes_used": round(minutes_used, 1),
            "renders_count": renders,
            "storage_mb": round(storage_bytes / (1024 * 1024), 1),
        })
        
    return reports
