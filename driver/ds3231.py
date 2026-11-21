"""DS3231 硬件 RTC 驱动。datetime 与 machine.RTC 格式兼容。"""


class DS3231:
    ADDRESS = 0x68

    def __init__(self, i2c):
        self.i2c = i2c

    @staticmethod
    def _bcd_to_int(value):
        return (value >> 4) * 10 + (value & 0x0F)

    @staticmethod
    def _int_to_bcd(value):
        return ((value // 10) << 4) | (value % 10)

    def datetime(self, value=None):
        if value is None:
            raw = self.i2c.readfrom_mem(self.ADDRESS, 0, 7)
            second = self._bcd_to_int(raw[0] & 0x7F)
            minute = self._bcd_to_int(raw[1] & 0x7F)
            hour_raw = raw[2]
            if hour_raw & 0x40:  # 12 小时制
                hour = self._bcd_to_int(hour_raw & 0x1F) % 12
                if hour_raw & 0x20:
                    hour += 12
            else:
                hour = self._bcd_to_int(hour_raw & 0x3F)
            weekday = self._bcd_to_int(raw[3] & 0x07)
            day = self._bcd_to_int(raw[4] & 0x3F)
            month = self._bcd_to_int(raw[5] & 0x1F)
            year = 2000 + self._bcd_to_int(raw[6])
            return (year, month, day, weekday, hour, minute, second, 0)
        year, month, day, weekday, hour, minute, second = value[:7]
        if not 2000 <= year <= 2099:
            raise ValueError("DS3231 year out of range")
        raw = bytes((
            self._int_to_bcd(second), self._int_to_bcd(minute),
            self._int_to_bcd(hour), self._int_to_bcd(weekday or 1),
            self._int_to_bcd(day), self._int_to_bcd(month),
            self._int_to_bcd(year - 2000),
        ))
        self.i2c.writeto_mem(self.ADDRESS, 0, raw)

    @staticmethod
    def valid(value):
        try:
            year, month, day, weekday, hour, minute, second = value[:7]
            if not (2020 <= year <= 2099 and 1 <= month <= 12 and
                    1 <= day <= 31 and 0 <= hour <= 23 and
                    0 <= minute <= 59 and 0 <= second <= 59):
                return False
            # 让系统时间实现校验真实日期（含闰年）。
            import time
            time.mktime((year, month, day, hour, minute, second, weekday - 1, 0))
            return True
        except (ValueError, TypeError, OverflowError, IndexError):
            return False
