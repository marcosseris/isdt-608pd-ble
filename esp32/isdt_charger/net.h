// Wi-Fi: networks from secrets.h plus networks added on the web page (saved in NVS).
// Connects to whichever known network is strongest. If none is reachable for a while it
// also opens a setup hotspot so you can add one from your phone:
//   SSID "ISDT-Charger-Setup", password SETUP_PASSWORD, then browse to http://192.168.4.1
// Once on Wi-Fi the same page is at http://isdt-charger.local (login: admin / SETUP_PASSWORD).
#pragma once
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <memory>
#include "secrets.h"

static const int MAX_SAVED_NETWORKS = 10;
static const char* SETUP_AP_SSID = "ISDT-Charger-Setup";
static const uint32_t AP_AFTER_MS = 60000;  // open the setup hotspot after 1 min without Wi-Fi

class Net {
 public:
  WebServer server{80};

  void begin() {
    WiFi.mode(WIFI_STA);
    WiFi.setHostname("isdt-charger");
    loadNetworks();
    lastConnected_ = millis();
    server.on("/", HTTP_GET, [this] { if (auth()) page(); });
    server.on("/add", HTTP_POST, [this] { if (auth()) addNetwork(); });
    server.on("/delete", HTTP_POST, [this] { if (auth()) deleteNetwork(); });
    server.begin();
  }

  void loop() {
    if (WiFi.status() == WL_CONNECTED) {
      lastConnected_ = millis();
      if (!mdns_) mdns_ = MDNS.begin("isdt-charger");
      if (apOn_ && millis() - connectedSince_ > 30000) stopAp();
      if (!connectedSince_) connectedSince_ = millis();
    } else {
      connectedSince_ = 0;
      if (mdns_) { MDNS.end(); mdns_ = false; }
      if (millis() - lastTry_ > 15000) {
        lastTry_ = millis();
        if (multi_) multi_->run(8000);
      }
      if (!apOn_ && millis() - lastConnected_ > AP_AFTER_MS) startAp();
    }
    server.handleClient();
  }

  bool online() const { return WiFi.status() == WL_CONNECTED; }

  // Filled in by the sketch so the page can show live data.
  std::function<String()> statusHtml;

 private:
  struct Cred { String ssid, pass; bool builtIn; };
  std::vector<Cred> nets_;
  std::unique_ptr<WiFiMulti> multi_;
  bool apOn_ = false, mdns_ = false;
  uint32_t lastConnected_ = 0, connectedSince_ = 0, lastTry_ = 0;

  void loadNetworks() {
    nets_.clear();
    for (const WifiCred* w = WIFI_NETWORKS; w->ssid; w++) nets_.push_back({w->ssid, w->pass, true});
    Preferences p;
    p.begin("wifi", true);
    int n = p.getInt("n", 0);
    for (int i = 0; i < n; i++) nets_.push_back({p.getString(("s" + String(i)).c_str(), ""), p.getString(("p" + String(i)).c_str(), ""), false});
    p.end();
    multi_.reset(new WiFiMulti());  // WiFiMulti can't drop entries, so rebuild it
    for (auto& c : nets_) if (c.ssid.length()) multi_->addAP(c.ssid.c_str(), c.pass.c_str());
    lastTry_ = 0;
  }

  void saveNetworks() {
    Preferences p;
    p.begin("wifi", false);
    p.clear();
    int n = 0;
    for (auto& c : nets_) {
      if (c.builtIn) continue;
      p.putString(("s" + String(n)).c_str(), c.ssid);
      p.putString(("p" + String(n)).c_str(), c.pass);
      n++;
    }
    p.putInt("n", n);
    p.end();
  }

  void startAp() {
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(SETUP_AP_SSID, SETUP_PASSWORD);
    apOn_ = true;
  }

