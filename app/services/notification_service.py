import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger("dbops.notify")

SETTINGS_FILE = Path(settings.BACKUP_STORAGE_DIR) / "notification_settings.json"

# Every event the control plane can announce. Defaults are chosen so the noisy
# channels (health) are opt-in while important operations are opt-out.
DEFAULT_EVENTS: Dict[str, bool] = {
    "backup_success": True,
    "backup_failure": True,
    "restore_started": True,
    "restore_success": True,
    "restore_failure": True,
    "migration_detected": True,
    "alert": True,
    "health": False,
    "test": True,
}

SEVERITY_ICON = {
    "INFO": "ℹ️",
    "SUCCESS": "✅",
    "WARNING": "⚠️",
    "CRITICAL": "🚨",
}


def _mask_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "•" * len(token)
    return f"{token[:4]}…{token[-4:]}"


class NotificationService:
    def __init__(self) -> None:
        self._ensure_storage()

    # ------------------------------------------------------------------ storage
    def _ensure_storage(self) -> None:
        settings.get_backup_path()
        if not SETTINGS_FILE.exists():
            self._save(self._defaults())

    def _defaults(self) -> Dict[str, Any]:
        token = settings.ALERT_TELEGRAM_BOT_TOKEN or ""
        chat_id = settings.ALERT_TELEGRAM_CHAT_ID or ""
        return {
            "enabled": bool(token and chat_id),
            "bot_token": token,
            "chat_id": chat_id,
            "telegram_recipients": [],
            "telegram_update_offset": None,
            "silent": False,
            "events": dict(DEFAULT_EVENTS),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load(self) -> Dict[str, Any]:
        self._ensure_storage()
        base = self._defaults()
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if data.get("bot_token_enc"):
                    from app.crypto_utils import decrypt_persisted_secret
                    data["bot_token"] = decrypt_persisted_secret(data.pop("bot_token_enc"))
                base.update({k: v for k, v in data.items() if k != "events"})
                events = dict(DEFAULT_EVENTS)
                events.update(data.get("events") or {})
                base["events"] = events
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("Could not read Telegram notification settings. Restore the settings file or its encryption key.") from e
        return base

    def _save(self, data: Dict[str, Any]) -> None:
        settings.get_backup_path()
        persisted = dict(data)
        token = persisted.pop("bot_token", "")
        if token:
            from app.crypto_utils import encrypt_persisted_secret
            persisted["bot_token_enc"] = encrypt_persisted_secret(token)
        else:
            persisted.pop("bot_token_enc", None)
        temp_file = SETTINGS_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(persisted, f, indent=2)
        temp_file.replace(SETTINGS_FILE)

    # ------------------------------------------------------------------- config
    def get_config(self) -> Dict[str, Any]:
        cfg = self._load()
        token = cfg.get("bot_token") or ""
        return {
            "enabled": bool(cfg.get("enabled")),
            "telegram_configured": bool(token and (cfg.get("chat_id") or cfg.get("telegram_recipients"))),
            "bot_token_masked": _mask_token(token),
            "chat_id": cfg.get("chat_id") or "",
            "telegram_recipients": [str(item) for item in cfg.get("telegram_recipients", [])],
            "silent": bool(cfg.get("silent")),
            "events": dict(cfg.get("events") or DEFAULT_EVENTS),
            "updated_at": cfg.get("updated_at"),
        }

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self._load()

        if payload.get("enabled") is not None:
            cfg["enabled"] = bool(payload["enabled"])
        if payload.get("silent") is not None:
            cfg["silent"] = bool(payload["silent"])
        if payload.get("chat_id") is not None:
            cfg["chat_id"] = str(payload["chat_id"]).strip()

        # Only treat the token as a change when a real value (not the masked echo) arrives.
        token = payload.get("bot_token")
        if token is not None:
            token = str(token).strip()
            if token and token != self.get_config()["bot_token_masked"] and "…" not in token:
                cfg["bot_token"] = token

        incoming_events = payload.get("events")
        if isinstance(incoming_events, dict):
            events = dict(cfg.get("events") or DEFAULT_EVENTS)
            for k, v in incoming_events.items():
                if k in DEFAULT_EVENTS:
                    events[k] = bool(v)
            cfg["events"] = events

        cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save(cfg)
        return self.get_config()

    # --------------------------------------------------------------- telegram IO
    async def send_message(self, text: str) -> Dict[str, Any]:
        cfg = self._load()
        token = cfg.get("bot_token")
        # Accept comma/newline/semicolon separated private chats, groups, or
        # channels. Telegram requires a separate sendMessage call per chat.
        chat_ids = list(dict.fromkeys(
            part.strip()
            for part in str(cfg.get("chat_id") or "").replace(";", ",").replace("\n", ",").split(",")
            if part.strip()
        ))
        chat_ids.extend(chat_id for chat_id in cfg.get("telegram_recipients", []) if str(chat_id) not in chat_ids)
        if not token or not chat_ids:
            return {"success": False, "status": "not_configured",
                    "message": "Telegram bot token and chat ID are not configured."}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        results = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                for chat_id in chat_ids:
                    body = {
                        "chat_id": chat_id,
                        "text": text,
                        "disable_notification": bool(cfg.get("silent")),
                        "disable_web_page_preview": True,
                    }
                    try:
                        resp = await client.post(url, json=body)
                        api_ok = resp.json().get("ok") if resp.headers.get("content-type", "").startswith("application/json") else True
                        ok = resp.status_code == 200 and api_ok
                        results.append({"chat_id": chat_id, "success": bool(ok), "status": f"status {resp.status_code}" if ok else resp.text[:300]})
                    except Exception as exc:
                        results.append({"chat_id": chat_id, "success": False, "status": str(exc)[:300]})
            delivered = sum(1 for result in results if result["success"])
            success = delivered == len(chat_ids)
            if success:
                return {"success": True, "status": f"sent to {delivered} chat(s)", "message": "Notification delivered to all configured Telegram chats.", "recipients": results}
            return {"success": False, "status": f"delivered to {delivered} of {len(chat_ids)} chat(s)",
                    "message": "One or more Telegram chats rejected the message. Confirm each user has started the bot and that each chat ID is correct.", "recipients": results}
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return {"success": False, "status": "failed", "message": str(e)}

    # ---------------------------------------------------------------- dispatcher
    async def notify(
        self,
        event: str,
        title: str,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
        severity: str = "INFO",
    ) -> Dict[str, Any]:
        cfg = self._load()
        if not cfg.get("enabled"):
            return {"success": False, "status": "disabled", "message": "Notifications are disabled."}
        if not (cfg.get("events") or {}).get(event, True):
            return {"success": False, "status": "muted", "message": f"Event '{event}' is muted."}

        if event == "migration_detected":
            details = details or {}
            rows = str(details.get("Migrations") or "").splitlines()
            lines = [
                "DATABASE MIGRATION DETECTED",
                "===========================",
                f"Database: {details.get('Target', 'Unknown')}",
                message,
                "",
                "Migration records:",
            ]
            lines.extend(f"  {row}" for row in rows[:20])
            if len(rows) > 20:
                lines.append(f"  ...and {len(rows) - 20} more")
            lines.extend(("", "DBOps Control Plane"))
            return await self.send_message("\n".join(lines))

        icon = SEVERITY_ICON.get(severity.upper(), "ℹ️")
        # Send plain text. Dynamic values can contain Markdown metacharacters
        # (such as underscores in migration names), which Telegram may reject.
        lines = [f"{icon} {title}"]
        if message:
            lines.append("")
            lines.append(message)
        if details:
            for k, v in list(details.items())[:12]:
                lines.append(f"• `{k}`: {v}")
        lines.append("")
        lines.append("DBOps Control Plane")
        return await self.send_message("\n".join(lines))

    async def send_test(self, message: str) -> Dict[str, Any]:
        cfg = self._load()
        if not cfg.get("enabled"):
            return {"success": False, "status": "disabled",
                    "details": {"reason": "Notifications are disabled. Enable them first."}}
        text = (
            "🧪 *DBOps Test Notification*\n\n"
            f"{message}\n\n"
            f"Time: {datetime.now(timezone.utc).isoformat()}\n\n"
            "_DBOps Control Plane_"
        )
        result = await self.send_message(text)
        return {"success": bool(result.get("success")), "status": result.get("status", "unknown"),
                "details": result}

    # ---------------------------------------------------------- recipient intake
    def record_telegram_update(self, update: Dict[str, Any]) -> None:
        """Persist chats which explicitly start the bot, plus the polling offset."""
        cfg = self._load()
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip().split(" ", 1)[0].split("@", 1)[0].lower()
        recipients = [str(item) for item in cfg.get("telegram_recipients", [])]
        if chat_id is not None and text == "/start":
            chat_id = str(chat_id)
            if chat_id not in recipients:
                recipients.append(chat_id)
                if cfg.get("bot_token"):
                    cfg["enabled"] = True
        elif chat_id is not None and text == "/stop":
            chat_id = str(chat_id)
            recipients = [item for item in recipients if item != chat_id]
        cfg["telegram_recipients"] = recipients
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            cfg["telegram_update_offset"] = update_id + 1
        self._save(cfg)


async def telegram_recipient_listener(stop_event) -> None:
    """Long-poll Telegram updates to learn recipients who send /start."""
    import asyncio

    offset = None
    while not stop_event.is_set():
        cfg = notification_service._load()
        token = cfg.get("bot_token")
        if not token:
            await asyncio.sleep(15)
            continue
        offset = cfg.get("telegram_update_offset", offset)
        base_url = f"https://api.telegram.org/bot{token}"
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                webhook_resp = await client.get(f"{base_url}/getWebhookInfo", timeout=10.0)
                webhook_data = webhook_resp.json()
                if webhook_data.get("ok") and (webhook_data.get("result") or {}).get("url"):
                    logger.warning("Telegram recipient discovery paused: this bot has a webhook configured; getUpdates polling cannot run alongside it.")
                    await asyncio.sleep(60)
                    continue
                params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    params["offset"] = offset
                response = await client.get(f"{base_url}/getUpdates", params=params)
                data = response.json()
                if not response.is_success or not data.get("ok"):
                    raise RuntimeError(data.get("description", f"Telegram returned HTTP {response.status_code}"))
                for update in data.get("result", []):
                    notification_service.record_telegram_update(update)
                    if isinstance(update.get("update_id"), int):
                        offset = update["update_id"] + 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Telegram recipient discovery failed; it will retry.", exc_info=True)
            await asyncio.sleep(15)


notification_service = NotificationService()
