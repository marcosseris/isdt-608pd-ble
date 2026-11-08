// ISDT 608PD BLE client - same protocol as isdt.py (from ISD Link 1.4.7).
//   bind:      write AF02  18 <16-byte app id> 00 <status>  -> notify AF02  19 <0 = ok>
//   real-time: write AF01  12 E4                            -> notify AF01  31 E5 ...
#pragma once
#include <NimBLEDevice.h>

static const NimBLEUUID SVC_AF00("af00");
static const NimBLEUUID CHR_AF01("af01");
static const NimBLEUUID CHR_AF02("af02");

static const char* STATUS_NAMES[] = {"Standby", "Charging", "Charge done", "Discharging", "Discharge done"};
static const char* CHEMISTRY[] = {"LiHV", "LiPo", "LiIon", "LiFe", "PbAc", "NiMH", "ULiHV"};

static const int MAX_CELLS = 8;

struct RealTime {
  bool valid = false;
  uint16_t inputMv = 0, outputW = 0, currentMa = 0, packMv = 0;
  uint8_t status = 0, chemistry = 0, percent = 0, balancePort = 0, cells = 0;
  uint32_t elapsedS = 0, capacityMah = 0;
  uint16_t cellMv[MAX_CELLS] = {0}, cellIr[MAX_CELLS] = {0};  // IR in 0.1 mOhm

  const char* statusName() const { return status < 5 ? STATUS_NAMES[status] : "Unknown"; }
  const char* chemistryName() const { return chemistry < 7 ? CHEMISTRY[chemistry] : "?"; }

  static bool parse(const uint8_t* b, size_t n, RealTime& rt) {
    if (n < 22 || b[1] != 0xE5) return false;
    auto u16 = [&](int i) { return (uint16_t)(b[i] | b[i + 1] << 8); };
    auto u32 = [&](int i) { return (uint32_t)b[i] | (uint32_t)b[i + 1] << 8 | (uint32_t)b[i + 2] << 16 | (uint32_t)b[i + 3] << 24; };
    rt.inputMv = u16(2);
    rt.outputW = u16(4);
    rt.status = b[6];
    rt.chemistry = b[7];
    rt.currentMa = u16(8);
    rt.packMv = u16(10);
    rt.percent = b[12];
    rt.balancePort = b[13];
    rt.elapsedS = u32(14);
    rt.capacityMah = u32(18);
    size_t i = 22;
    rt.cells = 0;
    if (rt.balancePort && n > 22) {
      uint8_t count = b[22];
      i = 23;
      while (rt.cells < count && rt.cells < MAX_CELLS && i + 4 <= n) {
        rt.cellMv[rt.cells] = u16(i);
        rt.cellIr[rt.cells] = u16(i + 2);
        rt.cells++;
        i += 4;
      }
    }
    rt.valid = true;
    return true;
  }
};

// Notifications arrive on the NimBLE task; hand them to loop() through a queue.
struct BlePacket {
  uint8_t chr;  // 1 = AF01, 2 = AF02
  uint8_t len;
  uint8_t data[64];
};

class Charger {
 public:
  enum State { IDLE, SCANNING, CONNECTED };

  void begin(const uint8_t appId[16]) {
    memcpy(appId_, appId, 16);
    queue_ = xQueueCreate(16, sizeof(BlePacket));
    NimBLEDevice::init("");
    NimBLEDevice::setPower(9);
  }

  // Call often. Returns true when a new reading is in `rt`.
  bool loop(RealTime& rt) {
    if (state_ != CONNECTED) {
      if (millis() - lastAttempt_ < retryDelayMs_) return false;
      lastAttempt_ = millis();
      if (connect()) {
        retryDelayMs_ = 2000;
        state_ = CONNECTED;
        misses_ = 0;
        statusText = "Connected";
      } else {
        disconnect();
        retryDelayMs_ = retryDelayMs_ ? min<uint32_t>(retryDelayMs_ * 2, 30000) : 1000;
      }
      return false;
    }

    if (!client_ || !client_->isConnected()) {
      statusText = "Disconnected";
      disconnect();
      return false;
    }

    bool got = false;
    BlePacket p;
    while (xQueueReceive(queue_, &p, 0) == pdTRUE) {
      if (p.chr == 1 && RealTime::parse(p.data, p.len, rt)) got = true;
    }
    if (got) {
      misses_ = 0;
      lastData_ = millis();
    }

    if (millis() - lastPoll_ >= 1000) {
      if (!got && lastPoll_ && millis() - lastData_ > 1500) misses_++;
      lastPoll_ = millis();
      const uint8_t req[] = {0x12, 0xE4};
      af01_->writeValue(req, sizeof(req), false);
      if (misses_ >= 6) {
        statusText = "No data from charger";
        disconnect();
      }
    }
    return got;
  }

