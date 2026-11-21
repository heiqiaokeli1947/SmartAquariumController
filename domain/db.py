"""统一 BTree 持久化；业务层只接触字符串，所有库共用一把锁。"""
import json
import os
import time

import _thread
import btree


DB_ROOT = "/flash"
DB_FILES = {
    "system": "sysConfig.db",
    "device": "deviceConfig.db",
    "daylight": "daylight.db",
    "calibration": "calibration.db",
    "stats": "stats.db",
    "log": "logEvents.db",
}

DEFAULTS = {
    "system": {
        "wifi_ssid": "", "wifi_password": "", "wifi_auth": "auto",
        "ap_ssid": "SmartAquarium-Setup", "ap_password": "12345678",
        "power_resume": "1", "backlight": "70", "home_timeout": "30",
        "feeding_duration": "600", "timezone": "8",
    },
    "device": {
        "light_mode": "auto", "led_targets": "[65,65,45,45]",
        "water_pump_mode": "auto", "water_pump_target": "70", "water_pump_auto": "70",
        "air_pump_mode": "auto", "air_pump_target": "60", "air_pump_auto": "60",
        "heater_mode": "auto", "heater_target_temp": "26.0",
        "heater_manual_output": "0", "heater_hysteresis": "0.5",
        "heater_max_temp": "32.0",
    },
    "daylight": {
        "night_start": "00:00", "night_leds": "[0,0,0,0]",
        "sunrise_start": "06:00", "sunrise_leds": "[0,0,0,0]",
        "day_start": "08:00", "day_leds": "[80,80,60,60]",
        "sunset_start": "18:00", "sunset_leds": "[80,80,60,60]",
        "night_end_start": "20:00", "night_end_leds": "[0,0,0,0]",
    },
    "calibration": {
        "led1_curve": "[0,10,20,30,40,50,60,70,80,90,100]",
        "led2_curve": "[0,10,20,30,40,50,60,70,80,90,100]",
        "led3_curve": "[0,10,20,30,40,50,60,70,80,90,100]",
        "led4_curve": "[0,10,20,30,40,50,60,70,80,90,100]",
        "water_pump_max": "100", "air_pump_max": "100", "heater_max_output": "60",
    },
    "stats": {},
    "log": {},
}


class Database:
    def __init__(self, root=DB_ROOT):
        self.root = root
        self.lock = _thread.allocate_lock()
        try:
            os.mkdir(root)
        except OSError:
            pass

    def _path(self, section):
        return self.root + "/" + DB_FILES[section]

    @staticmethod
    def _close(db, stream):
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _quarantine(path):
        try:
            os.rename(path, path + ".corrupt." + str(time.ticks_ms()))
        except OSError:
            try:
                os.remove(path)
            except OSError:
                pass

    def _read_unlocked(self, section):
        path = self._path(section)
        for attempt in range(2):
            db = None
            stream = None
            try:
                try:
                    stream = open(path, "r+b")
                except OSError:
                    stream = open(path, "w+b")
                db = btree.open(stream)
                result = {}
                for key in db:
                    result[key.decode()] = db[key].decode()
                self._close(db, stream)
                return result
            except Exception as exc:
                self._close(db, stream)
                print("DB read", path, exc)
                self._quarantine(path)
        return {}

    def _write_unlocked(self, section, values):
        if not values:
            return
        path = self._path(section)
        for attempt in range(2):
            db = None
            stream = None
            try:
                try:
                    stream = open(path, "r+b")
                except OSError:
                    stream = open(path, "w+b")
                db = btree.open(stream)
                for key, value in values.items():
                    db[str(key).encode()] = str(value).encode()
                db.flush()
                self._close(db, stream)
                return
            except Exception as exc:
                self._close(db, stream)
                print("DB write", path, exc)
                self._quarantine(path)
        raise OSError("database write failed: " + section)

    def ensure_defaults(self):
        self.lock.acquire()
        try:
            for section, defaults in DEFAULTS.items():
                existing = self._read_unlocked(section)
                missing = dict((key, value) for key, value in defaults.items() if key not in existing)
                self._write_unlocked(section, missing)
        finally:
            self.lock.release()

    def get_all(self, section):
        self.lock.acquire()
        try:
            return self._read_unlocked(section)
        finally:
            self.lock.release()

    def get(self, section, key, default=None):
        return self.get_all(section).get(key, default)

    def update(self, section, values):
        self.lock.acquire()
        try:
            self._write_unlocked(section, values)
        finally:
            self.lock.release()

    def set(self, section, key, value):
        self.update(section, {key: value})

    def get_json(self, section, key, default):
        try:
            return json.loads(self.get(section, key, json.dumps(default)))
        except (ValueError, TypeError):
            return default

    def set_json(self, section, key, value):
        self.set(section, key, json.dumps(value))

    def add_log(self, event, detail=""):
        """仅保留 20 条关键事件，避免无限消耗 Flash。"""
        self.lock.acquire()
        try:
            data = self._read_unlocked("log")
            index = int(data.get("index", "0")) % 20
            stamp = int(time.time()) if hasattr(time, "time") else time.ticks_ms()
            self._write_unlocked("log", {
                "index": str(index + 1),
                "event_%02d" % index: json.dumps({"time": stamp, "event": event, "detail": detail}),
            })
        except Exception as exc:
            print("DB log:", exc)
        finally:
            self.lock.release()
