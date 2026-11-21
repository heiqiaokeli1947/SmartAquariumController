"""HDC1080 温湿度传感器，转换等待由主循环非阻塞轮询。"""
import time


class HDC1080:
    ADDRESS = 0x40

    def __init__(self, i2c):
        self.i2c = i2c
        self.started_at = 0
        self.waiting = False

    def start(self):
        # 温度与湿度连续转换，14 位分辨率。
        self.i2c.writeto(self.ADDRESS, b"\x02\x10\x00")
        self.i2c.writeto(self.ADDRESS, b"\x00")
        self.started_at = time.ticks_ms()
        self.waiting = True

    def poll(self):
        if not self.waiting:
            return None
        if time.ticks_diff(time.ticks_ms(), self.started_at) < 25:
            return None
        self.waiting = False
        raw = self.i2c.readfrom(self.ADDRESS, 4)
        if len(raw) != 4:
            raise OSError("HDC1080 short frame")
        temp_raw = (raw[0] << 8) | raw[1]
        hum_raw = (raw[2] << 8) | raw[3]
        return {
            "env_temp": (temp_raw / 65536.0) * 165.0 - 40.0,
            "humidity": (hum_raw / 65536.0) * 100.0,
        }

    def cancel(self):
        self.waiting = False
