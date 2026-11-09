import asyncio
from typing import Optional
from urllib.parse import unquote

from fastapi import WebSocket

class RealtimeHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def publish(self, event: dict) -> None:
        async with self._lock:
            connections = tuple(self._connections)
        if not connections:
            return
        results = await asyncio.gather(
            *(websocket.send_json(event) for websocket in connections),
            return_exceptions=True,
        )
        failed = [
            websocket for websocket, result in zip(connections, results)
            if isinstance(result, Exception)
        ]
        if failed:
            async with self._lock:
                self._connections.difference_update(failed)


realtime_hub = RealtimeHub()


def update_event_for_request(method: str, path: str) -> Optional[dict]:
    if not path.startswith("/api/v1/") or method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    resource = path.removeprefix("/api/v1/")
    segments = resource.split("/")
    root = segments[0]
    if root == "auth":
        if len(segments) > 1 and segments[1] == "users":
            topic = "users"
        elif len(segments) > 1 and segments[1] == "activity":
            topic = "activity"
        else:
            return None
    else:
        topic = {
            "databases": "databases",
            "backups": "backups",
            "restores": "restores",
            "servers": "servers",
            "storage": "storage",
            "notifications": "settings",
            "alerts": "settings",
            "actions": "dashboard",
        }.get(root, "dashboard")

    event = {"type": "update", "topic": topic}
    if topic == "users" and len(segments) > 2 and segments[2] not in {"", "password"}:
        event["target"] = unquote(segments[2])
    return event
