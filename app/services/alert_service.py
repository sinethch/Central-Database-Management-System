import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, Any
import httpx
from app.config import settings

logger = logging.getLogger("dbops.alerts")


class AlertService:
    @staticmethod
    async def send_alert(title: str, message: str, severity: str = "WARNING", details: Optional[Dict[str, Any]] = None, send_telegram: bool = True) -> Dict[str, Any]:
        """
        Dispatches alerts across all configured channels (Webhook, Telegram, Email).

        Telegram delivery is delegated to NotificationService so the persisted
        settings and per-event toggles configured from the dashboard apply here too.
        """
        results = {
            "webhook": "not_configured",
            "telegram": "not_configured",
            "email": "not_configured"
        }

        # 1. Webhook (Generic / Slack / Discord compatible)
        if settings.ALERT_WEBHOOK_URL:
            try:
                payload = {
                    "app": settings.APP_NAME,
                    "title": f"[{severity}] {title}",
                    "text": message,
                    "severity": severity,
                    "details": details or {},
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(settings.ALERT_WEBHOOK_URL, json=payload)
                    resp.raise_for_status()
                    results["webhook"] = f"sent (status {resp.status_code})"
            except Exception as e:
                logger.error(f"Webhook alert failed: {e}")
                results["webhook"] = f"failed: {str(e)}"

        # 2. Telegram Bot (via persisted NotificationService settings)
        if send_telegram:
            try:
                from app.services.notification_service import notification_service
                tg = await notification_service.notify("alert", title, message, details, severity)
                results["telegram"] = tg.get("status", "unknown")
            except Exception as e:
                logger.error(f"Telegram alert failed: {e}")
                results["telegram"] = f"failed: {str(e)}"

        # 3. SMTP Email
        if settings.ALERT_EMAIL_ENABLED and settings.SMTP_HOST and settings.ALERT_EMAIL_TO:
            try:
                msg = MIMEMultipart()
                msg["From"] = settings.ALERT_EMAIL_FROM or f"noreply@{settings.SMTP_HOST}"
                msg["To"] = settings.ALERT_EMAIL_TO
                msg["Subject"] = f"[{severity}] {title} - {settings.APP_NAME}"

                body = f"{title}\n\nSeverity: {severity}\n\n{message}\n\nDetails: {details or {}}"
                msg.attach(MIMEText(body, "plain"))

                with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
                    if settings.SMTP_USER and settings.SMTP_PASSWORD:
                        server.starttls()
                        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    server.send_message(msg)
                results["email"] = "sent"
            except Exception as e:
                logger.error(f"Email alert failed: {e}")
                results["email"] = f"failed: {str(e)}"

        return results


alert_service = AlertService()

