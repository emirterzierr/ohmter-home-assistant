"""Names shared by the Home Assistant glue. Protocol values live in protocol.py."""

DOMAIN = "ohmter"
VERSION = "0.1.0"

CONF_CODE = "pairing_code"
CONF_NAME = "bridge_name"
CONF_BASE_URL = "base_url"
CONF_TOKEN = "bridge_token"
CONF_BRIDGE_ID = "bridge_id"
OPT_DEVICES = "devices"

STORAGE_VERSION = 1
STORAGE_KEY = "ohmter.{entry_id}"

ISSUE_REVOKED = "bridge_revoked"
ISSUE_CONFLICT = "bridge_conflict"

KIND_LABELS = {
    "ev": ("EV charger", "Laadpaal"),
    "thermostat": ("Thermostat", "Thermostaat"),
    "p1": ("P1 smart meter", "P1 slimme meter"),
    "solar": ("Solar inverter", "Omvormer"),
    "battery": ("Home battery (read only)", "Thuisbatterij (alleen lezen)"),
}
