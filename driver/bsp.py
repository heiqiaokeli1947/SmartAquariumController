"""SmartAquariumController ESP32-S3 N16R8 硬件抽象层。"""
import time
from machine import I2C, Pin, PWM

from driver.ds3231 import DS3231
from driver.hdc1080 import HDC1080
from driver.mock import MOCK_RTC, MOCK_TDS, MOCK_WATER, MockDS3231, MockTDS
from driver.tds import TDSModule


class BSP:
    # 固定引脚，不占用 N16R8 的 PSRAM、strapping 与 USB 引脚。
    LED_PINS = (4, 5, 12, 13)
    WATER_PUMP_PIN = 14
    AIR_PUMP_PIN = 21
    HEATER_PIN = 7
    BACKLIGHT_PIN = 17
    HIGH_WATER_PIN = 47
    LOW_WATER_PIN = 38
    I2C_SDA = 8
    I2C_SCL = 3
    SENSOR_STALE_MS = 10000

    def __init__(self, init_display=False):
        self._pwm = {}
        for index, pin in enumerate(self.LED_PINS, 1):
            self._pwm["led%d" % index] = PWM(Pin(pin), freq=25000, duty_u16=0)
        self._pwm["water_pump"] = PWM(Pin(self.WATER_PUMP_PIN), freq=1000, duty_u16=0)
        self._pwm["air_pump"] = PWM(Pin(self.AIR_PUMP_PIN), freq=1000, duty_u16=0)
        self._pwm["heater"] = PWM(Pin(self.HEATER_PIN), freq=10, duty_u16=0)
        self._pwm["backlight"] = PWM(Pin(self.BACKLIGHT_PIN), freq=1000, duty_u16=0)
        self._duty = dict((name, 0.0) for name in self._pwm)

        self.high_water_pin = Pin(self.HIGH_WATER_PIN, Pin.IN, Pin.PULL_DOWN)
        self.low_water_pin = Pin(self.LOW_WATER_PIN, Pin.IN, Pin.PULL_DOWN)
        if MOCK_WATER:
            # 无水位开关时固定为正常水位，避免误触发缺水保护。
            high_raw, low_raw = 0, 1
        else:
            high_raw = self.high_water_pin.value()
            low_raw = self.low_water_pin.value()
        self._water_raw = (high_raw, low_raw)
        self._water_stable = (high_raw, low_raw)
        self._water_candidate = (high_raw, low_raw)
        self._water_count = 5 if MOCK_WATER else 1

        self.i2c = I2C(1, sda=Pin(self.I2C_SDA), scl=Pin(self.I2C_SCL), freq=100000)
        self.rtc = MockDS3231(self.i2c) if MOCK_RTC else DS3231(self.i2c)
        if MOCK_TDS:
            self.tds = MockTDS()
            print("TDS/NTC: mock data (module not attached)")
        else:
            try:
                self.tds = TDSModule()
            except (OSError, ValueError) as exc:
                self.tds = None
                print("TDS UART init:", exc)
        self.hdc = HDC1080(self.i2c)
        self.display = None
        self.touch = None
        self.display_error = None
        if init_display:
            self.init_display()

        now = time.ticks_ms()
        self._values = {
            "tds": None, "conductivity": None, "water_temp": None,
            "env_temp": None, "humidity": None,
        }
        self._updated = dict((key, 0) for key in self._values)
        self._next_sample = now
        self._last_tds_attempt = now
        self._last_hdc_attempt = now
        self.sensor_errors = {"tds": 0 if self.tds is not None else 1, "hdc1080": 0}

    def init_display(self):
        if self.display is not None:
            return True
        try:
            from driver.st7796 import FT6336U, ST7796
            self.display = ST7796()
            self.touch = FT6336U(self.i2c)
            self.display_error = None
            return True
        except Exception as exc:
            self.display = None
            self.touch = None
            self.display_error = str(exc)
            print("Display init:", exc)
            return False

    @staticmethod
    def _percent(value):
        return max(0.0, min(100.0, float(value)))

    def set_output(self, name, percent):
        """原地更新 PWM，不重复创建 PWM 对象。"""
        if name not in self._pwm:
            raise KeyError(name)
        value = self._percent(percent)
        if value == self._duty[name]:
            return
        self._pwm[name].duty_u16(int(round(value * 65535.0 / 100.0)))
        self._duty[name] = value

    def output(self, name):
        return self._duty[name]

    def safe_outputs(self):
        for name in self._pwm:
            self.set_output(name, 0)

    def read_water(self):
        """10 Hz 调用；连续五次一致后才更新稳定水位。"""
        if MOCK_WATER:
            self._water_raw = (0, 1)
            self._water_stable = (0, 1)
            self._water_count = 5
            return self._water_raw
        raw = (self.high_water_pin.value(), self.low_water_pin.value())
        self._water_raw = raw
        if raw == self._water_candidate:
            self._water_count = min(5, self._water_count + 1)
        else:
            self._water_candidate = raw
            self._water_count = 1
        if self._water_count >= 5 and raw != self._water_stable:
            self._water_stable = raw
        return raw

    def water_snapshot(self):
        high, low = self._water_stable
        valid = self._water_count >= 5
        if not valid:
            level = "debouncing"
        elif high == 0 and low == 0:
            level = "low"
        elif high == 0 and low == 1:
            level = "normal"
        elif high == 1 and low == 1:
            level = "high"
        else:
            level = "invalid"
        return {
            "high_raw": self._water_raw[0], "low_raw": self._water_raw[1],
            "high": high, "low": low, "valid": valid, "level": level,
            "safe": valid and low == 1,
        }

    def _start_samples(self, now):
        if time.ticks_diff(now, self._next_sample) < 0:
            return
        self._next_sample = time.ticks_add(now, 1000)
        tds_interval = 5000 if self.sensor_errors["tds"] else 1000
        if self.tds is not None and not self.tds.waiting and time.ticks_diff(now, self._last_tds_attempt) >= tds_interval:
            self._last_tds_attempt = now
            try:
                self.tds.start_read(1)
            except OSError:
                self.sensor_errors["tds"] += 1
        hdc_interval = 5000 if self.sensor_errors["hdc1080"] else 1000
        if not self.hdc.waiting and time.ticks_diff(now, self._last_hdc_attempt) >= hdc_interval:
            self._last_hdc_attempt = now
            try:
                self.hdc.start()
            except OSError:
                self.sensor_errors["hdc1080"] += 1

    def poll_sensors(self):
        """每个主循环调用；UART/I2C 等待均不在此阻塞。"""
        now = time.ticks_ms()
        self._start_samples(now)
        if self.tds is not None:
            try:
                result = self.tds.poll()
                if result is False:
                    self.tds.cancel()
                    self.sensor_errors["tds"] += 1
                elif result:
                    self.sensor_errors["tds"] = 0
                    for key in ("tds", "conductivity", "water_temp"):
                        self._values[key] = result[key]
                        self._updated[key] = now
            except OSError:
                self.tds.cancel()
                self.sensor_errors["tds"] += 1
        try:
            result = self.hdc.poll()
            if result:
                self.sensor_errors["hdc1080"] = 0
                for key in ("env_temp", "humidity"):
                    self._values[key] = result[key]
                    self._updated[key] = now
        except OSError:
            self.hdc.cancel()
            self.sensor_errors["hdc1080"] += 1

    def sensor_snapshot(self):
        now = time.ticks_ms()
        result = {}
        for key, value in self._values.items():
            fresh = value is not None and time.ticks_diff(now, self._updated[key]) <= self.SENSOR_STALE_MS
            result[key] = round(value, 2) if fresh else None
            result[key + "_valid"] = fresh
        result["errors"] = dict(self.sensor_errors)
        return result
