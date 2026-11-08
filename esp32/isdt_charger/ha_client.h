// Mirrors the charger into Home Assistant (same sensor.isdt_charger_* entities as ha_live.py)
// and sends the phone notification. Runs in its own FreeRTOS task so slow HTTPS calls never
// stall the BLE polling. Stops for good on 401/403 so it can't trip Home Assistant's IP ban.
#pragma once
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include "ca_certs.h"
#include "charger.h"
#include "secrets.h"

static const char* STATUS_ICONS[] = {"mdi:power-sleep", "mdi:battery-charging", "mdi:battery-check",
                                     "mdi:battery-arrow-down", "mdi:battery-check-outline"};
static const int HA_MAX_CELLS = 6;
static const uint32_t PUBLISH_INTERVAL_MS = 5000;

struct Snapshot {
  bool online = false;
  RealTime rt;
  float remainingS = -1;
  const char* phase = "";
};

class HAClient {
 public:
  void begin() {
    mutex_ = xSemaphoreCreateMutex();
    xTaskCreatePinnedToCore(taskEntry, "ha", 12288, this, 1, &task_, 1);
  }

  void update(const Snapshot& s, bool urgent) {
    xSemaphoreTake(mutex_, portMAX_DELAY);
    pending_ = s;
    hasPending_ = true;
    xSemaphoreGive(mutex_);
    if (urgent) xTaskNotifyGive(task_);
  }

  void notifyDone(const String& msg) {
    xSemaphoreTake(mutex_, portMAX_DELAY);
    doneMsg_ = msg;
    xSemaphoreGive(mutex_);
    xTaskNotifyGive(task_);
  }

  volatile bool stopped = false;
  char lastError[112] = "";  // fixed buffer: written by the HA task, read by loop()

 private:
  static void taskEntry(void* self) { static_cast<HAClient*>(self)->run(); }

  void run() {
    for (;;) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(PUBLISH_INTERVAL_MS));
      if (stopped || WiFi.status() != WL_CONNECTED) continue;

      Snapshot s;
      String done;
      bool have = false;
      xSemaphoreTake(mutex_, portMAX_DELAY);
      if (hasPending_) { s = pending_; have = true; hasPending_ = false; }
      done = doneMsg_;
      doneMsg_ = "";
      xSemaphoreGive(mutex_);

