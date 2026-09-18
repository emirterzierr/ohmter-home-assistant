"""HTTP client for ohmter.nl, with the transport injected.

No Home Assistant import and no HTTP library import: the caller hands in an
async `transport(method, url, json, headers) -> (status, body)`. In Home
Assistant that wraps the shared aiohttp session; in the server's own test
suite it wraps FastAPI's TestClient, which is how the client is exercised
against the real router on Python 3.10 while Home Assistant itself needs 3.13.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from .protocol import (
    ERR_BAD_CODE,
    ERR_BAD_TOKEN,
    ERR_BRIDGE_REVOKED,
    ERR_CODE_EXPIRED,
    PATHS,
    PROTOCOL_VERSION,
)

Transport = Callable[[str, str, Optional[Dict[str, Any]], Dict[str, str]], Awaitable["tuple[int, Any]"]]


class OhmterError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class OhmterUnreachable(OhmterError):
    """Network failure or a 5xx: try again next interval, do not change anything."""


class BridgeRevoked(OhmterError):
    """The household disconnected this bridge in the dashboard. Re-pair."""


class BadPairingCode(OhmterError):
    """Wrong or expired code typed in the config flow."""


def _detail(body: Any) -> "tuple[str, str]":
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict):
            return str(d.get("code") or ""), str(d.get("message") or "")
        if isinstance(d, str):
            return "", d
        if body.get("error"):
            return "", str(body["error"])
    return "", str(body)[:200]


class OhmterClient:
    def __init__(self, base_url: str, token: Optional[str], transport: Transport, *, user_agent: str = "OhmterHA"):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self._transport = transport
        self._user_agent = user_agent

    # -- plumbing -----------------------------------------------------------

    def _headers(self, auth: bool) -> Dict[str, str]:
        h = {"User-Agent": self._user_agent, "Accept": "application/json", "X-Ohmter-Protocol": str(PROTOCOL_VERSION)}
        if auth:
            if not self.token:
                raise BridgeRevoked(401, ERR_BAD_TOKEN, "no bridge token")
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]], *, auth: bool = True) -> Any:
        url = self.base_url + path
        try:
            status, body = await self._transport(method, url, payload, self._headers(auth))
        except OhmterError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure is "unreachable"
            raise OhmterUnreachable(0, "unreachable", str(exc)) from exc
        if 200 <= status < 300:
            return body
        code, message = _detail(body)
        if status == 401 and code == ERR_BRIDGE_REVOKED:
            raise BridgeRevoked(status, code, message)
        if status == 401:
            raise BridgeRevoked(status, code or ERR_BAD_TOKEN, message)
        if status in (400,) and code in (ERR_BAD_CODE, ERR_CODE_EXPIRED):
            raise BadPairingCode(status, code, message)
        if status >= 500 or status == 429:
            raise OhmterUnreachable(status, code or "server_error", message)
        raise OhmterError(status, code or "error", message)

    # -- calls --------------------------------------------------------------

    async def pair(self, code: str, *, name: str, ha_version: str, integration_version: str, instance_id: str) -> Dict[str, Any]:
        body = await self._call("POST", PATHS["pair"], {
            "code": code, "name": name, "ha_version": ha_version,
            "integration_version": integration_version, "instance_id": instance_id,
        }, auth=False)
        self.token = body["bridge_token"]
        return body

    async def put_mapping(self, devices: List[Dict[str, Any]]) -> Dict[str, Any]:
        """PUT the full mapping; the whole answer, including `removed_in_ohmter`."""
        body = await self._call("PUT", PATHS["devices"], {"devices": devices})
        return body if isinstance(body, dict) else {}

    async def put_devices(self, devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return list((await self.put_mapping(devices)).get("devices") or [])

    async def sync(self, *, bridge_meta: Dict[str, Any], reports: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._call("POST", PATHS["sync"], {"bridge": bridge_meta, "devices": reports})

    async def ack(self, command_id: int, *, ok: bool, error: Optional[str] = None) -> Dict[str, Any]:
        path = PATHS["ack"].format(command_id=int(command_id))
        return await self._call("POST", path, {"ok": bool(ok), "error": (error or None)})

    async def unpair(self) -> None:
        try:
            await self._call("DELETE", PATHS["unpair"], None)
        except BridgeRevoked:
            pass  # already gone from the other side; nothing to undo
