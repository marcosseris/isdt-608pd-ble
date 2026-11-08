// ISDT 608PD -> Home Assistant bridge for ESP32.
//
// Connects to the charger over BLE, estimates time-to-full, publishes the same
// sensor.isdt_charger_* entities as the desktop app (so the Charger dashboard tab works
// unchanged) and sends a phone notification through Home Assistant when a charge finishes.
//
// Setup: fill in secrets.h (generated from the desktop app), flash, done. Add Wi-Fi networks in
// secrets.h or later at http://isdt-charger.local / the "ISDT-Charger-Setup" hotspot.
// Board: any ESP32 with BLE. Partition scheme: "Minimal SPIFFS (1.9MB APP with OTA)".
// Only one thing can talk to the charger at a time: close ISD Link and the desktop app.

#include <ArduinoOTA.h>
#include "secrets.h"
#include "charger.h"
#include "estimator.h"
#include "ha_client.h"
#include "net.h"

Charger charger;
Estimator estimator;
HAClient ha;
Net net;
RealTime rt;
bool otaStarted = false, wasConnected = false;
static const uint32_t OFFLINE_GRACE_MS = 60000;
int lastStatus = -1;

static String hms(uint32_t s) {
  char b[16];
  snprintf(b, sizeof(b), "%02u:%02u:%02u", s / 3600, s / 60 % 60, s % 60);
  return b;
}

static void parseAppId(uint8_t out[16]) {
  const char* hex = CHARGER_APP_ID;
  for (int i = 0; i < 16; i++) {
    char byte[3] = {hex[i * 2], hex[i * 2 + 1], 0};
    out[i] = strtoul(byte, nullptr, 16);
  }
}

static Snapshot snapshot() {
  Snapshot s;
  s.online = charger.connected() && rt.valid;
  s.rt = rt;
  s.remainingS = estimator.remainingS();
  s.phase = estimator.phase();
  return s;
}

static String statusHtml() {
  String h = "<b>" + String(charger.connected() && rt.valid ? rt.statusName() : "Charger offline") + "</b> &middot; " + charger.statusText;
  if (charger.connected() && rt.valid && rt.status) {
    h += "<p style='font-size:34px;margin:6px 0'>" + String(rt.percent) + "%</p>";
    h += "<table><tr><td>Pack</td><td>" + String(rt.chemistryName()) + " " + rt.cells + "S, " + String(rt.packMv / 1000.0f, 2) + " V</td></tr>";
    h += "<tr><td>Current</td><td>" + String(rt.currentMa / 1000.0f, 2) + " A, " + rt.outputW + " W</td></tr>";
    h += "<tr><td>Added</td><td>" + String(rt.capacityMah) + " mAh in " + hms(rt.elapsedS) + "</td></tr>";
    float rem = estimator.remainingS();
    if (rt.status == 1) h += "<tr><td>Time left</td><td>" + (rem >= 0 ? hms(rem) : String("estimating...")) + " (" + estimator.phase() + ")</td></tr>";
    h += "<tr><td>Cells</td><td>";
    for (int i = 0; i < rt.cells; i++) h += String(rt.cellMv[i] / 1000.0f, 3) + " ";
    h += "V</td></tr></table>";
  }
  if (ha.lastError[0]) h += "<p style=color:#f87171>Home Assistant: " + String(ha.lastError) + "</p>";
  return h;
}

void setup() {
  Serial.begin(115200);
  Serial.println("\nISDT charger bridge starting");
  uint8_t appId[16];
  parseAppId(appId);

  net.statusHtml = statusHtml;
  net.begin();
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  ha.begin();
  ha.update(snapshot(), true);  // show "offline" until the charger is found
  charger.begin(appId);
}

void loop() {
  net.loop();

  if (net.online() && !otaStarted) {
    ArduinoOTA.setHostname("isdt-charger");
    ArduinoOTA.setPassword(SETUP_PASSWORD);
    ArduinoOTA.begin();
    otaStarted = true;
    Serial.printf("Wi-Fi %s, IP %s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  }
  if (otaStarted) ArduinoOTA.handle();

  if (charger.loop(rt)) {
    estimator.add(rt);
    if (lastStatus == 1 && rt.status == 2) {
      char msg[160];
      snprintf(msg, sizeof(msg), "%s %uS pack charged to %.2f V (%lu mAh in %s). Swap the battery!",
               rt.chemistryName(), rt.cells, rt.packMv / 1000.0f, (unsigned long)rt.capacityMah, hms(rt.elapsedS).c_str());
      Serial.println(msg);
      ha.notifyDone(msg);
    }
    ha.update(snapshot(), rt.status != lastStatus);
    lastStatus = rt.status;
  }

  // Short BLE drops reconnect within seconds: keep the last reading (and lastStatus, so a charge
  // that finishes during the gap still triggers the alert) and only report "offline" after a minute.
  static uint32_t disconnectedAt = 0;
  static bool reportedOffline = true;
  if (charger.connected() != wasConnected) {
    wasConnected = charger.connected();
    Serial.printf("Charger %s (%s)\n", wasConnected ? "connected" : "disconnected", charger.statusText.c_str());
    if (wasConnected) reportedOffline = false;
    else disconnectedAt = millis();
  }
  if (!wasConnected && !reportedOffline && millis() - disconnectedAt > OFFLINE_GRACE_MS) {
    reportedOffline = true;
    rt.valid = false;
    lastStatus = -1;
    ha.update(snapshot(), true);
  }

  static uint32_t lastLog = 0;
  if (charger.connected() && rt.valid && millis() - lastLog > 10000) {
    lastLog = millis();
    Serial.printf("%s %u%% %.2fV %.2fA left %.0fs heap %u\n", rt.statusName(), rt.percent, rt.packMv / 1000.0f,
                  rt.currentMa / 1000.0f, estimator.remainingS(), ESP.getFreeHeap());
  }
  delay(5);
}