      if (done.length()) {
        // Companion-app service with high priority so Android delivers it immediately (the plain
        // notify.send_message action can't set a priority, so Doze could delay it).
        String body = "{\"title\":\"\xF0\x9F\x94\x8B Battery charged\",\"message\":\"" + done +
                      "\",\"data\":{\"priority\":\"high\",\"ttl\":0,\"importance\":\"high\","
                      "\"channel\":\"Charger\",\"tag\":\"isdt-charger\","
                      "\"push\":{\"interruption-level\":\"time-sensitive\"}}}";
        if (!post("/api/services/notify/" HA_NOTIFY_SERVICE, body) && !stopped && ++doneTries_ < 10) {
          xSemaphoreTake(mutex_, portMAX_DELAY);
          if (!doneMsg_.length()) doneMsg_ = done;  // retry on the next cycle
          xSemaphoreGive(mutex_);
        } else {
          doneTries_ = 0;
        }
      }
      if (have) publish(s);
    }
  }

  // ---- HTTP ----------------------------------------------------------------------------
  bool post(const String& path, const String& body) {
    if (stopped) return false;
    if (!tls_) {
      tls_ = new WiFiClientSecure();
      if (HA_USE_TLS) tls_->setCACert(CA_BUNDLE);
    }
    http_.setReuse(true);
    http_.setTimeout(10000);
    bool ok = HA_USE_TLS ? http_.begin(*tls_, HA_HOST, HA_PORT, path, true)
                         : http_.begin(plain_, HA_HOST, HA_PORT, path, false);
    if (!ok) { snprintf(lastError, sizeof(lastError), "connection failed"); return false; }
    http_.addHeader("Authorization", "Bearer " HA_TOKEN);
    http_.addHeader("Content-Type", "application/json");
    int code = http_.POST(body);
    if (code == 401 || code == 403) {
      stopped = true;
      snprintf(lastError, sizeof(lastError), "Home Assistant refused the token (HTTP %d) - stopped until reboot", code);
    } else if (code < 200 || code >= 300) {
      snprintf(lastError, sizeof(lastError), "%s", code < 0 ? http_.errorToString(code).c_str() : ("HTTP " + String(code)).c_str());
    } else {
      lastError[0] = 0;
    }
    http_.end();
    return code >= 200 && code < 300;
  }

  // ---- entity states -------------------------------------------------------------------
  static String num(float v, int decimals) { return String(v, decimals); }
  static String q(const String& s) { return "\"" + s + "\""; }

  static String sensor(const String& state, const char* name, const char* unit, const char* devClass,
                       const char* icon, const String& extra = "") {
    String a = "{\"friendly_name\":" + q(name);
    if (unit) a += ",\"unit_of_measurement\":" + q(unit) + ",\"state_class\":\"measurement\"";
    if (devClass) a += ",\"device_class\":" + q(devClass);
    if (icon) a += ",\"icon\":" + q(icon);
    a += extra + ",\"source\":\"esp32\"}";
    return "{\"state\":" + q(state) + ",\"attributes\":" + a + "}";
  }

  void send(int idx, const char* entity, const String& body) {
    uint32_t h = 2166136261u;  // FNV-1a: only send what changed
    for (size_t i = 0; i < body.length(); i++) h = (h ^ (uint8_t)body[i]) * 16777619u;
    if (sent_[idx] == h) return;
    if (post(String("/api/states/") + entity, body)) sent_[idx] = h;
  }

  void publish(const Snapshot& s) {
    const RealTime& rt = s.rt;
    const String NA = "unavailable";
    bool on = s.online && rt.valid, active = on && rt.status != 0, charging = on && rt.status == 1;
    int i = 0;

    send(i++, "binary_sensor.isdt_charger_connected",
         "{\"state\":" + q(on ? "on" : "off") + ",\"attributes\":{\"friendly_name\":\"Charger connected\",\"device_class\":\"connectivity\",\"source\":\"esp32\"}}");

    String extra = ",\"status_code\":" + (on ? String(rt.status) : String("null")) +
                   ",\"pack\":" + q(active ? String(rt.chemistryName()) + " " + rt.cells + "S" : "") +
                   ",\"phase\":" + q(charging ? s.phase : "");
    send(i++, "sensor.isdt_charger_status",
         sensor(on ? rt.statusName() : NA, "Charger status", nullptr, nullptr,
                on && rt.status < 5 ? STATUS_ICONS[rt.status] : "mdi:battery-off", extra));
    send(i++, "sensor.isdt_charger_battery", sensor(active ? String(rt.percent) : NA, "Charger battery", "%", "battery", nullptr));
    send(i++, "sensor.isdt_charger_time_left",
         sensor(charging && s.remainingS >= 0 ? num(s.remainingS / 60, 1) : active ? "0" : NA,
                "Charger time left", "min", "duration", "mdi:timer-sand"));

    String doneAt = NA;
    time_t now = time(nullptr);
    if (charging && s.remainingS >= 0 && now > 1700000000) {
      time_t eta = now + (time_t)s.remainingS;
      char buf[32];
      strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S+00:00", gmtime(&eta));
      doneAt = buf;
    }
    String doneBody = "{\"state\":" + q(doneAt) + ",\"attributes\":{\"friendly_name\":\"Charger done at\",\"device_class\":\"timestamp\",\"icon\":\"mdi:clock-check-outline\",\"source\":\"esp32\"}}";
    send(i++, "sensor.isdt_charger_done_at", doneBody);

    send(i++, "sensor.isdt_charger_pack_voltage", sensor(active ? num(rt.packMv / 1000.0f, 2) : NA, "Charger pack voltage", "V", "voltage", nullptr));
    send(i++, "sensor.isdt_charger_current", sensor(active ? num(rt.currentMa / 1000.0f, 2) : NA, "Charger current", "A", "current", nullptr));
    send(i++, "sensor.isdt_charger_power", sensor(active ? String(rt.outputW) : NA, "Charger power", "W", "power", nullptr));
    send(i++, "sensor.isdt_charger_added", sensor(active ? String(rt.capacityMah) : NA, "Charger added", "mAh", nullptr, "mdi:battery-plus-outline"));
    send(i++, "sensor.isdt_charger_elapsed", sensor(active ? num(rt.elapsedS / 60.0f, 1) : NA, "Charger elapsed", "min", "duration", "mdi:timer-outline"));
    send(i++, "sensor.isdt_charger_input_voltage", sensor(on ? num(rt.inputMv / 1000.0f, 1) : NA, "Charger input voltage", "V", "voltage", nullptr));

    for (int c = 0; c < HA_MAX_CELLS; c++) {
      bool has = active && c < rt.cells;
      String ir = has && rt.cellIr[c] ? num(rt.cellIr[c] / 10.0f, 1) : String("null");
      char entity[40], name[24];
      snprintf(entity, sizeof(entity), "sensor.isdt_charger_cell_%d", c + 1);
      snprintf(name, sizeof(name), "Charger cell %d", c + 1);
      send(i++, entity, sensor(has ? num(rt.cellMv[c] / 1000.0f, 3) : NA, name, "V", "voltage", nullptr, ",\"ir_mohm\":" + ir));
    }
  }

  SemaphoreHandle_t mutex_;
  TaskHandle_t task_;
  Snapshot pending_;
  bool hasPending_ = false;
  String doneMsg_;
  int doneTries_ = 0;
  WiFiClientSecure* tls_ = nullptr;
  WiFiClient plain_;
  HTTPClient http_;
  uint32_t sent_[32] = {0};
};