  bool connected() const { return state_ == CONNECTED; }
  String statusText = "Starting";
  String address;

 private:
  bool connect() {
    if (!client_) {
      client_ = NimBLEDevice::createClient();
      // 30-50 ms interval and a 6 s supervision timeout (default is much shorter): Wi-Fi and BLE
      // share one radio, and brief gaps while Wi-Fi is busy were dropping the link.
      client_->setConnectionParams(24, 40, 0, 600);
    }

    // Reconnect straight to the charger we know; only scan when that fails twice in a row.
    bool ok = false;
    if (haveAddr_ && directFails_ < 2) {
      statusText = "Reconnecting";
      client_->setConnectTimeout(5000);
      ok = client_->connect(addr_);
      directFails_ = ok ? 0 : directFails_ + 1;
    }
    if (!ok) {
      const NimBLEAdvertisedDevice* found = scanForCharger();
      if (!found) return false;
      statusText = "Connecting";
      client_->setConnectTimeout(10000);
      addr_ = found->getAddress();
      haveAddr_ = true;
      address = addr_.toString().c_str();
      ok = client_->connect(found);
      NimBLEDevice::getScan()->clearResults();
      directFails_ = 0;
    }
    if (!ok) {
      statusText = "Connect failed";
      return false;
    }

    NimBLERemoteService* svc = client_->getService(SVC_AF00);
    af01_ = svc ? svc->getCharacteristic(CHR_AF01) : nullptr;
    af02_ = svc ? svc->getCharacteristic(CHR_AF02) : nullptr;
    if (!af01_ || !af02_) {
      statusText = "Unexpected services";
      return false;
    }
    QueueHandle_t q = queue_;
    auto handler = [q](NimBLERemoteCharacteristic* c, uint8_t* data, size_t len, bool) {
      BlePacket p;
      p.chr = c->getUUID() == CHR_AF01 ? 1 : 2;
      p.len = len > sizeof(p.data) ? sizeof(p.data) : len;
      memcpy(p.data, data, p.len);
      xQueueSend(q, &p, 0);
    };
    if (!af01_->subscribe(true, handler) || !af02_->subscribe(true, handler)) {
      statusText = "Subscribe failed";
      return false;
    }
    return bind();
  }

  const NimBLEAdvertisedDevice* scanForCharger() {
    state_ = SCANNING;
    statusText = "Scanning";
    NimBLEScan* scan = NimBLEDevice::getScan();
    scan->setActiveScan(true);
    NimBLEScanResults results = scan->getResults(6000, false);
    for (int i = 0; i < results.getCount(); i++) {
      const NimBLEAdvertisedDevice* d = results.getDevice(i);
      if (d->isAdvertisingService(SVC_AF00) || d->getName().find("608") != std::string::npos) return d;
    }
    statusText = "Charger not found (phone app open?)";
    scan->clearResults();
    return nullptr;
  }

  bool bind() {
    statusText = "Binding";
    xQueueReset(queue_);
    for (uint8_t mode = 0; mode < 2; mode++) {
      uint8_t req[19];
      req[0] = 0x18;
      memcpy(req + 1, appId_, 16);
      req[17] = 0x00;
      req[18] = mode;
      for (int attempt = 0; attempt < 5; attempt++) {
        af02_->writeValue(req, sizeof(req), false);
        uint32_t start = millis();
        BlePacket p;
        while (millis() - start < 1000) {
          if (xQueueReceive(queue_, &p, pdMS_TO_TICKS(50)) == pdTRUE && p.chr == 2 && p.len >= 2 && p.data[0] == 0x19) {
            if (p.data[1] == 0) return true;
            break;
          }
        }
      }
    }
    statusText = "Charger refused to bind";
    return false;
  }

  void disconnect() {
    if (client_ && client_->isConnected()) client_->disconnect();
    af01_ = af02_ = nullptr;
    state_ = IDLE;
    lastPoll_ = 0;
  }

  uint8_t appId_[16];
  QueueHandle_t queue_ = nullptr;
  NimBLEClient* client_ = nullptr;
  NimBLEAddress addr_;
  bool haveAddr_ = false;
  int directFails_ = 0;
  NimBLERemoteCharacteristic* af01_ = nullptr;
  NimBLERemoteCharacteristic* af02_ = nullptr;
  State state_ = IDLE;
  uint32_t lastAttempt_ = 0, retryDelayMs_ = 0, lastPoll_ = 0, lastData_ = 0;
  int misses_ = 0;
};
