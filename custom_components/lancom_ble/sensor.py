"""Sensoren für die Lancom BLE Integration."""

from __future__ import annotations

from typing import Any
import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.bluetooth import MONOTONIC_TIME

from .const import DOMAIN
from . import LancomBLEScannerManager, LancomBLERemoteScanner, identifier_for

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setze Lancom BLE Sensoren pro AP auf."""
    _LOGGER.debug(
        "lancom_ble.sensor.async_setup_entry gestartet (entry_id=%s)",
        entry.entry_id,
    )

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not store:
        _LOGGER.debug("Kein Store für entry_id=%s gefunden", entry.entry_id)
        return

    manager: LancomBLEScannerManager | None = store.get("scanner_manager")
    if not manager:
        _LOGGER.debug(
            "Kein scanner_manager im Store für entry_id=%s", entry.entry_id
        )
        return

    entities: list[SensorEntity] = []

    # Existierende Scanner beim Setup berücksichtigen
    for scanner in manager.scanners.values():
        _LOGGER.debug(
            "Erzeuge Paket-Sensoren für existierenden Scanner %s",
            scanner.mac_upper,
        )
        entities.extend(create_sensors_for_scanner(hass, entry, scanner))

    if entities:
        async_add_entities(entities)
        _LOGGER.debug(
            "Es wurden %d Paket-Sensor(en) bei Setup hinzugefügt",
            len(entities),
        )

    # Auf neue Scanner reagieren (z. B. durch Webhook / add_ap)
    @callback
    def _handle_new_scanner(scanner: LancomBLERemoteScanner) -> None:
        _LOGGER.debug(
            "Neuer Scanner registriert (%s) -> Paket-Sensoren werden hinzugefügt",
            scanner.mac_upper,
        )
        async_add_entities(create_sensors_for_scanner(hass, entry, scanner))

    manager.register_scanner_listener(_handle_new_scanner)


def create_sensors_for_scanner(
    hass: HomeAssistant,
    entry: ConfigEntry,
    scanner: LancomBLERemoteScanner,
) -> list[SensorEntity]:
    """Erzeuge alle Paket-Sensoren für einen Scanner/AP."""
    return [
        LancomBLEPacketsTodaySensor(hass, entry, scanner),
        LancomBLEPacketsLastMinuteSensor(hass, entry, scanner),
        LancomBLEPacketsLastHourSensor(hass, entry, scanner),
        LancomBLEPacketsPerMinuteSensor(hass, entry, scanner),
    ]


class _BaseLancomBLEPacketSensor(SensorEntity):
    """Basisklasse für Paket-Sensoren (stellt Device-Verknüpfung bereit)."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "packets"
    # device_class lassen wir leer (nur "Anzahl", generisch)
    _attr_device_class = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        scanner: LancomBLERemoteScanner,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._scanner = scanner

        mac_upper = scanner.mac_upper
        ident = identifier_for(mac_upper)

        # DeviceInfo verknüpft den Sensor mit dem bestehenden AP-Device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ident)},
        )


class LancomBLEPacketsTodaySensor(_BaseLancomBLEPacketSensor):
    """Sensor: Anzahl Datenpakete, die heute über diesen AP eingetroffen sind."""

    # Totalzähler, der innerhalb eines Tages nur steigt
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        scanner: LancomBLERemoteScanner,
    ) -> None:
        super().__init__(hass, entry, scanner)

        mac_upper = scanner.mac_upper
        ident = identifier_for(mac_upper)

        self._attr_unique_id = f"{entry.entry_id}_{ident}_packets_today"
        self._attr_name = "Pakete heute"

    @property
    def native_value(self) -> int:
        return self._scanner.packets_today

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ap_mac": self._scanner.mac_upper,
            "scope": "today",
        }


class LancomBLEPacketsLastMinuteSensor(_BaseLancomBLEPacketSensor):
    """Sensor: Anzahl Datenpakete in der letzten Minute für diesen AP."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        scanner: LancomBLERemoteScanner,
    ) -> None:
        super().__init__(hass, entry, scanner)

        mac_upper = scanner.mac_upper
        ident = identifier_for(mac_upper)

        self._attr_unique_id = f"{entry.entry_id}_{ident}_packets_last_minute"
        self._attr_name = "Pakete letzte Minute"

    @property
    def native_value(self) -> int:
        # Berechne auf Basis des internen packet_times-Arrays
        try:
            packet_times = getattr(self._scanner, "_packet_times", None)
        except Exception:
            packet_times = None
        if not packet_times:
            return 0

        now_mono = MONOTONIC_TIME()
        cutoff_1m = now_mono - 60
        return sum(1 for t in packet_times if t >= cutoff_1m)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ap_mac": self._scanner.mac_upper,
            "scope": "last_minute",
        }


class LancomBLEPacketsLastHourSensor(_BaseLancomBLEPacketSensor):
    """Sensor: Anzahl Datenpakete in der letzten Stunde für diesen AP."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        scanner: LancomBLERemoteScanner,
    ) -> None:
        super().__init__(hass, entry, scanner)

        mac_upper = scanner.mac_upper
        ident = identifier_for(mac_upper)

        self._attr_unique_id = f"{entry.entry_id}_{ident}_packets_last_hour"
        self._attr_name = "Pakete letzte Stunde"

    @property
    def native_value(self) -> int:
        try:
            packet_times = getattr(self._scanner, "_packet_times", None)
        except Exception:
            packet_times = None
        if not packet_times:
            return 0

        now_mono = MONOTONIC_TIME()
        cutoff_1h = now_mono - 3600
        return sum(1 for t in packet_times if t >= cutoff_1h)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ap_mac": self._scanner.mac_upper,
            "scope": "last_hour",
        }


class LancomBLEPacketsPerMinuteSensor(_BaseLancomBLEPacketSensor):
    """Sensor: geschätzte Paketrate pro Minute (rolling window, letzte 60s)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    # Einheit: Pakete pro Minute
    _attr_native_unit_of_measurement = "packets/minute"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        scanner: LancomBLERemoteScanner,
    ) -> None:
        super().__init__(hass, entry, scanner)

        mac_upper = scanner.mac_upper
        ident = identifier_for(mac_upper)

        self._attr_unique_id = f"{entry.entry_id}_{ident}_packets_per_minute"
        self._attr_name = "Pakete pro Minute"

    @property
    def native_value(self) -> float | int:
        """Rate auf Basis der letzten 60 Sekunden."""
        try:
            packet_times = getattr(self._scanner, "_packet_times", None)
        except Exception:
            packet_times = None
        if not packet_times:
            return 0.0

        now_mono = MONOTONIC_TIME()
        cutoff_1m = now_mono - 60
        count_last_minute = sum(1 for t in packet_times if t >= cutoff_1m)

        # Da das Fenster genau 60s ist, entspricht die Rate der Anzahl.
        # Optional könnte man hier glätten oder mit kürzerem Fenster rechnen.
        return float(count_last_minute)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ap_mac": self._scanner.mac_upper,
            "scope": "rate_per_minute",
        }