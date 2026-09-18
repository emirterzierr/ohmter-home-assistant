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

A mapping Ohmter refuses is not a failed sync. The bridge keeps reporting
through the last mapping Ohmter accepted (persisted, so a restart does not
lose it), raises a repair issue that says so in the household's language, and
tries the new mapping again only when it changes. Before this, one refused
PUT - an 81-character device name was enough - failed every tick and the
config entry's setup, and the whole bridge went silent.

Whenever Ohmter lets go of a device, what it held is handed back here, on the
entity it changed (see watchdog.py): an accepted mapping that drops a device,
re-points a role or withdraws the grant; a device deleted in Ohmter; a revoked
bridge - at once, because Home Assistant stops refreshing a coordinator whose
auth failed, so the watchdog's 15 minutes would never come; and, through
`async_hand_back_all`, the integration being removed.
"""
from __future__ import annotations

import copy
import datetime
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
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
    ISSUE_MAPPING_REFUSED,
    ISSUE_REMOVED_IN_OHMTER,
    ISSUE_REVOKED,
    OPT_DEVICES,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERSION,
)
from .executor import CommandRefused, execute, hand_back, is_release
from .hastate import build_reports
from .mapping import ACTION_ROLE, lang_index, refusal_reason, replaced_entity, spec_for_server
from .ohmter_client import BridgeRevoked, OhmterClient, OhmterError, OhmterUnreachable
from .protocol import SYNC_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)
_MAX_REMEMBERED_IDS = 500
# A hand-back that cannot run is retried every tick for this long. An entity
# that has not come back in a week has been deleted from Home Assistant.
_PENDING_HANDBACK_MAX_SECONDS = 7 * 24 * 3600


def _replaced_in(hass: HomeAssistant, device: Dict[str, Any], action: str) -> Optional[str]:
    """The entity this action would touch, if Home Assistant replaced it since it was mapped."""
    role = ACTION_ROLE.get(str(action or ""))
    entity_id = (device.get("entities") or {}).get(role) if role else None
    if not entity_id:
        return None
    entry = er.async_get(hass).async_get(entity_id)
    return replaced_entity(device, action, entry.id if entry is not None else None)


async def async_hand_back(
    hass: HomeAssistant,
    device: Dict[str, Any],
    memory: Dict[str, Any],
    commands: List[Dict[str, Any]],
    now: datetime.datetime,
    *,
    why: str,
) -> List[tuple]:
    """Run one device's hand-back against this Home Assistant; logs each result."""
    if not commands:
        return []

    async def call_service(domain: str, service: str, data: Dict[str, Any]) -> None:
        await hass.services.async_call(domain, service, data, blocking=True)

    results = await hand_back(
        device, commands, get_state=lambda eid: hass.states.get(eid) if eid else None,
        call_service=call_service, memory=memory, now=now,
        is_replaced=lambda d, action: _replaced_in(hass, d, action),
    )
    for ok, note in results:
        (_LOGGER.warning if ok else _LOGGER.error)(
            "Ohmter let go of %s (%s); handed back: %s", device.get("device_key"), why, note)
    return results


