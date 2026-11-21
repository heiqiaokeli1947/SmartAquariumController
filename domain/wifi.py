"""非阻塞 WiFi STA 重连、配置 AP 与 AP 扫描。"""
import time

import machine
import network


class WiFiManager:
    CONNECT_TIMEOUT_MS = 25000
    RECONNECT_INTERVAL_MS = 30000
    AP_IDLE_TIMEOUT_MS = 300000

    def __init__(self, config):
        self.ssid = config.get("wifi_ssid", "")
        self.password = config.get("wifi_password", "")
        self.auth = config.get("wifi_auth", "auto")
        self.ap_ssid = config.get("ap_ssid", "SmartAquarium-Setup")
        self.ap_password = config.get("ap_password", "12345678")
        self.sta = network.WLAN(network.STA_IF)
        self.ap = network.WLAN(network.AP_IF)
        self.state = "offline"
        self.last_error = None
        self.scan_results = []
        self._deadline = 0
        self._retry_at = 0
        self._last_ap_activity = time.ticks_ms()

    def start(self):
        try:
            self.sta.active(True)
            if self.ssid:
                self._connect()
            else:
                self.start_ap()
        except OSError as exc:
            self.last_error = str(exc)
            self.start_ap()

    def _connect(self):
        try:
            if not self.sta.active():
                self.sta.active(True)
            if self.sta.isconnected():
                self.state = "connected"
                return
            self.sta.connect(self.ssid, self.password)
            self.state = "connecting"
            self._deadline = time.ticks_add(time.ticks_ms(), self.CONNECT_TIMEOUT_MS)
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            self.state = "disconnected"
            self._retry_at = time.ticks_add(time.ticks_ms(), self.RECONNECT_INTERVAL_MS)
            self.start_ap()

    def configure(self, ssid, password, auth="auto"):
        self.ssid = ssid.strip()
        self.password = password
        self.auth = auth
        self.note_activity()
        try:
            self.sta.disconnect()
        except OSError:
            pass
        self._retry_at = 0
        self._connect()

    def start_ap(self):
        try:
            if not self.ap.active():
                self.ap.active(True)
                self.ap.config(essid=self.ap_ssid, authmode=network.AUTH_WPA_WPA2_PSK,
                               password=self.ap_password)
                self.ap.config(max_clients=4)
                self._last_ap_activity = time.ticks_ms()
            return True
        except OSError as exc:
            self.last_error = str(exc)
            print("Config AP:", exc)
            return False

    def stop_ap(self):
        try:
            self.ap.active(False)
        except OSError:
            pass

    def note_activity(self):
        self._last_ap_activity = time.ticks_ms()

    def scan(self):
        try:
            if not self.sta.active():
                self.sta.active(True)
            raw = self.sta.scan()
            result = []
            auth_names = {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA/WPA2", 5: "WPA2/WPA3"}
            for item in raw:
                ssid = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                if ssid:
                    result.append({"ssid": ssid, "rssi": item[3],
                                   "auth": auth_names.get(item[4], str(item[4])), "hidden": bool(item[5])})
            result.sort(key=lambda entry: entry["rssi"], reverse=True)
            self.scan_results = result
            self.last_error = None
            return result
        except OSError as exc:
            self.last_error = str(exc)
            return []

    def update(self):
        now = time.ticks_ms()
        if self.sta.isconnected():
            if self.state != "connected":
                print("WiFi connected:", self.sta.ifconfig()[0])
            self.state = "connected"
            self._retry_at = time.ticks_add(now, self.RECONNECT_INTERVAL_MS)
            if self.ap.active():
                self.stop_ap()
            return
        if self.state == "connecting":
            if time.ticks_diff(now, self._deadline) >= 0:
                self.state = "disconnected"
                self.last_error = "connect timeout"
                self._retry_at = time.ticks_add(now, self.RECONNECT_INTERVAL_MS)
                self.start_ap()
        elif self.ssid and time.ticks_diff(now, self._retry_at) >= 0:
            self._connect()
        if not self.ssid and not self.ap.active():
            self.start_ap()
        if self.ap.active() and time.ticks_diff(now, self._last_ap_activity) >= self.AP_IDLE_TIMEOUT_MS:
            print("Config AP idle timeout, restarting")
            machine.reset()

    def snapshot(self):
        try:
            connected = self.sta.isconnected()
            ap_active = self.ap.active()
        except OSError:
            connected = False
            ap_active = False
        rssi = None
        if connected:
            try:
                rssi = self.sta.status('rssi')
            except Exception:
                rssi = None
        remaining = None
        if ap_active:
            elapsed = time.ticks_diff(time.ticks_ms(), self._last_ap_activity)
            remaining = max(0, (self.AP_IDLE_TIMEOUT_MS - elapsed) // 1000)
        return {
            "state": "connected" if connected else self.state,
            "connected": connected, "ssid": self.ssid,
            "ip": self.sta.ifconfig()[0] if connected else None,
            "rssi": rssi,
            "ap_active": ap_active,
            "ap_ssid": self.ap_ssid if ap_active else None,
            "ap_ip": self.ap.ifconfig()[0] if ap_active else None,
            "ap_idle_remaining": remaining,
            "auth": self.auth, "last_error": self.last_error,
            "scan_results": self.scan_results,
        }
