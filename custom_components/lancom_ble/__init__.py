"""
Lancom BLE Passive Bluetooth Integration für Home Assistant.

Ziele und aktueller stabiler Stand:
- Pro Access Point (AP) genau EIN Device im Device Registry mit Connection ('mac', lower).
- NEUE Geräte werden mit eindeutigem Default-Namen angelegt: "Lancom AP <MAC>".
- Remote-Scanner (BaseHaRemoteScanner) pro AP, Bermuda-kompatibel (discovered_device_timestamps, time_since_last_detection).
- Self-Advert nutzt einen Basisnamen OHNE MAC (z. B. "Lancom AP" oder der vom Benutzer gesetzte Name); der Monitor hängt "(MAC)" idealerweise selbst an.
- Self-Advert wird bei jedem Aufruf wirklich gesendet (kein Early-Return), und Refresh wird zuverlässig neu geplant.
- Verwendet echte HA-Bluetooth Klassen (BLEDevice, BluetoothAdvertisementData) für Advert-Paare.
- Services: add_ap, sync_registry, consolidate_devices, force_scanner_name, fix_all_names.

Neu in dieser Version:
- Automatische Aktualisierung von device.name auf den (bereinigten) Benutzer-Namen, sobald name_by_user gesetzt/angepasst wird.
- Zusätzlich: Nach einer Namensänderung wird der Scanner für diese MAC einmal neu registriert, damit der Bluetooth/Advertisement Monitor
  seinen Titel sofort aktualisiert (einige HA-Versionen lesen den Anzeigenamen zur Scanner-Registrierzeit).
- device.name bleibt Default "Lancom AP <MAC>", solange kein Benutzername existiert.

Zusätzlich:
- Interne Paket-Counter pro AP (packets_today) und Rolling-Zeiten (packet_times).
- Sensor-Plattform wird per async_forward_entry_setups geladen.
"""

from __future__ import annotations

import logging
import re
from binascii import unhexlify
from typing import Any, Dict, Iterable, Tuple, Callable
from collections import deque
from datetime import datetime, timezone

from aiohttp.web_response import Response

from homeassistant.core import HomeAssistant, callback, ServiceCall, Event
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.device_registry import (
    EVENT_DEVICE_REGISTRY_UPDATED,
    EventDeviceRegistryUpdatedData,
)
from homeassistant.components.webhook import (
    async_register as async_register_webhook,
    async_unregister as async_unregister_webhook,
)
from homeassistant.components.bluetooth import (
    async_register_scanner,
    BaseHaRemoteScanner,
    MONOTONIC_TIME,
)

# Echte Modelle der Bluetooth-Integration (aktuelle HA-Versionen)
try:
    from homeassistant.components.bluetooth.models import (
        BLEDevice,
        BluetoothAdvertisementData,
    )
except ImportError:  # Fallback für sehr alte Builds
    from dataclasses import dataclass

    @dataclass
    class BLEDevice:
        address: str
        name: str | None = None
        rssi: int | None = None

    @dataclass
    class BluetoothAdvertisementData:
        local_name: str | None
        service_uuids: list[str]
        manufacturer_data: dict[int, bytes]
        service_data: dict[str, bytes]
        rssi: int | None
        tx_power: int | None

from .const import DOMAIN, CONF_WEBHOOK_ID, DEFAULT_WEBHOOK_ID, CONF_AP_MACS

_LOGGER = logging.getLogger(__name__)

# Regex für beliebige MAC (auch gemischt, mit Bindestrich oder ohne Trennzeichen)
MAC_ANY_PATTERN = re.compile(
    r"([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{12}"
)


# -------------------- Helfer -------------------- #


def format_ble_mac(raw: str) -> str:
    """Normiere Eingabe zu AA:BB:CC:DD:EE:FF (uppercase), behalte Original bei ungültiger Länge."""
    if not raw:
        return raw
    s = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(s) != 12:
        return raw.upper()
    return ":".join(s[i : i + 2] for i in range(0, 12, 2)).upper()


def identifier_for(mac_upper: str) -> str:
    """Stabiler Identifier für Device Registry."""
    return f"lancom_ble_{mac_upper.lower().replace(':', '_')}"