async def async_hand_back_all(
    hass: HomeAssistant,
    devices: List[Dict[str, Any]],
    memories: Dict[str, Dict[str, Any]],
    pending: List[Dict[str, Any]],
    now: datetime.datetime,
    *,
    why: str,
) -> None:
    """Hand back everything Ohmter holds - the mapped devices and anything still pending."""
    for p in pending or []:
        device, mem = p.get("device") or {}, p.get("memory") or {}
        roles = set(p.get("roles") or [])
        await async_hand_back(hass, device, mem, watchdog.release_commands(device, mem, roles, label=why), now, why=why)
    for d in devices or []:
        key = d.get("device_key")
        mem = memories.get(key)
        if not mem or mem.get("released_at"):
            continue
        await async_hand_back(hass, d, mem, watchdog.release_commands(d, mem, label=why), now, why=why)
        watchdog.mark_released(mem, now)


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
        # The devices as Ohmter last *accepted* them. Reports are read through
        # this list, never through a mapping the server refused.
        self.devices: List[Dict[str, Any]] = []
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY.format(entry_id=entry.entry_id))
        self.memories: Dict[str, Dict[str, Any]] = {}
        # Hand-backs that could not run when Ohmter let go, each with the
        # device as it was mapped then - the entity Ohmter actually changed.
        self.pending_handbacks: List[Dict[str, Any]] = []
        self.executed_ids: List[int] = []
        self.last_ok_sync: Optional[str] = None
        self.last_command_note: Optional[str] = None
        self.bridge_status: str = "starting"
        self._devices_signature: Optional[str] = None
        self._refused_signature: Optional[str] = None
        self._instance_id: Optional[str] = None
        # Devices deleted in the Ohmter dashboard that this mapping still lists.
        self._removed_in_ohmter: set = set()

    # -- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self.memories = dict(stored.get("memories") or {})
        self.pending_handbacks = [p for p in (stored.get("pending_handbacks") or []) if isinstance(p, dict) and p.get("device")]
        self.executed_ids = list(stored.get("executed_ids") or [])[-_MAX_REMEMBERED_IDS:]
        self.last_ok_sync = stored.get("last_ok_sync")
        accepted = stored.get("accepted_devices")
        # An install from before accepted_devices was stored starts from the
        # saved options, which is what it reported through until now.
        self.devices = list(accepted) if isinstance(accepted, list) else list(self.entry.options.get(OPT_DEVICES) or [])
        self._instance_id = await async_get_instance_id(self.hass)

    async def async_shutdown(self) -> None:
        await self._persist()
        await super().async_shutdown()

    async def _persist(self) -> None:
        await self._store.async_save({
            "memories": self.memories,
            "pending_handbacks": self.pending_handbacks,
            "executed_ids": self.executed_ids[-_MAX_REMEMBERED_IDS:],
            "last_ok_sync": self.last_ok_sync,
            "accepted_devices": self.devices,
        })

    async def _send_mapping_if_changed(self, now: datetime.datetime) -> None:
        """PUT the saved mapping when it changed; a refusal keeps the accepted one."""
        current = list(self.entry.options.get(OPT_DEVICES) or [])
        signature = json.dumps(current, sort_keys=True)
        if signature in (self._devices_signature, self._refused_signature):
            return
        try:
            body = await self.client.put_mapping([spec_for_server(d) for d in current])
        except (BridgeRevoked, OhmterUnreachable):
            raise
        except OhmterError as exc:
            self._refused_signature = signature
            _LOGGER.warning("Ohmter refused the device mapping (%s); still reporting through the one it accepted", exc.code)
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_MAPPING_REFUSED, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_MAPPING_REFUSED,
                translation_placeholders={"reason": refusal_reason(exc.code, exc.status, lang_index(self.hass.config.language))},
            )
            return
        # Accepted. What Ohmter held on anything this mapping no longer gives
        # it - a device removed, an entity re-pointed, the grant withdrawn - is
        # handed back now, on the entity Ohmter changed. Until the server has
        # accepted the change the old mapping is still in force and the
        # watchdog still covers it, so nothing is handed back early.
        for old, commands, roles, gone in watchdog.mapping_handoffs(self.memories, self.devices, current):
            await self._let_go(old, roles, commands, now, why="mapping changed", device_gone=gone)
        watchdog.drop_orphaned_memory(self.memories, self.devices, current)
        self.devices = current
        self._devices_signature = signature
        self._refused_signature = None
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_MAPPING_REFUSED)
        self._note_removed_in_ohmter(body)
        await self._persist()

    async def _let_go(
        self,
        device: Dict[str, Any],
        roles: set,
        commands: List[Dict[str, Any]],
        now: datetime.datetime,
        *,
        why: str,
        device_gone: bool,
    ) -> List[tuple]:
        """Hand back what Ohmter holds on `roles` of `device`, and stop holding it.

        The live memory is cleared whether or not the hand-back ran: left in
        place after a re-point, the watchdog would one day apply it to the
        *new* entity. A hand-back that could not run - the old entity was
        unavailable - is kept with the device as it was mapped, and retried
        every tick until it runs.
        """
        key = device["device_key"]
        mem = self.memories.get(key) or {}
        snapshot = copy.deepcopy(mem)
        results = await async_hand_back(self.hass, device, mem, commands, now, why=why)
        if not all(ok for ok, _ in results):
            self.pending_handbacks.append({
                "device": device, "memory": snapshot, "roles": sorted(roles), "why": why, "since": now.isoformat(),
            })
        if device_gone:
            self.memories.pop(key, None)
        else:
            watchdog.forget(mem, set(roles))
        return results

    async def _retry_pending_handbacks(self, now: datetime.datetime) -> None:
        """Run the hand-backs that could not run when Ohmter let go."""
        still = []
        for p in self.pending_handbacks:
            device, mem, roles = p["device"], p["memory"], set(p.get("roles") or [])
            commands = watchdog.release_commands(device, mem, roles, label=p.get("why") or "retry")
            if not commands:
                continue
            results = await async_hand_back(self.hass, device, mem, commands, now, why=f"{p.get('why')}, retried")
            if all(ok for ok, _ in results):
                continue
            try:
                since = datetime.datetime.fromisoformat(str(p.get("since"))).replace(tzinfo=None)
            except ValueError:
                since = None
            if since is not None and (now.replace(tzinfo=None) - since).total_seconds() > _PENDING_HANDBACK_MAX_SECONDS:
                _LOGGER.error("Gave up handing %s back after a week; check it in Home Assistant", device.get("device_key"))
                continue
            still.append(p)
        if len(still) != len(self.pending_handbacks):
            self.pending_handbacks = still
            await self._persist()

    def _note_removed_in_ohmter(self, body: Dict[str, Any]) -> None:
        """What the server says was deleted in Ohmter and is still mapped here."""
        self._removed_in_ohmter = {str(k) for k in (body.get("removed_in_ohmter") or [])}
        self._raise_removed_in_ohmter()

    def _raise_removed_in_ohmter(self) -> None:
        keys = sorted(k for k in self._removed_in_ohmter if self._device(k) is not None)
        if not keys:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_REMOVED_IN_OHMTER)
            return
        names = [str((self._device(k) or {}).get("name") or k) for k in keys]
        ir.async_create_issue(
            self.hass, DOMAIN, ISSUE_REMOVED_IN_OHMTER, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REMOVED_IN_OHMTER, translation_placeholders={"devices": ", ".join(names)},
        )

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

    def _replaced(self, device: Dict[str, Any], action: str) -> Optional[str]:
        return _replaced_in(self.hass, device, action)

    # -- the loop ---------------------------------------------------------------

    async def _async_update_data(self) -> Dict[str, Any]:
        now = dt_util.now().replace(tzinfo=None)
        # Hand-backs never wait for Ohmter to answer.
        if self.pending_handbacks:
            await self._retry_pending_handbacks(now)
        try:
            # The mapping is read from the entry on every tick and re-sent when
            # it changed. Home Assistant coalesces back-to-back reloads, so a
            # second device mapped while the first reload was still running
            # never reached Ohmter when the send lived in setup alone.
            await self._send_mapping_if_changed(now)
            reports = build_reports(self.devices, self._get_state, dt_util.utcnow().isoformat())
            result = await self.client.sync(bridge_meta=self._bridge_meta(), reports=reports)
        except BridgeRevoked as exc:
            self.bridge_status = "revoked"
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_REVOKED, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_REVOKED,
            )
            # A revoked token is final: Ohmter will never command this bridge
            # again, and Home Assistant stops refreshing a coordinator whose
            # auth failed - so the watchdog's 15 minutes of silence would
            # never be counted. Everything Ohmter holds is handed back now.
            for d in self.devices:
                mem = self.memories.get(d["device_key"]) or {}
                if mem.get("released_at"):
                    continue
                roles = set(watchdog.HOLDABLE_ROLES.get(str(d.get("kind") or ""), ()))
                await self._let_go(d, roles, watchdog.release_commands(d, mem, roles, label="revoked"), now,
                                   why="bridge revoked", device_gone=False)
            self.last_command_note = "Ohmter revoked this bridge; everything it held was handed back"
            await self._persist()
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
        if is_release(command):
            await self._run_release(command, device, now)
            return
        replaced = self._replaced(device, str(command.get("action") or ""))
        if replaced:
            await self._safe_ack(cid, ok=False, error=(
                f"{replaced} was replaced in Home Assistant after it was mapped; not run. "
                "Check the device in Ohmter > Configure."))
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

    async def _run_release(self, command: Dict[str, Any], device: Dict[str, Any], now: datetime.datetime) -> None:
        """The household deleted this device in Ohmter: hand back what Ohmter holds.

        The server marks the command a release and cannot know more: what
        Ohmter left set lives in this bridge's memory, so the command's own
        action is not what runs. A redelivered release is acknowledged again,
        not run twice.
        """
        cid = int(command.get("id") or 0)
        if cid in set(self.executed_ids):
            await self._safe_ack(cid, ok=True)
            return
        key = device["device_key"]
        mem = self.memories.get(key) or {}
        roles = set(watchdog.HOLDABLE_ROLES.get(str(device.get("kind") or ""), ()))
        results = await self._let_go(device, roles, watchdog.release_commands(device, mem, roles, label="removed_in_ohmter"),
                                     now, why="removed in Ohmter", device_gone=False)
        self.executed_ids = sorted({*self.executed_ids, cid})[-_MAX_REMEMBERED_IDS:]
        # Say so now, not at the next restart: this device is still listed
        # here, and nothing will use it until the household removes it here.
        self._removed_in_ohmter.add(key)
        self._raise_removed_in_ohmter()
        failed = [note for ok, note in results if not ok]
        self.last_command_note = f"{key}: handed back ({'; '.join(n for _, n in results) or 'nothing Ohmter had set'})"
        await self._safe_ack(cid, ok=not failed, error=("; ".join(failed)[:240] or None))

    async def _safe_ack(self, cid: int, *, ok: bool, error: Optional[str] = None) -> None:
        if cid <= 0:
            return
        try:
            await self.client.ack(cid, ok=ok, error=error)
        except OhmterError as exc:
            _LOGGER.warning("could not acknowledge command %s: %s", cid, exc)

    async def _run_watchdog(self, now: datetime.datetime) -> None:
        """Hand the hardware back if Ohmter has been quiet for too long.

        A device whose hand-back could not run - its entity unavailable - is
        not marked released, so the next tick of the same outage tries again.
        What was done to an entity Home Assistant has since replaced under the
        same id counts as handed back: Ohmter never touched the one there now.
        """
        by_key: Dict[str, List[Dict[str, Any]]] = {}
        for synthetic in watchdog.releases_due(self.devices, self.memories, last_ok_sync=self.last_ok_sync, now=now):
            by_key.setdefault(synthetic["device_key"], []).append(synthetic)
        for key, commands in by_key.items():
            device = self._device(key)
            if device is None:
                continue
            mem = self.memories.setdefault(key, {})
            results = await async_hand_back(self.hass, device, mem, commands, now, why="Ohmter quiet")
            if all(ok for ok, _ in results):
                watchdog.mark_released(mem, now)
                self.last_command_note = f"{key}: released (Ohmter quiet)"
        await self._persist()
