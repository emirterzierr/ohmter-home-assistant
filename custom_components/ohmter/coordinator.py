"""The sync loop: state up, commands down, every SYNC_INTERVAL_SECONDS.

Home Assistant glue around the pure modules. What it owns:

* one aiohttp transport for `OhmterClient`;
* the per-device memory the executor and watchdog share, persisted with
  `Store` so a restart mid-window still restores;
* the set of executed command ids, so a redelivered command is acknowledged
  without running twice;
* the repair issues for a revoked or conflicting bridge.

A failed sync raises `UpdateFailed` *after* the watchdog has had its turn, so
"Ohmter is unreachable" and "hand the hardware back" never depend on each
other.
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import watchdog
from .const import (
    CONF_BASE_URL,
    CONF_BRIDGE_ID,
    CONF_TOKEN,
    DOMAIN,
    ISSUE_CONFLICT,
    ISSUE_REVOKED,
    OPT_DEVICES,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERSION,
)
from .executor import CommandRefused, execute
from .hastate import build_reports
from .ohmter_client import BridgeRevoked, OhmterClient, OhmterError, OhmterUnreachable
from .protocol import SYNC_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)
_MAX_REMEMBERED_IDS = 500


def make_transport(session: aiohttp.ClientSession):
    async def transport(method: str, url: str, payload: Optional[Dict[str, Any]], headers: Dict[str, str]):
        async with session.request(
            method, url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                body = {"error": (await resp.text())[:200]}
            return resp.status, body

    return transport


class OhmterCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Ohmter bridge {entry.data.get(CONF_BRIDGE_ID, '')}",
            update_interval=datetime.timedelta(seconds=SYNC_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.client = OhmterClient(
            entry.data[CONF_BASE_URL],
            entry.data[CONF_TOKEN],
            make_transport(async_get_clientsession(hass)),
            user_agent=f"OhmterHA/{VERSION} HomeAssistant/{HA_VERSION}",
        )
        self.devices: List[Dict[str, Any]] = list(entry.options.get(OPT_DEVICES) or [])
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY.format(entry_id=entry.entry_id))
        self.memories: Dict[str, Dict[str, Any]] = {}
        self.executed_ids: List[int] = []
        self.last_ok_sync: Optional[str] = None
        self.last_command_note: Optional[str] = None
        self.bridge_status: str = "starting"
        self._devices_signature: Optional[str] = None
        self._instance_id: Optional[str] = None

    # -- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self.memories = dict(stored.get("memories") or {})
        self.executed_ids = list(stored.get("executed_ids") or [])[-_MAX_REMEMBERED_IDS:]
        self.last_ok_sync = stored.get("last_ok_sync")
        self._instance_id = await async_get_instance_id(self.hass)

    async def async_shutdown(self) -> None:
        await self._persist()
        await super().async_shutdown()

    async def _persist(self) -> None:
        await self._store.async_save({
            "memories": self.memories,
            "executed_ids": self.executed_ids[-_MAX_REMEMBERED_IDS:],
            "last_ok_sync": self.last_ok_sync,
        })

    def _bridge_meta(self) -> Dict[str, Any]:
        return {"ha_version": HA_VERSION, "integration_version": VERSION, "instance_id": self._instance_id or ""}

    # -- helpers the executor needs -------------------------------------------

    def _get_state(self, entity_id: str):
        return self.hass.states.get(entity_id) if entity_id else None

    async def _call_service(self, domain: str, service: str, data: Dict[str, Any]) -> None:
        await self.hass.services.async_call(domain, service, data, blocking=True)

    def _device(self, key: str) -> Optional[Dict[str, Any]]:
        for d in self.devices:
            if d.get("device_key") == key:
                return d
        return None

    # -- the loop ---------------------------------------------------------------

    async def _async_update_data(self) -> Dict[str, Any]:
        now = dt_util.now().replace(tzinfo=None)
        try:
            # The mapping is read from the entry on every tick and re-sent when
            # it changed. Home Assistant coalesces back-to-back reloads, so a
            # second device mapped while the first reload was still running
            # never reached Ohmter when the send lived in setup alone.
            current = list(self.entry.options.get(OPT_DEVICES) or [])
            signature = json.dumps(current, sort_keys=True)
            if signature != self._devices_signature:
                await self.client.put_devices(current)
                self.devices = current
                self._devices_signature = signature
            reports = build_reports(self.devices, self._get_state, dt_util.utcnow().isoformat())
            result = await self.client.sync(bridge_meta=self._bridge_meta(), reports=reports)
        except BridgeRevoked as exc:
            self.bridge_status = "revoked"
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_REVOKED, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_REVOKED,
            )
            await self._run_watchdog(now)
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except OhmterUnreachable as exc:
            self.bridge_status = "unreachable"
            await self._run_watchdog(now)
            raise UpdateFailed(f"Ohmter unreachable: {exc}") from exc
        except OhmterError as exc:
            self.bridge_status = "error"
            await self._run_watchdog(now)
            raise UpdateFailed(str(exc)) from exc

        self.last_ok_sync = now.isoformat()
        watchdog.mark_ohmter_back(self.memories)
        status = str(result.get("bridge_status") or "ok")
        self.bridge_status = status
        if status == "conflict":
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_CONFLICT, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_CONFLICT,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CONFLICT)
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_REVOKED)

        for command in result.get("commands") or []:
            await self._run_command(command, now)
        await self._persist()
        return {
            "bridge_status": status,
            "last_ok_sync": self.last_ok_sync,
            "last_command": self.last_command_note,
            "accepted": result.get("accepted") or [],
            "unknown": result.get("unknown") or [],
            "commands_run": len(result.get("commands") or []),
        }

    async def _run_command(self, command: Dict[str, Any], now: datetime.datetime) -> None:
        key = str(command.get("device_key") or "")
        device = self._device(key)
        cid = int(command.get("id") or 0)
        if device is None:
            await self._safe_ack(cid, ok=False, error=f"no device mapped as {key!r} in Home Assistant")
            return
        mem = self.memories.setdefault(key, {})
        executed = set(self.executed_ids)
        try:
            note = await execute(
                command, device, get_state=self._get_state, call_service=self._call_service,
                memory=mem, now=now, executed_ids=executed,
            )
        except CommandRefused as exc:
            _LOGGER.info("Ohmter command %s on %s refused: %s", cid, key, exc)
            await self._safe_ack(cid, ok=False, error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a service call failure is a failed command, not a crash
            _LOGGER.warning("Ohmter command %s on %s failed: %s", cid, key, exc)
            await self._safe_ack(cid, ok=False, error=str(exc)[:200])
            return
        self.executed_ids = sorted(executed)[-_MAX_REMEMBERED_IDS:]
        self.last_command_note = f"{key}: {note}"
        await self._safe_ack(cid, ok=True)

    async def _safe_ack(self, cid: int, *, ok: bool, error: Optional[str] = None) -> None:
        if cid <= 0:
            return
        try:
            await self.client.ack(cid, ok=ok, error=error)
        except OhmterError as exc:
            _LOGGER.warning("could not acknowledge command %s: %s", cid, exc)

    async def _run_watchdog(self, now: datetime.datetime) -> None:
        """Hand the hardware back if Ohmter has been quiet for too long."""
        for synthetic in watchdog.releases_due(self.devices, self.memories, last_ok_sync=self.last_ok_sync, now=now):
            key = synthetic["device_key"]
            device = self._device(key)
            if device is None:
                continue
            mem = self.memories.setdefault(key, {})
            params = dict(synthetic.get("params") or {})
            params.pop("_release", None)
            # A release must run even if the interval guard would refuse it.
            mem["last_number_write_at"] = None
            try:
                note = await execute(
                    {**synthetic, "params": params}, device, get_state=self._get_state,
                    call_service=self._call_service, memory=mem, now=now, executed_ids=set(),
                )
                _LOGGER.warning("Ohmter quiet for too long; handed %s back to its own settings: %s", key, note)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("release of %s failed: %s", key, exc)
            watchdog.mark_released(mem, now)
            self.last_command_note = f"{key}: released (Ohmter quiet)"
        await self._persist()