def normalize_input_mac_list(raw: str | list[str] | None) -> list[str]:
    """Wandelt String/Liste in bereinigte eindeutige MAC-Liste um."""
    if raw is None:
        return []
    items: Iterable[str]
    if isinstance(raw, list):
        items = raw
    else:
        tmp = raw
        for ch in [",", ";", "\n", " "]:
            tmp = tmp.replace(ch, "\n")
        items = tmp.split("\n")
    out: list[str] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        fm = format_ble_mac(item)
        if fm.count(":") == 5 and len(fm) == 17:
            out.append(fm)
        else:
            _LOGGER.warning(
                "Ungültige MAC ignoriert: %s (normalisiert=%s)", item, fm
            )
    return sorted(set(out))


def mac_connection_only(mac_upper: str) -> set[tuple[str, str]]:
    """Nur ('mac', lower) – wie Shelly. Vermeidet Mehrfach-Treffer."""
    return {("mac", mac_upper.lower())}


def _safe_int(value, default: int | None = None) -> int | None:
    """Robuste int-Konvertierung, mit Fallback default."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(round(value))
        except Exception:
            return default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except Exception:
            try:
                return int(round(float(value.strip())))
            except Exception:
                return default
    return default


def _strip_paren_mac(name: str, mac_upper: str) -> str:
    """Entferne ein trailing '(MAC)'."""
    suffix = f"({mac_upper})"
    if name.endswith(suffix):
        return name[: -len(suffix)].strip()
    return name


def _cleanup_user_name(name: str, mac_upper: str) -> str:
    """
    Bereinigt NUR Benutzer-Namen:
    - Entfernt trailing "(MAC)"
    - Entfernt isolierte MAC-Fragmente innerhalb des Namens
    - Lässt legitime Worte stehen
    Ziel: Schöner Basisname ohne doppelte MAC, aber Default "Lancom AP <MAC>" bleibt in device.name.
    """
    original = name
    name = _strip_paren_mac(name, mac_upper)
    name = MAC_ANY_PATTERN.sub("", name).strip()
    name = re.sub(r"\s{2,}", " ", name)
    if not name:
        name = "Lancom AP"
    if name != original:
        _LOGGER.debug("Benutzername bereinigt: '%s' -> '%s'", original, name)
    return name


def get_base_device_name(hass: HomeAssistant, mac_upper: str) -> str:
    """
    LOGIK Basisname:
    - Wenn user einen eigenen Namen gesetzt hat (name_by_user), verwenden wir dessen bereinigte Version ohne MAC.
    - Sonst nehmen wir "Lancom AP" als Basis (OHNE MAC), obwohl device.name den Default mit MAC hat.
    Dadurch:
      * Device Registry bleibt eindeutig: "Lancom AP <MAC>"
      * Self-Advert: "Lancom AP" oder Benutzername ohne MAC
    """
    devreg = dr.async_get(hass)
    ident = identifier_for(mac_upper)
    device = devreg.async_get_device(identifiers={(DOMAIN, ident)})
    if not device:
        return "Lancom AP"
    if device.name_by_user:
        return _cleanup_user_name(device.name_by_user, mac_upper)
    # Kein Benutzername → generischer Basisname
    return "Lancom AP"


def ensure_device_registry_default_name(hass: HomeAssistant, mac_upper: str):
    """
    Stellt sicher, dass NEUE Geräte mit Default "Lancom AP <MAC>" existieren.
    Ändert NICHT user-spezifische Namen.
    """
    devreg = dr.async_get(hass)
    ident = identifier_for(mac_upper)
    device = devreg.async_get_device(identifiers={(DOMAIN, ident)})
    if not device:
        return
    # Nur ändern, wenn kein Benutzername gesetzt wurde und aktueller Name nicht Default entspricht.
    if device.name_by_user:
        return
    desired = f"Lancom AP {mac_upper}"
    current = device.name or ""
    if current != desired:
        try:
            devreg.async_update_device(device.id, name=desired)
            _LOGGER.debug(
                "Device-Defaultname gesetzt: %s -> %s", mac_upper, desired
            )
        except Exception as e:
            _LOGGER.debug(
                "Konnte Defaultnamen nicht setzen (%s): %s", mac_upper, e
            )


def maybe_align_device_name_with_user(hass: HomeAssistant, mac_upper: str) -> bool:
    """
    Wenn der Benutzer einen Namen gesetzt hat (name_by_user), setze device.name auf den bereinigten Benutzer-Namen.
    Nur dann, wenn device.name aktuell generisch ist (Default 'Lancom AP <MAC>' oder beginnt mit 'Lancom AP').
    Rückgabe: True, wenn aktualisiert wurde.
    """
    devreg = dr.async_get(hass)
    ident = identifier_for(mac_upper)
    device = devreg.async_get_device(identifiers={(DOMAIN, ident)})
    if not device:
        return False
    user = device.name_by_user
    if not user:
        return False
    cleaned = _cleanup_user_name(user, mac_upper)
    current = device.name or ""
    is_default_like = current == f"Lancom AP {mac_upper}" or current.startswith(
        "Lancom AP"
    )
    if is_default_like and current != cleaned:
        try:
            devreg.async_update_device(device.id, name=cleaned)
            _LOGGER.debug(
                "device.name an Benutzername angeglichen: '%s' -> '%s'",
                current,
                cleaned,
            )
            return True
        except Exception as e:
            _LOGGER.debug(
                "device.name Update fehlgeschlagen (%s): %s", mac_upper, e
            )
    return False


# -------------------- Scanner Manager -------------------- #


class LancomBLEScannerManager:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.hass = hass
        self.config_entry = config_entry
        self._scanners: dict[str, LancomBLERemoteScanner] = {}
        self._cancel_callbacks: dict[str, Callable[[], None]] = {}
        # Listener, die informiert werden, wenn ein neuer Scanner entsteht
        self._scanner_listeners: list[
            Callable[[LancomBLERemoteScanner], None]
        ] = []

    def ensure_initial_scanners(self, mac_list: list[str]):
        for mac in mac_list:
            self.get_or_create_scanner(mac, inject_self=True)

    def get_or_create_scanner(
        self, ap_mac_raw: str, inject_self: bool = False
    ) -> "LancomBLERemoteScanner":
        mac_upper = format_ble_mac(ap_mac_raw)
        if mac_upper not in self._scanners:
            # Device zuerst erzeugen/aktualisieren
            self._register_or_update_device(mac_upper)
            ensure_device_registry_default_name(self.hass, mac_upper)
            # Remote-Scanner registrieren
            scanner = LancomBLERemoteScanner(self.hass, mac_upper)
            cancel = async_register_scanner(self.hass, scanner)
            self._scanners[mac_upper] = scanner
            self._cancel_callbacks[mac_upper] = cancel
            _LOGGER.info("Scanner registriert (source=%s).", mac_upper)
            # Listener über neuen Scanner informieren
            for cb in list(self._scanner_listeners):
                try:
                    cb(scanner)
                except Exception as e:
                    _LOGGER.debug(
                        "Scanner-Listener-Callback fehlgeschlagen für %s: %s",
                        mac_upper,
                        e,
                    )
            if inject_self:
                scanner.inject_self_advert()
        else:
            if inject_self:
                self._scanners[mac_upper].inject_self_advert()
        return self._scanners[mac_upper]

    def inject_ble(self, payload: dict[str, Any]):
        ap_mac = payload.get("deviceMac")
        if not ap_mac:
            _LOGGER.debug(
                "Webhook-Daten ohne deviceMac ignoriert: %s", payload
            )
            return
        scanner = self.get_or_create_scanner(ap_mac, inject_self=True)
        scanner.inject_ble(payload)

    def remove_scanner(self, mac_upper: str):
        if mac_upper in self._cancel_callbacks:
            try:
                self._cancel_callbacks[mac_upper]()
            except Exception as e:
                _LOGGER.debug(
                    "Fehler beim Deregistrieren von %s: %s", mac_upper, e
                )
            del self._cancel_callbacks[mac_upper]
        if mac_upper in self._scanners:
            del self._scanners[mac_upper]
        _LOGGER.info("Scanner entfernt: %s", mac_upper)

    def unload(self):
        for mac, cancel in list(self._cancel_callbacks.items()):
            try:
                cancel()
            except Exception as e:
                _LOGGER.debug(
                    "Fehler beim Entladen von %s: %s", mac, e
                )
            _LOGGER.debug("Scanner entladen: %s", mac)
        self._cancel_callbacks.clear()
        self._scanners.clear()
        self._scanner_listeners.clear()

    @property
    def scanners(self) -> dict[str, "LancomBLERemoteScanner"]:
        """Readonly-Zugriff auf bekannte Scanner."""
        return self._scanners

    def register_scanner_listener(
        self, callback: Callable[["LancomBLERemoteScanner"], None]
    ) -> None:
        """Registriere einen Callback, der bei neuen Scannern aufgerufen wird."""
        self._scanner_listeners.append(callback)

    def _register_or_update_device(self, mac_upper: str):
        devreg = dr.async_get(self.hass)
        ident = identifier_for(mac_upper)
        existing = devreg.async_get_device(identifiers={(DOMAIN, ident)})
        desired = mac_connection_only(mac_upper)
        if existing:
            changed = False
            if (
                self.config_entry.entry_id
                not in existing.config_entries
            ):
                devreg.async_update_device(
                    existing.id,
                    add_config_entry_id=self.config_entry.entry_id,
                )
                changed = True
            if existing.connections != desired:
                devreg.async_update_device(
                    existing.id, new_connections=desired
                )
                changed = True
            if changed:
                _LOGGER.debug("Device aktualisiert: %s", mac_upper)
            return
        devreg.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, ident)},
            name=f"Lancom AP {mac_upper}",  # Default mit MAC
            manufacturer="LANCOM Systems",
            model="Access Point (BLE Scanner)",
            sw_version="1.0",
            connections=desired,
        )
        _LOGGER.debug("Device neu erstellt: %s", mac_upper)

    def sync_existing_devices(self) -> int:
        devreg = dr.async_get(self.hass)
        count = 0
        for device in devreg.devices.values():
            for ident in device.identifiers:
                if not isinstance(ident, tuple) or len(ident) < 2:
                    continue
                id_domain, id_value = ident[0], ident[1]
                if (
                    id_domain == DOMAIN
                    and isinstance(id_value, str)
                    and id_value.startswith("lancom_ble_")
                ):
                    parts = (
                        id_value.replace("lancom_ble_", "").split("_")
                    )
                    if len(parts) == 6:
                        mac_upper = ":".join(p.upper() for p in parts)
                        # Stelle Defaultnamen sicher, nur wenn kein user name gesetzt
                        if not device.name_by_user:
                            ensure_device_registry_default_name(
                                self.hass, mac_upper
                            )
                        count += 1
                    break
        return count

    def consolidate_devices(self) -> int:
        devreg = dr.async_get(self.hass)
        grouped: dict[str, list[dr.DeviceEntry]] = {}
        for device in devreg.devices.values():
            if not any(
                isinstance(ident, tuple)
                and len(ident) >= 2
                and ident[0] == DOMAIN
                for ident in device.identifiers
            ):
                continue
            mac_conns = [
                c
                for c in device.connections
                if isinstance(c, tuple)
                and len(c) >= 2
                and c[0] == "mac"
            ]
            if not mac_conns:
                continue
            mac_lower = mac_conns[0][1]
            grouped.setdefault(mac_lower.upper(), []).append(device)
        removed = 0
        for mac_upper, devices in grouped.items():
            if len(devices) <= 1:
                continue
            primary = None
            ident_expected = identifier_for(mac_upper)
            for dev in devices:
                if any(
                    isinstance(ident, tuple)
                    and len(ident) >= 2
                    and ident[0] == DOMAIN
                    and ident[1] == ident_expected
                    for ident in dev.identifiers
                ):
                    primary = dev
                    break
            if primary is None:
                primary = devices[0]
            desired = mac_connection_only(mac_upper)
            if primary.connections != desired:
                devreg.async_update_device(
                    primary.id, new_connections=desired
                )
            for dev in devices:
                if dev.id == primary.id:
                    continue
                devreg.async_remove_device(dev.id)
                removed += 1
                _LOGGER.debug(
                    "Duplikat gelöscht: %s (%s)", dev.name, dev.id
                )
        return removed

    def _re_register_scanner(
        self, mac_upper: str
    ) -> "LancomBLERemoteScanner":
        """
        Scanner für eine MAC neu registrieren, um UI/Monitor-Anzeige zu aktualisieren.
        """
        # Alten Scanner deregistrieren
        cancel = self._cancel_callbacks.pop(mac_upper, None)
        if cancel:
            try:
                cancel()
            except Exception as e:
                _LOGGER.debug(
                    "Fehler beim Deregistrieren (re-register) von %s: %s",
                    mac_upper,
                    e,
                )
        if mac_upper in self._scanners:
            self._scanners.pop(mac_upper, None)

        # Neuen Scanner registrieren
        scanner = LancomBLERemoteScanner(self.hass, mac_upper)
        cancel_new = async_register_scanner(self.hass, scanner)
        self._scanners[mac_upper] = scanner
        self._cancel_callbacks[mac_upper] = cancel_new
        _LOGGER.debug(
            "Scanner neu registriert nach Namensänderung: %s", mac_upper
        )

        # Self-Advert sofort senden
        scanner.inject_self_advert()
        return scanner

    def handle_device_registry_update(
        self, event: Event[EventDeviceRegistryUpdatedData]
    ):
        """
        Reagiere auf Namensänderungen:
        - device.name wird automatisch an den bereinigten Benutzer-Namen angeglichen, wenn ein Benutzername existiert
          und device.name noch generisch ist.
        - Danach Self-Advert neu injizieren.
        - Zusätzlich: Wenn wir device.name angepasst haben, Scanner neu registrieren, damit der Monitor den neuen Titel übernimmt.
        """
        if event.data.get("action") != "update":
            return
        device_id = event.data.get("device_id")
        if not device_id:
            return
        devreg = dr.async_get(self.hass)
        device = devreg.async_get(device_id)
        if not device:
            return
        for ident in device.identifiers:
            if not (
                isinstance(ident, tuple)
                and len(ident) >= 2
                and ident[0] == DOMAIN
            ):
                continue
            value = ident[1]
            if not value.startswith("lancom_ble_"):
                continue
            parts = value.replace("lancom_ble_", "").split("_")
            if len(parts) != 6:
                continue
            mac_upper = ":".join(p.upper() for p in parts)
            # device.name an Benutzername angleichen (falls sinnvoll)
            updated = maybe_align_device_name_with_user(
                self.hass, mac_upper
            )
            # Scanner aktualisieren
            scanner = self._scanners.get(mac_upper)
            if updated:
                _LOGGER.debug(
                    "Gerätename angeglichen → Scanner wird neu registriert: %s",
                    mac_upper,
                )
                scanner = self._re_register_scanner(mac_upper)
            else:
                if scanner:
                    _LOGGER.debug(
                        "Gerätename geändert → Reinject Self Advert für %s",
                        mac_upper,
                    )
                    scanner.reinject_name()
            break

    def fix_all_names(self) -> int:
        """
        Bereinigt nur Benutzer-Namen von überflüssigen MAC-Anhängseln.
        Device Default 'Lancom AP <MAC>' bleibt.
        """
        devreg = dr.async_get(self.hass)
        changed = 0
        for device in devreg.devices.values():
            for ident in device.identifiers:
                if (
                    isinstance(ident, tuple)
                    and len(ident) >= 2
                    and ident[0] == DOMAIN
                ):
                    val = ident[1]
                    if not val.startswith("lancom_ble_"):
                        continue
                    parts = val.replace("lancom_ble_", "").split("_")
                    if len(parts) != 6:
                        continue
                    mac_upper = ":".join(p.upper() for p in parts)
                    if device.name_by_user:
                        cleaned = _cleanup_user_name(
                            device.name_by_user, mac_upper
                        )
                        if cleaned != device.name_by_user:
                            try:
                                devreg.async_update_device(
                                    device.id, name_by_user=cleaned
                                )
                                changed += 1
                            except Exception:
                                pass
        return changed


# -------------------- Remote Scanner -------------------- #


class LancomBLERemoteScanner(BaseHaRemoteScanner):
    __slots__ = (
        "hass",
        "mac_upper",
        "_mac_lower",
        "_discovered_devices",
        "_lancom_timestamps",
        "_last_detection_monotonic",
        "_self_injected",
        "_delayed_task_cancel",
        "_friendly_base_name",
        "_packet_times",
        "_packets_today",
        "_today_date",
    )

    def __init__(self, hass: HomeAssistant, ble_mac_upper: str):
        super().__init__(
            source=ble_mac_upper,
            adapter=ble_mac_upper,
            connector=None,
            connectable=False,
        )
        self.hass = hass
        self.mac_upper = ble_mac_upper
        self._mac_lower = ble_mac_upper.lower()
        # dict[MAC_UPPER] -> (BLEDevice, BluetoothAdvertisementData)
        self._discovered_devices: dict[
            str, Tuple[BLEDevice, BluetoothAdvertisementData]
        ] = {}
        self._lancom_timestamps: Dict[str, float] = {}
        self._last_detection_monotonic: float = 0.0
        self._self_injected = False
        self._delayed_task_cancel: Callable[[], None] | None = None
        self._friendly_base_name = "Lancom AP"
        # Paket-Statistik (pro AP)
        self._packet_times: deque[float] = deque()
        self._packets_today: int = 0
        self._today_date: str = (
            datetime.now(timezone.utc)
            .astimezone()
            .date()
            .isoformat()
        )

    @property
    def address(self) -> str:
        return self._mac_lower

    @property
    def name(self) -> str:
        return self._friendly_base_name

    @property
    def packets_today(self) -> int:
        """Anzahl der heute empfangenen Datenpakete für diesen AP."""
        return self._packets_today

    @property
    def discovered_devices(self):
        return [pair[0] for pair in self._discovered_devices.values()]

    @property
    def discovered_devices_and_advertisement_data(self):
        return self._discovered_devices

    @property
    def discovered_device_timestamps(self) -> Dict[str, float]:
        return self._lancom_timestamps

    def time_since_last_detection(self) -> float:
        now = MONOTONIC_TIME()
        if self._last_detection_monotonic <= 0:
            return 9999.0
        return max(0.0, now - self._last_detection_monotonic)

    def _touch_detection(self):
        self._last_detection_monotonic = MONOTONIC_TIME()

    def _set_stamp(self, mac_upper: str):
        self._lancom_timestamps[mac_upper] = MONOTONIC_TIME()
        self._touch_detection()

    def _roll_today_if_needed(self) -> None:
        """Setzt den 'heute'-Zähler zurück, wenn sich das Datum geändert hat."""
        now_local = datetime.now(timezone.utc).astimezone()
        today = now_local.date().isoformat()
        if today != self._today_date:
            _LOGGER.debug(
                "Neuer Tag erkannt für AP %s: %s -> %s, Zähler zurückgesetzt (alt=%d)",
                self.mac_upper,
                self._today_date,
                today,
                self._packets_today,
            )
            self._today_date = today
            self._packets_today = 0

    def _record_packet(self) -> None:
        """
        Registriert ein neu eingetroffenes Datenpaket für diesen AP.
        Hält eine Zeitliste für Rolling-Statistiken und zählt 'heute'.
        """
        self._roll_today_if_needed()

        now_mono = MONOTONIC_TIME()
        self._packet_times.append(now_mono)
        self._packets_today += 1

        # Rolling-Window: alte Einträge (>24h) entfernen
        cutoff_24h = now_mono - 24 * 3600
        while self._packet_times and self._packet_times[0] < cutoff_24h:
            self._packet_times.popleft()

    def _inject(
        self,
        mac_upper: str,
        local_name: str,
        rssi: int,
        details: dict[str, Any],
    ):
        """Hilfsfunktion: Füllt interne Strukturen und ruft _async_on_advertisement auf."""
        self._set_stamp(mac_upper)
        bledev = BLEDevice(address=mac_upper, name=local_name, rssi=rssi)
        adv = BluetoothAdvertisementData(
            local_name=local_name,
            service_uuids=[],
            manufacturer_data={},
            service_data={},
            rssi=rssi,
            tx_power=None,
        )
        self._discovered_devices[mac_upper] = (bledev, adv)
        try:
            self._async_on_advertisement(
                address=mac_upper,
                rssi=rssi,
                local_name=local_name,
                service_uuids=adv.service_uuids,
                service_data=adv.service_data,
                manufacturer_data=adv.manufacturer_data,
                tx_power=adv.tx_power,
                details=details,
                advertisement_monotonic_time=MONOTONIC_TIME(),
            )
            _LOGGER.debug(
                "Injected advert: name='%s' mac='%s' rssi=%s details=%s",
                local_name,
                mac_upper,
                rssi,
                details,
            )
        except Exception as e:
            _LOGGER.error(
                "Advert injection fehlgeschlagen für %s: %s", mac_upper, e
            )

    def _compute_base_name(self) -> str:
        base = get_base_device_name(self.hass, self.mac_upper)
        self._friendly_base_name = base
        return base

    def inject_self_advert(self):
        """
        Sende (oder erneuere) den Self-Advert sofort.
        """
        base = self._compute_base_name()
        ensure_device_registry_default_name(self.hass, self.mac_upper)
        self._inject(self.mac_upper, base, -55, {"lancom_self": True})
        self._self_injected = True
        _LOGGER.debug(
            "Self Advert (immediate) gesendet: base='%s', mac='%s'",
            base,
            self.mac_upper,
        )
        self._schedule_delayed_self_advert()

    def reinject_name(self):
        """
        Erneuere den Self-Advert, z. B. nach Namensänderung (Device Registry Update).
        """
        base = self._compute_base_name()
        self._inject(
            self.mac_upper, base, -54, {"lancom_self_rename": True}
        )
        _LOGGER.debug(
            "Self Advert (rename) gesendet: base='%s', mac='%s'",
            base,
            self.mac_upper,
        )
        self._schedule_delayed_self_advert()

    def _schedule_delayed_self_advert(self):
        """
        Plane einen Refresh-Self-Advert in wenigen Sekunden.
        Falls bereits ein Task existiert, ersetze ihn.
        """
        if self._delayed_task_cancel:
            try:
                self._delayed_task_cancel()
            except Exception:
                pass
            self._delayed_task_cancel = None

        def _delayed(_now):
            self._delayed_task_cancel = None
            base = self._compute_base_name()
            self._inject(
                self.mac_upper, base, -58, {"lancom_self_refresh": True}
            )
            _LOGGER.debug(
                "Self Advert (refresh) gesendet: base='%s', mac='%s'",
                base,
                self.mac_upper,
            )

        self._delayed_task_cancel = async_call_later(
            self.hass, 5, _delayed
        )

    @callback
    def inject_ble(self, data: dict[str, Any]):
        """Webhook: injiziere fremde Advertisements."""
        measurements = data.get("measurements", [])
        for m in measurements:
            raw = m.get("deviceAddress")
            if not raw:
                continue
            dev_mac_upper = format_ble_mac(raw)
            rssi_val = _safe_int(m.get("rssi"), default=-70)
            if rssi_val == -127:
                rssi_val = -70
            name = m.get("name") or dev_mac_upper
            adv_hex = m.get("advertisingData", "")
            if isinstance(adv_hex, str) and adv_hex:
                try:
                    _ = unhexlify(adv_hex)
                except Exception:
                    _LOGGER.debug(
                        "Ungültiges advertisingData (%s) für %s",
                        adv_hex,
                        dev_mac_upper,
                    )
            # Jedes Measurement zählt als ein Datenpaket für diesen AP
            self._record_packet()
            self._inject(
                dev_mac_upper, name, rssi_val, {"lancom": True}
            )


# -------------------- Setup / Lifecycle -------------------- #


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    webhook_id = entry.data.get(CONF_WEBHOOK_ID, DEFAULT_WEBHOOK_ID)
    ap_raw = entry.data.get(CONF_AP_MACS)
    ap_list = normalize_input_mac_list(ap_raw)

    manager = LancomBLEScannerManager(hass, entry)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "scanner_manager": manager,
        "webhook_id": webhook_id,
    }

    @callback
    def _handle_devreg(ev: Event[EventDeviceRegistryUpdatedData]):
        manager.handle_device_registry_update(ev)

    entry.async_on_unload(
        hass.bus.async_listen(
            EVENT_DEVICE_REGISTRY_UPDATED, _handle_devreg
        )
    )

    if ap_list:
        _LOGGER.info(
            "Initiale AP-Liste (%d): %s",
            len(ap_list),
            ", ".join(ap_list),
        )
        manager.ensure_initial_scanners(ap_list)
    else:
        _LOGGER.info(
            "Keine initiale AP-Liste: Scanner entstehen beim ersten Webhook."
        )

    @callback
    async def lancom_ble_webhook_handler(
        _hass_cb, _webhook_id_cb, request
    ):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                _LOGGER.error(
                    "Webhook erwartet JSON-Objekt, erhalten: %s",
                    type(payload),
                )
                return Response(status=200)
            manager.inject_ble(payload)
            return Response(status=200)
        except Exception as e:
            _LOGGER.error("Webhook Fehler: %s", e)
            return Response(status=200)

    async_register_webhook(
        hass,
        DOMAIN,
        "Lancom BLE Webhook",
        webhook_id,
        lancom_ble_webhook_handler,
    )

    # Sensor-Plattform explizit laden
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    _LOGGER.debug("Sensor-Plattform für Lancom BLE forwarded")

    # Services

    async def handle_add_ap(call: ServiceCall):
        mac = call.data.get("mac")
        if not mac:
            _LOGGER.error("Service add_ap ohne mac.")
            return
        fm = format_ble_mac(mac)
        if fm.count(":") != 5:
            _LOGGER.error(
                "Ungültige MAC beim add_ap: %s (normalisiert=%s)",
                mac,
                fm,
            )
            return
        manager.get_or_create_scanner(fm, inject_self=True)
        _LOGGER.info("AP hinzugefügt: %s", fm)

    async def handle_sync_registry(call: ServiceCall):
        count = manager.sync_existing_devices()
        _LOGGER.info(
            "Registry Sync abgeschlossen. %d Device(s) geprüft.", count
        )

    async def handle_consolidate_devices(call: ServiceCall):
        removed = manager.consolidate_devices()
        _LOGGER.info(
            "Konsolidierung abgeschlossen. %d Duplikat(e) entfernt.",
            removed,
        )

    async def handle_force_scanner_name(call: ServiceCall):
        mac = call.data.get("mac")
        if not mac:
            _LOGGER.error("Service force_scanner_name ohne mac.")
            return
        fm = format_ble_mac(mac)
        manager._re_register_scanner(fm)

    async def handle_fix_all_names(call: ServiceCall):
        changed = manager.fix_all_names()
        _LOGGER.info(
            "fix_all_names abgeschlossen. %d Benutzer-Namen bereinigt.",
            changed,
        )

    hass.services.async_register(
        DOMAIN, "add_ap", handle_add_ap, schema=None
    )
    hass.services.async_register(
        DOMAIN, "sync_registry", handle_sync_registry, schema=None
    )
    hass.services.async_register(
        DOMAIN,
        "consolidate_devices",
        handle_consolidate_devices,
        schema=None,
    )
    hass.services.async_register(
        DOMAIN,
        "force_scanner_name",
        handle_force_scanner_name,
        schema=None,
    )
    hass.services.async_register(
        DOMAIN, "fix_all_names", handle_fix_all_names, schema=None
    )

    _LOGGER.info(
        "Lancom BLE aktiv – Webhook: /api/webhook/%s", webhook_id
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not store:
        return True
    async_unregister_webhook(hass, store["webhook_id"])
    if "scanner_manager" in store:
        store["scanner_manager"].unload()
    # Sensor-Plattform entladen
    await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        for srv in (
            "add_ap",
            "sync_registry",
            "consolidate_devices",
            "force_scanner_name",
            "fix_all_names",
        ):
            try:
                hass.services.async_remove(DOMAIN, srv)
            except Exception:
                pass
    _LOGGER.info("Lancom BLE entladen (entry_id=%s).", entry.entry_id)
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    devreg = dr.async_get(hass)
    devices = list(
        dr.async_entries_for_config_entry(devreg, entry.entry_id)
    )
    removed = 0
    detached = 0
    for device in devices:
        if device.config_entries == {entry.entry_id}:
            devreg.async_remove_device(device.id)
            removed += 1
        else:
            devreg.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )
            detached += 1
    _LOGGER.info(
        "Lancom BLE entfernt: %d gelöscht, %d entkoppelt.",
        removed,
        detached,
    )


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    devreg = dr.async_get(hass)
    if device_entry.config_entries == {config_entry.entry_id}:
        devreg.async_remove_device(device_entry.id)
        _LOGGER.info(
            "Device gelöscht: %s (%s)",
            device_entry.name or device_entry.name_by_user,
            device_entry.id,
        )
        return True
    our_idents = {
        ident
        for ident in device_entry.identifiers
        if isinstance(ident, tuple)
        and len(ident) >= 2
        and ident[0] == DOMAIN
    }
    if our_idents:
        new_identifiers = set(device_entry.identifiers) - our_idents
        devreg.async_update_device(
            device_entry.id,
            new_identifiers=new_identifiers,
            remove_config_entry_id=config_entry.entry_id,
        )
        _LOGGER.info(
            "Device entkoppelt: %s (%s)",
            device_entry.name or device_entry.name_by_user,
            device_entry.id,
        )
    else:
        devreg.async_update_device(
            device_entry.id,
            remove_config_entry_id=config_entry.entry_id,
        )
        _LOGGER.info(
            "Device entkoppelt (keine Identifier): %s (%s)",
            device_entry.name or device_entry.name_by_user,
            device_entry.id,
        )
    return True