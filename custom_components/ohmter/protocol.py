"""The wire protocol between the Ohmter bridge and ohmter.nl - constants only.

This file is the integration's copy of what `core/bridge.py` declares on the
server. It imports nothing, so the server's test suite can `exec` it and fail
the build if the two halves disagree on a path, a field name or a kind. When
this file and core/bridge.py differ, one of them is wrong; fix both in one
commit.

Nothing here is a secret. The only secret in the whole design is the bridge
token Ohmter issues at pairing, and it lives in the config entry.
"""

PROTOCOL_VERSION = 1
SYNC_INTERVAL_SECONDS = 20
TOKEN_PREFIX = "ohb_"
DEFAULT_BASE_URL = "https://ohmter.nl"

DEVICE_KINDS = ("ev", "thermostat", "p1", "solar", "battery")

# What Ohmter may ask this bridge to do, per kind. Read-only kinds have none,
# which is the whole of the "battery is read-only in v1" decision on this side.
ACTIONS_BY_KIND = {
    "ev": ("on", "off", "set_current"),
    "thermostat": ("set_temperature", "restore"),
    "p1": (),
    "solar": (),
    "battery": (),
}

ROLES_BY_KIND = {
    "ev": ("switch", "power", "current", "energy", "plugged_in"),
    "thermostat": ("climate",),
    "p1": ("power", "import_power", "export_power", "import_energy", "export_energy"),
    "solar": ("power", "consumption", "grid_import", "grid_export"),
    "battery": ("soc", "power"),
}

# An EV needs only `power`: a charger with no writable switch is still worth
# watching, and Ohmter says "monitoring only" rather than refusing it. Control
# needs a switch *and* the household's confirmation (`control_confirmed`).
REQUIRED_ROLES = {
    "ev": ("power",),
    "thermostat": ("climate",),
    "p1": (),
    "solar": ("power",),
    "battery": ("soc",),
}

PATHS = {
    "pair": "/bridge/pair",
    "devices": "/bridge/devices",
    "sync": "/bridge/sync",
    "ack": "/bridge/commands/{command_id}/ack",
    "unpair": "/bridge/self",
}

# The Home Assistant domain each role's entity must come from, and the units a
# numeric role may report. The server checks the same tables; a mismatch is
# refused at mapping time with the role named, never turned into a number.
ROLE_DOMAINS = {
    "switch": ("switch",),
    "power": ("sensor",),
    "current": ("number",),
    "energy": ("sensor",),
    "plugged_in": ("binary_sensor",),
    "climate": ("climate",),
    "import_power": ("sensor",),
    "export_power": ("sensor",),
    "import_energy": ("sensor",),
    "export_energy": ("sensor",),
    "consumption": ("sensor",),
    "grid_import": ("sensor",),
    "grid_export": ("sensor",),
    "soc": ("sensor",),
}
ROLE_UNITS = {
    "power": ("w", "kw", "mw"),
    "import_power": ("w", "kw", "mw"),
    "export_power": ("w", "kw", "mw"),
    "consumption": ("w", "kw", "mw"),
    "grid_import": ("w", "kw", "mw"),
    "grid_export": ("w", "kw", "mw"),
    "energy": ("wh", "kwh", "mwh"),
    "import_energy": ("wh", "kwh", "mwh"),
    "export_energy": ("wh", "kwh", "mwh"),
    "current": ("a", "ma"),
    "soc": ("%",),
}

# Two fields that tie a command and a report to one mapping. A command names
# the entity it was decided for, as {"role", "entity_id"}, and the bridge
# refuses to run it against any other; a report carries the fingerprint of
# the role -> entity map it was read through, and the server ignores a report
# (and holds back that device's commands) when its own mapping is newer.
COMMAND_TARGET_FIELD = "target"
REPORT_MAPPING_REV_FIELD = "mapping_rev"

# Error codes the server sends in `detail.code`; the integration branches on
# these and nothing else.
ERR_BRIDGE_REVOKED = "bridge_revoked"
ERR_BAD_TOKEN = "bad_bridge_token"
ERR_BAD_CODE = "bad_code"
ERR_CODE_EXPIRED = "code_expired"

# How long the bridge holds a state Ohmter asked for after Ohmter goes quiet.
# Past this, the household's own hardware settings win (see watchdog.py).
HOLD_TIMEOUT_SECONDS = 900

# A `number` entity (charger max current) is written at most this often, so a
# fast load-balancing loop cannot hammer a charger whose vendor allows one
# write per quarter hour.
MIN_NUMBER_WRITE_INTERVAL_SECONDS = 60