  void stopAp() {
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_STA);
    apOn_ = false;
  }

  bool auth() {
    if (server.authenticate("admin", SETUP_PASSWORD)) return true;
    server.requestAuthentication(BASIC_AUTH, "ISDT charger");
    return false;
  }

  static String esc(const String& s) {
    String o;
    for (char c : s) {
      if (c == '<') o += "&lt;"; else if (c == '>') o += "&gt;"; else if (c == '&') o += "&amp;"; else if (c == '"') o += "&quot;"; else o += c;
    }
    return o;
  }

  void page() {
    String h = F("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                 "<meta http-equiv=refresh content=10><title>ISDT charger</title><style>"
                 "body{font-family:system-ui,sans-serif;background:#16181d;color:#e8e8ea;margin:0;padding:16px;max-width:560px}"
                 "h2{margin:18px 0 8px}.card{background:#20232a;border-radius:10px;padding:12px;margin:8px 0}"
                 "td{padding:4px 8px 4px 0}input{background:#16181d;color:#e8e8ea;border:1px solid #444;border-radius:6px;padding:7px;width:100%;box-sizing:border-box;margin:4px 0}"
                 "button{background:#3b82f6;color:#fff;border:0;border-radius:6px;padding:8px 14px;margin-top:6px}"
                 ".del{background:#7f1d1d;padding:4px 10px;margin:0}.muted{color:#8b8f98;font-size:13px}</style></head><body>"
                 "<h1>ISDT 608PD</h1>");
    if (statusHtml) h += "<div class=card>" + statusHtml() + "</div>";

    h += "<h2>Wi-Fi networks</h2><div class=card><table>";
    String cur = WiFi.status() == WL_CONNECTED ? WiFi.SSID() : "";
    int idx = 0;
    for (auto& c : nets_) {
      h += "<tr><td>" + esc(c.ssid) + (c.ssid == cur ? " <b>(connected)</b>" : "") + "</td><td>";
      if (c.builtIn) h += "<span class=muted>in secrets.h</span>";
      else h += "<form method=post action=/delete style=margin:0><input type=hidden name=i value=" + String(idx) +
                "><button class=del>Remove</button></form>";
      h += "</td></tr>";
      if (!c.builtIn) idx++;
    }
    if (nets_.empty()) h += "<tr><td class=muted>No networks saved yet</td></tr>";
    h += "</table></div><h2>Add a network</h2><div class=card><form method=post action=/add>"
         "<input name=ssid placeholder='Wi-Fi name (SSID)' required maxlength=32>"
         "<input name=pass type=password placeholder=Password maxlength=63>"
         "<button>Save and connect</button></form><p class=muted>Up to " + String(MAX_SAVED_NETWORKS) +
         " networks. The charger monitor connects to whichever saved network is strongest.</p></div>";
    h += "<p class=muted>IP " + (WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : String("-")) +
         (apOn_ ? " &middot; setup hotspot on (192.168.4.1)" : "") + " &middot; up " + String(millis() / 60000) + " min</p></body></html>";
    server.send(200, "text/html", h);
  }

  void addNetwork() {
    String ssid = server.arg("ssid"), pass = server.arg("pass");
    ssid.trim();
    int saved = 0;
    for (auto& c : nets_) if (!c.builtIn) saved++;
    if (ssid.length() && saved < MAX_SAVED_NETWORKS) {
      for (auto it = nets_.begin(); it != nets_.end();) it = (!it->builtIn && it->ssid == ssid) ? nets_.erase(it) : it + 1;
      nets_.push_back({ssid, pass, false});
      saveNetworks();
      loadNetworks();
    }
    redirect();
  }

  void deleteNetwork() {
    int target = server.arg("i").toInt(), idx = 0;
    for (auto it = nets_.begin(); it != nets_.end(); ++it) {
      if (it->builtIn) continue;
      if (idx++ == target) { nets_.erase(it); break; }
    }
    saveNetworks();
    loadNetworks();
    redirect();
  }

  void redirect() {
    server.sendHeader("Location", "/");
    server.send(303);
  }
};
