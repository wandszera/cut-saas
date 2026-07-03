import logging
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models.workspace import Workspace
from app.models.user import User
from app.models.workspace_member import WorkspaceMember

logger = logging.getLogger("app.billing_emails")


def _get_workspace_recipients(db: Session, workspace_id: int) -> list[str]:
    """Retorna os emails do dono e membros administradores do workspace."""
    members = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role.in_(["owner", "admin", "editor"]),
        )
        .all()
    )
    user_ids = [m.user_id for m in members]
    if not user_ids:
        return []
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    return [user.email for user in users if user.email]


def send_billing_activation_email(db: Session, workspace_id: int, plan_name: str) -> None:
    """Envia email informando que a assinatura foi ativada com sucesso."""
    recipients = _get_workspace_recipients(db, workspace_id)
    if not recipients:
        logger.warning(f"Nenhum destinatário encontrado para o workspace {workspace_id}")
        return

    subject = f"[{settings.app_name}] Sua assinatura do plano {plan_name} está ativa! 🎉"
    body = (
        f"Olá!\n\n"
        f"Temos o prazer de informar que a assinatura do plano *{plan_name}* para o seu workspace "
        f"foi ativada com sucesso!\n\n"
        f"Sua nova cota mensal e os limites estendidos já estão liberados para uso. "
        f"Aproveite a plataforma para criar excelentes cortes!\n\n"
        f"Se tiver qualquer dúvida, basta responder a este email.\n\n"
        f"Atenciosamente,\n"
        f"Equipe {settings.app_name}"
    )

    # Simulação do envio ou integração real via HTTP/SMTP
    logger.info(
        f"ENVIANDO EMAIL TRANSACIONAL:\n"
        f"Para: {recipients}\n"
        f"Assunto: {subject}\n"
        f"Corpo:\n{body}\n"
        f"----------------------------------------"
    )


def send_billing_cancellation_email(db: Session, workspace_id: int) -> None:
    """Envia email informando sobre o cancelamento da assinatura."""
    recipients = _get_workspace_recipients(db, workspace_id)
    if not recipients:
        return

    subject = f"[{settings.app_name}] Assinatura cancelada"
    body = (
        f"Olá!\n\n"
        f"Confirmamos o cancelamento da sua assinatura recorrente. O seu workspace retornou "
        f"ao plano Free.\n\n"
        f"Você ainda poderá acessar, visualizar e baixar todos os vídeos e clipes gerados anteriormente, "
        f"mas novos processamentos serão limitados pelas cotas do plano Free.\n\n"
        f"Esperamos ver você de volta em breve!\n\n"
        f"Atenciosamente,\n"
        f"Equipe {settings.app_name}"
    )

    logger.info(
        f"ENVIANDO EMAIL TRANSACIONAL:\n"
        f"Para: {recipients}\n"
        f"Assunto: {subject}\n"
        f"Corpo:\n{body}\n"
        f"----------------------------------------"
    )


def send_quota_warning_email(db: Session, workspace_id: int, used_minutes: float, limit_minutes: float) -> None:
    """Envia alerta quando o workspace atinge 80% ou 100% do limite de vídeo do plano."""
    recipients = _get_workspace_recipients(db, workspace_id)
    if not recipients:
        return

    pct = int((used_minutes / limit_minutes * 100) if limit_minutes else 0)
    if pct >= 100:
        subject = f"🚨 [{settings.app_name}] Limite mensal de processamento atingido!"
        body = (
            f"Olá!\n\n"
            f"O seu workspace atingiu 100% do limite mensal de processamento de vídeos ({used_minutes:.1f}/{limit_minutes:.0f} minutos).\n\n"
            f"Para continuar processando novos vídeos e gerando clipes automáticos neste ciclo, "
            f"por favor faça um upgrade de plano na aba de Faturamento (Billing).\n\n"
            f"Seus clipes e dados atuais continuam totalmente salvos e disponíveis para download.\n\n"
            f"Atenciosamente,\n"
            f"Equipe {settings.app_name}"
        )
    else:
        subject = f"⚠️ [{settings.app_name}] Você consumiu {pct}% da sua cota mensal de vídeo"
        body = (
            f"Olá!\n\n"
            f"Gostaríamos de avisar que seu workspace consumiu {used_minutes:.1f} minutos de vídeo, "
            f"o que representa {pct}% do limite mensal de {limit_minutes:.0f} minutos do seu plano.\n\n"
            f"Caso precise de mais capacidade para evitar interrupções nos seus jobs, "
            f"considere realizar um upgrade de plano na página de Faturamento.\n\n"
            f"Atenciosamente,\n"
            f"Equipe {settings.app_name}"
        )

    logger.info(
        f"ENVIANDO EMAIL TRANSACIONAL:\n"
        f"Para: {recipients}\n"
        f"Assunto: {subject}\n"
        f"Corpo:\n{body}\n"
        f"----------------------------------------"
    )
