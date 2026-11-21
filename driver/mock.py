"""临时部署档位：当前板子只接 ST7796 屏幕和 HDC1080。

缺少 DS3231、TDS/NTC、水位开关时用这里的替身顶上，
保证日光节律、加热联锁和界面流程可以完整跑通。
接回真实硬件时把下面三个开关改为 False 即可。
"""
import machine
import random

from driver.ds3231 import DS3231


MOCK_RTC = True
MOCK_TDS = True
MOCK_WATER = True


class MockDS3231:
    """无 DS3231 时用 machine.RTC 顶替，掉电后回落到固定起始时间。"""

    def __init__(self, i2c=None):
        if not DS3231.valid(machine.RTC().datetime()):
            machine.RTC().datetime((2026, 11, 20, 4, 8, 0, 0, 0))

    def datetime(self, value=None):
        if value is None:
            year, month, day, weekday, hour, minute, second, _ = machine.RTC().datetime()
            # DS3231 的星期为 1~7，machine.RTC 为 0~6。
            return (year, month, day, weekday + 1, hour, minute, second, 0)
        machine.RTC().datetime((value[0], value[1], value[2],
                                max(0, min(6, value[3] - 1)),
                                value[4], value[5], value[6], 0))

    @staticmethod
    def valid(value):
        return DS3231.valid(value)


class MockTDS:
    """模拟 TDS/NTC 模块：水温与 TDS 做小幅随机游走，始终有效。"""

    def __init__(self):
        self.waiting = False
        self._temperature = 25.5
        self._tds = 180.0
        self._result = None

    def start_read(self, channel=1):
        self._temperature += random.uniform(-0.2, 0.2)
        self._temperature = min(29.0, max(23.0, self._temperature))
        self._tds = min(320.0, max(80.0, self._tds + random.uniform(-8.0, 8.0)))
        self._result = {
            "water_temp": round(self._temperature, 1),
            "tds": round(self._tds, 1),
            "conductivity": round(self._tds * 2.0, 1),
        }
        self.waiting = True
        return True

    def poll(self):
        if not self.waiting:
            return None
        self.waiting = False
        result = self._result
        self._result = None
        return result

    def cancel(self):
        self.waiting = False
        self._result = None
