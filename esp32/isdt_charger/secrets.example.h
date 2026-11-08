// Copy to secrets.h and fill in.
#pragma once

// ---- Wi-Fi -------------------------------------------------------------------------------------
// Networks to try (the strongest one in range wins). You can also add networks later from the
// web page (http://isdt-charger.local) or the setup hotspot, without reflashing.
struct WifiCred { const char* ssid; const char* pass; };
static const WifiCred WIFI_NETWORKS[] = {
    // {"MyHomeWiFi", "password"},
    // {"Phone hotspot", "password"},
    {nullptr, nullptr}  // keep this last
};

// Password for the "ISDT-Charger-Setup" hotspot and the web page (user "admin"). 8+ characters.
#define SETUP_PASSWORD "change-me-123"

// ---- Home Assistant ----------------------------------------------------------------------------
#define HA_HOST "your-ha.duckdns.org"
#define HA_PORT 8123
#define HA_USE_TLS 1
#define HA_TOKEN "your-long-lived-access-token"
#define HA_NOTIFY_SERVICE "mobile_app_your_phone"  // notify.<this> - your phone in the companion app (Developer tools > Actions > notify.mobile_app_...)

// ---- Charger -----------------------------------------------------------------------------------
// Pairing ID sent to the charger (same as the desktop app's app_id.txt).
#define CHARGER_APP_ID "0123456789abcdef0123456789abcdef"
