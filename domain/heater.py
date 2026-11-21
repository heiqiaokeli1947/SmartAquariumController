"""带回差的比例加热控制与独立安全联锁。"""


class HeaterController:
    def __init__(self, config, max_output):
        self.mode = config["heater_mode"]
        self.target_temp = float(config["heater_target_temp"])
        self.manual_output = float(config["heater_manual_output"])
        self.hysteresis = max(0.1, float(config["heater_hysteresis"]))
        self.max_temp = float(config["heater_max_temp"])
        self.max_output = max(0.0, min(100.0, float(max_output)))
        self.actual = 0.0
        self.heat_enabled = False
        self.protection = "starting"
        self.fade_step = 0.5

    def set(self, mode=None, target_temp=None, manual_output=None, hysteresis=None,
            max_temp=None, max_output=None):
        if mode is not None:
            if mode not in ("auto", "manual"):
                raise ValueError("invalid heater mode")
            self.mode = mode
        if max_temp is not None:
            self.max_temp = max(5.5, min(45.0, float(max_temp)))
            self.target_temp = min(self.target_temp, self.max_temp - 0.5)
        if target_temp is not None:
            self.target_temp = max(5.0, min(self.max_temp - 0.5, float(target_temp)))
        if manual_output is not None:
            self.manual_output = max(0.0, min(100.0, float(manual_output)))
        if hysteresis is not None:
            self.hysteresis = max(0.1, min(5.0, float(hysteresis)))
        if max_output is not None:
            self.max_output = max(0.0, min(100.0, float(max_output)))

    def _protection(self, temperature, water):
        if not water["valid"]:
            return "water_debouncing"
        if water["level"] in ("low", "invalid"):
            return "water_" + water["level"]
        if temperature is None:
            return "sensor_invalid"
        if temperature >= self.max_temp:
            return "overtemperature"
        return None

    def update(self, sensors, water):
        temperature = sensors.get("water_temp")
        self.protection = self._protection(temperature, water)
        if self.protection:
            self.heat_enabled = False
            self.actual = 0.0
            return 0.0
        if self.mode == "auto":
            if temperature >= self.target_temp:
                self.heat_enabled = False
            elif temperature <= self.target_temp - self.hysteresis:
                self.heat_enabled = True
            desired = 0.0
            if self.heat_enabled:
                ratio = (self.target_temp - temperature) / self.hysteresis
                desired = self.max_output * max(0.1, min(1.0, ratio))
        else:
            desired = min(self.manual_output, self.max_output)
        difference = desired - self.actual
        if abs(difference) <= self.fade_step:
            self.actual = desired
        else:
            self.actual += self.fade_step if difference > 0 else -self.fade_step
        return self.actual

    def snapshot(self):
        return {
            "mode": self.mode, "target_temp": self.target_temp,
            "manual_output": self.manual_output, "hysteresis": self.hysteresis,
            "max_temp": self.max_temp, "max_output": self.max_output,
            "actual": round(self.actual, 1), "heat_enabled": self.heat_enabled,
            "protection": self.protection,
        }
