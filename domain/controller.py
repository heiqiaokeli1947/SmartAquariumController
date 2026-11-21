"""鱼缸控制核心：唯一配置写入者、命令仲裁器和统一状态发布者。"""
import json
import time

import _thread

from domain.db import DEFAULTS
from domain.feeding import FeedingController
from domain.heater import HeaterController
from domain.light import LightController
from domain.pumps import PumpController
from domain.time_service import TimeService
from domain.wifi import WiFiManager
from version import VERSION


STAGE_NAMES = ("night", "sunrise", "day", "sunset", "night_end")


def _bool(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def _minute(value):
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
    else:
        hour, minute = divmod(int(value), 60)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid time")
    return hour * 60 + minute


def _time_text(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


class Controller:
    def __init__(self, bsp, database):
        self.bsp = bsp
        self.db = database
        self.db.ensure_defaults()
        self.system = self.db.get_all("system")
        saved_device = self.db.get_all("device")
        self.power_resume = _bool(self.system.get("power_resume", "1"))
        device = saved_device if self.power_resume else dict(DEFAULTS["device"])
        daylight = self.db.get_all("daylight")
        calibration = self.db.get_all("calibration")
        schedule = self._load_schedule(daylight)
        curves = [self._curve(calibration.get("led%d_curve" % index, ""), index)
                  for index in range(1, 5)]
        targets = self._led_targets(device.get("led_targets", "[0,0,0,0]"))
        self.light = LightController(device.get("light_mode", "auto"), targets, schedule, curves)
        self.pumps = PumpController(device, calibration)
        self.heater = HeaterController(device, calibration.get("heater_max_output", 60))
        self.feeding = FeedingController(int(self.system.get("feeding_duration", 600)))
        self.time = TimeService(bsp.rtc, int(self.system.get("timezone", 8)))
        self.time.initialize()
        self.wifi = WiFiManager(self.system)

        self._command_lock = _thread.allocate_lock()
        self._state_lock = _thread.allocate_lock()
        self._commands = []
        self._results = {}
        self._next_command_id = 1
        self._state_json = "{}"
        self._boot_tick = time.ticks_ms()
        self._last_publish = time.ticks_add(self._boot_tick, -200)
        self._last_stats = self._boot_tick
        self._alarms = []

    def start_services(self):
        try:
            self.wifi.start()
        except Exception as exc:
            print("WiFi start:", exc)

    @staticmethod
    def _led_targets(value):
        values = json.loads(value) if isinstance(value, str) else value
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError("four LED values required")
        return [max(0.0, min(100.0, float(item))) for item in values]

    @staticmethod
    def _curve(value, index):
        try:
            curve = json.loads(value) if isinstance(value, str) else value
            if not isinstance(curve, list) or len(curve) != 11:
                raise ValueError()
            curve = [max(0.0, min(100.0, float(item))) for item in curve]
            if any(curve[pos] > curve[pos + 1] for pos in range(10)):
                raise ValueError()
            return curve
        except (ValueError, TypeError):
            if value not in ("", None):
                print("Invalid LED%d calibration, using linear curve" % index)
            return [pos * 10.0 for pos in range(11)]

    def _load_schedule(self, values):
        schedule = []
        for name in STAGE_NAMES:
            targets = self._led_targets(values.get(name + "_leds", "[0,0,0,0]"))
            schedule.append({"name": name, "start": _minute(values.get(name + "_start", "00:00")),
                             "targets": targets})
        starts = [stage["start"] for stage in schedule]
        if len(set(starts)) != 5:
            raise ValueError("daylight stage times must be unique")
        return schedule

    def submit(self, command, source="web"):
        if not isinstance(command, dict) or not command.get("type"):
            raise ValueError("command type required")
        self._command_lock.acquire()
        try:
            command_id = self._next_command_id
            self._next_command_id += 1
            self._commands.append((command_id, dict(command), source))
            return command_id
        finally:
            self._command_lock.release()

    def command_result(self, command_id, remove=True):
        self._command_lock.acquire()
        try:
            item = self._results.get(command_id)
            if item is None:
                return None
            if remove:
                del self._results[command_id]
            return item[1]
        finally:
            self._command_lock.release()

    def _complete(self, command_id, ok=True, message="ok", data=None):
        result = {"id": command_id, "ok": ok, "message": message, "data": data}
        self._command_lock.acquire()
        try:
            self._results[command_id] = (time.ticks_ms(), result)
            if len(self._results) > 40:
                now = time.ticks_ms()
                for key, item in list(self._results.items()):
                    if time.ticks_diff(now, item[0]) > 30000:
                        del self._results[key]
        finally:
            self._command_lock.release()

    def note_web_activity(self):
        self.wifi.note_activity()

    def _process_commands(self):
        self._command_lock.acquire()
        try:
            pending = self._commands
            self._commands = []
        finally:
            self._command_lock.release()
        for command_id, command, source in pending:
            try:
                data = self._dispatch(command, source)
                self._complete(command_id, True, "ok", data)
            except Exception as exc:
                self._complete(command_id, False, str(exc))

    def _dispatch(self, command, source):
        kind = command["type"]
        if kind == "set_light":
            targets = command.get("targets")
            if targets is None and "channel" in command:
                targets = list(self.light.targets)
                channel = int(command["channel"]) - 1
                if not 0 <= channel < 4:
                    raise ValueError("LED channel must be 1..4")
                targets[channel] = float(command.get("value", 0))
            self.light.set(command.get("mode"), targets)
            self.db.update("device", {"light_mode": self.light.mode,
                                      "led_targets": json.dumps(self.light.targets)})
            return self.light.snapshot()
        if kind == "set_pump":
            name = command.get("pump", "")
            name = {"water": "water_pump", "air": "air_pump"}.get(name, name)
            if not self.pumps.set(name, command.get("mode"), command.get("target"),
                                  command.get("auto_speed")):
                raise ValueError("pumps are locked during feeding")
            pump = self.pumps.channels[name]
            self.db.update("device", {name + "_mode": pump["mode"],
                                      name + "_target": pump["target"],
                                      name + "_auto": pump["auto_speed"]})
            return pump
        if kind == "set_heater":
            self.heater.set(command.get("mode"), command.get("target_temp"),
                            command.get("manual_output"), command.get("hysteresis"),
                            command.get("max_temp"), command.get("max_output"))
            self.db.update("device", {
                "heater_mode": self.heater.mode, "heater_target_temp": self.heater.target_temp,
                "heater_manual_output": self.heater.manual_output,
                "heater_hysteresis": self.heater.hysteresis, "heater_max_temp": self.heater.max_temp,
            })
            self.db.set("calibration", "heater_max_output", self.heater.max_output)
            return self.heater.snapshot()
        if kind == "start_feeding":
            duration = int(command.get("duration", self.feeding.duration))
            if not self.feeding.start(duration, self.pumps.snapshot()):
                return self.feeding.snapshot()
            self.db.add_log("feeding_start", str(duration))
            return self.feeding.snapshot()
        if kind == "cancel_feeding":
            saved = self.feeding.cancel()
            if saved:
                self.pumps.restore(saved)
                self.db.add_log("feeding_cancel")
            return self.feeding.snapshot()
        if kind == "set_config":
            return self._set_config(command.get("values", command))
        if kind == "set_time":
            values = command.get("datetime")
            if values is None:
                values = [command[key] for key in ("year", "month", "day", "hour", "minute", "second")]
            if len(values) != 6:
                raise ValueError("datetime requires six fields")
            rtc_ok = self.time.set_datetime(*[int(value) for value in values])
            return {"time": self.time.snapshot(), "rtc_written": rtc_ok}
        if kind == "wifi_config":
            ssid = str(command.get("ssid", "")).strip()
            password = str(command.get("password", ""))
            auth = str(command.get("auth", "auto"))
            if not ssid:
                raise ValueError("SSID required")
            if auth != "OPEN" and len(password) < 8:
                raise ValueError("WiFi password requires at least 8 characters")
            self.db.update("system", {"wifi_ssid": ssid, "wifi_password": password, "wifi_auth": auth})
            self.system.update({"wifi_ssid": ssid, "wifi_password": password, "wifi_auth": auth})
            self.wifi.configure(ssid, password, auth)
            return self.wifi.snapshot()
        if kind == "wifi_scan":
            return self.wifi.scan()
        raise ValueError("unknown command: " + kind)

    def _set_config(self, values):
        if not isinstance(values, dict):
            raise ValueError("config object required")
        updates = {}
        if "power_resume" in values:
            self.power_resume = _bool(values["power_resume"])
            updates["power_resume"] = "1" if self.power_resume else "0"
        if "backlight" in values:
            backlight = max(0, min(100, int(values["backlight"])))
            updates["backlight"] = str(backlight)
            self.bsp.set_output("backlight", backlight)
        if "home_timeout" in values:
            timeout = int(values["home_timeout"])
            if not 5 <= timeout <= 300:
                raise ValueError("home timeout must be 5..300 seconds")
            updates["home_timeout"] = str(timeout)
        if "feeding_duration" in values:
            self.feeding.set_duration(values["feeding_duration"])
            updates["feeding_duration"] = str(self.feeding.duration)
        if "timezone" in values:
            timezone = int(values["timezone"])
            if not -12 <= timezone <= 14:
                raise ValueError("timezone must be -12..14")
            self.time.timezone = timezone
            updates["timezone"] = str(timezone)
        if updates:
            self.db.update("system", updates)
            self.system.update(updates)
        if "schedule" in values:
            schedule = self._validate_schedule(values["schedule"])
            self.light.set_schedule(schedule)
            stored = {}
            for stage in schedule:
                name = stage["name"]
                stored[name + "_start"] = _time_text(stage["start"])
                stored[name + "_leds"] = json.dumps(stage["targets"])
            self.db.update("daylight", stored)
        if "led_curves" in values:
            curves = values["led_curves"]
            if not isinstance(curves, list) or len(curves) != 4:
                raise ValueError("four LED curves required")
            checked = [self._curve(curve, index + 1) for index, curve in enumerate(curves)]
            self.light.curves = checked
            for index, curve in enumerate(checked, 1):
                self.db.set_json("calibration", "led%d_curve" % index, curve)
        return self._config_snapshot()

    @staticmethod
    def _validate_schedule(values):
        if not isinstance(values, list) or len(values) != 5:
            raise ValueError("five daylight stages required")
        result = []
        names = []
        for stage in values:
            name = stage.get("name")
            if name not in STAGE_NAMES or name in names:
                raise ValueError("invalid daylight stage names")
            names.append(name)
            result.append({"name": name, "start": _minute(stage["start"]),
                           "targets": Controller._led_targets(stage["targets"])})
        if set(names) != set(STAGE_NAMES) or len(set(stage["start"] for stage in result)) != 5:
            raise ValueError("incomplete schedule or duplicate times")
        return result

    def _config_snapshot(self):
        return {
            "power_resume": self.power_resume,
            "backlight": int(float(self.system.get("backlight", 70))),
            "home_timeout": int(self.system.get("home_timeout", 30)),
            "feeding_duration": self.feeding.duration,
            "timezone": self.time.timezone,
            "wifi_ssid": self.wifi.ssid, "wifi_auth": self.wifi.auth,
            "ap_ssid": self.wifi.ap_ssid,
            "schedule": self.light.schedule,
            "led_curves": self.light.curves,
        }

    def _update_alarms(self, sensors, water):
        alarms = []
        if water["level"] == "low":
            alarms.append("water_low")
        elif water["level"] == "high":
            alarms.append("water_high")
        elif water["level"] == "invalid":
            alarms.append("water_level_invalid")
        if sensors.get("water_temp") is None:
            alarms.append("water_temp_invalid")
        if sensors.get("env_temp") is None or sensors.get("humidity") is None:
            alarms.append("environment_invalid")
        if not self.time.time_valid:
            alarms.append("time_invalid")
        self._alarms = alarms

    def _publish(self, sensors, water):
        state = {
            "version": VERSION,
            "uptime": time.ticks_diff(time.ticks_ms(), self._boot_tick) // 1000,
            "sensors": sensors, "water": water, "light": self.light.snapshot(),
            "pumps": self.pumps.snapshot(), "heater": self.heater.snapshot(),
            "feeding": self.feeding.snapshot(), "time": self.time.snapshot(),
            "wifi": self.wifi.snapshot(), "alarms": self._alarms,
            "config": self._config_snapshot(),
            "display": {"available": self.bsp.display is not None,
                        "touch": bool(self.bsp.touch and self.bsp.touch.available),
                        "error": self.bsp.display_error},
        }
        encoded = json.dumps(state)
        self._state_lock.acquire()
        try:
            self._state_json = encoded
        finally:
            self._state_lock.release()

    def get_state(self):
        self._state_lock.acquire()
        try:
            encoded = self._state_json
        finally:
            self._state_lock.release()
        return json.loads(encoded)

    def get_config(self):
        return self.get_state().get("config", self._config_snapshot())

    def step(self):
        self._process_commands()
        self.bsp.read_water()
        self.bsp.poll_sensors()
        water = self.bsp.water_snapshot()
        sensors = self.bsp.sensor_snapshot()
        saved = self.feeding.update()
        if saved:
            self.pumps.restore(saved)
            self.db.add_log("feeding_finished")
        try:
            self.wifi.update()
            wifi_connected = self.wifi.sta.isconnected()
        except OSError as exc:
            wifi_connected = False
            self.wifi.state = "disconnected"
            self.wifi.last_error = str(exc)
        self.time.update(wifi_connected)

        led_outputs = self.light.update(self.time.minutes_of_day())
        for index, value in enumerate(led_outputs, 1):
            self.bsp.set_output("led%d" % index, value)
        pump_outputs = self.pumps.update(self.feeding.active)
        for name, value in pump_outputs.items():
            self.bsp.set_output(name, value)
        self.bsp.set_output("heater", self.heater.update(sensors, water))
        self._update_alarms(sensors, water)

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_stats) >= 60000:
            self._last_stats = now
            try:
                self.db.set_json("stats", "sensors", {"values": sensors, "water": water})
            except Exception as exc:
                print("Stats save:", exc)
        if time.ticks_diff(now, self._last_publish) >= 200:
            self._last_publish = now
            self._publish(sensors, water)
