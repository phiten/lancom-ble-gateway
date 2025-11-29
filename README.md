# Lancom BLE Gateway (Home Assistant Custom Integration)

Dieses Repository stellt eine Home Assistant Custom Integration bereit, mit der LANCOM Access Points als BLE-Scanner genutzt werden können.  
Die APs liefern per Webhook BLE-Advertisements an Home Assistant, die über die Bluetooth-Integration sichtbar gemacht werden.

## Features

- Pro Access Point genau EIN Gerät im Device Registry (`Lancom AP <MAC>`).
- Remote-Scanner pro AP (`BaseHaRemoteScanner`), kompatibel mit dem Home Assistant Bluetooth-Monitor:
  - `discovered_devices`
  - `discovered_devices_and_advertisement_data`
  - `discovered_device_timestamps`
  - `time_since_last_detection`
- Self-Advert pro AP mit bereinigtem Basisnamen (ohne MAC).
- Nutzung der offiziellen Home Assistant BLE-Modelle (`BLEDevice`, `BluetoothAdvertisementData`) mit Fallback für alte HA-Versionen.
- Webhook-basierte Einspeisung der LANCOM-Messdaten.
- Services:
  - `lancom_ble.add_ap`
  - `lancom_ble.sync_registry`
  - `lancom_ble.consolidate_devices`
  - `lancom_ble.force_scanner_name`
  - `lancom_ble.fix_all_names`
- **Paket-Statistik pro AP**:
  - Sensoren:
    - `Pakete heute`
    - `Pakete letzte Minute`
    - `Pakete letzte Stunde`
    - `Pakete pro Minute`

## Installation via HACS

1. HACS öffnen → **Integrationen**.
2. Oben rechts auf die **drei Punkte** → **Custom repositories**.
3. Dieses Repository hinzufügen (Typ **Integration**):

   `https://github.com/phiten/lancom-ble-gateway`

4. Nach `Lancom BLE Gateway` suchen und installieren.
5. Home Assistant neu starten.

## Konfiguration

1. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach `Lancom BLE Gateway` suchen.
2. Webhook-ID festlegen (Standard: `lancom_ble_webhook`).
3. Optional: AP-MACs (Liste, Komma- oder Zeilen-getrennt) eintragen, um Scanner sofort zu erstellen.

### Webhook-URL

Die Webhook-URL hat die Form:

`https://<dein-homeassistant>/api/webhook/<webhook_id>`

Beispiel (Standard-ID):

`https://homeassistant.local:8123/api/webhook/lancom_ble_webhook`

## Lizenz

Siehe [LICENSE](LICENSE).