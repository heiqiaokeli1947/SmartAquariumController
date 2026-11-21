"""ST7796U 480x320 横屏 SPI 驱动与 FT6336U 触摸驱动。"""
import time
from machine import Pin, SPI


class ST7796:
    WIDTH = 480
    HEIGHT = 320

    def __init__(self, spi_id=2, sck=18, mosi=9, miso=16, cs=15, dc=10, rst=11,
                 baudrate=60000000):
        self.cs = Pin(cs, Pin.OUT, value=1)
        self.dc = Pin(dc, Pin.OUT, value=1)
        self.rst = Pin(rst, Pin.OUT, value=1)
        self.spi = SPI(spi_id, baudrate=baudrate, polarity=0, phase=0,
                       sck=Pin(sck), mosi=Pin(mosi), miso=Pin(miso))
        self._last_flush = None
        self._init_panel()

    def _write(self, command, data=None):
        self.cs.value(0)
        self.dc.value(0)
        self.spi.write(bytes((command,)))
        if data:
            self.dc.value(1)
            self.spi.write(data if isinstance(data, bytes) else bytes(data))
        self.cs.value(1)

    def _init_panel(self):
        self.rst.value(0)
        time.sleep_ms(20)
        self.rst.value(1)
        time.sleep_ms(120)
        self._write(0x01)                 # 软件复位
        time.sleep_ms(120)
        self._write(0x11)                 # 退出休眠
        time.sleep_ms(120)
        self._write(0x3A, b"\x55")      # RGB565
        # 同型模组参考的电源、帧率与 gamma 参数。
        self._write(0xF0, b"\xC3")
        self._write(0xF0, b"\x96")
        self._write(0xB4, b"\x01")
        self._write(0xB7, b"\xC6")
        self._write(0xB9, b"\x02\xE0")
        self._write(0xC0, b"\x80\x16")
        self._write(0xC1, b"\x15")
        self._write(0xC2, b"\xA7")
        self._write(0xC5, b"\x23")
        self._write(0xE8, b"\x40\x8A\x00\x00\x29\x19\xA5\x33")
        self._write(0xE0, b"\xF0\x09\x0B\x06\x04\x15\x2F\x54\x42\x3C\x17\x14\x18\x1B")
        self._write(0xE1, b"\xE0\x09\x0B\x06\x04\x03\x2B\x43\x42\x3B\x16\x14\x17\x1B")
        self._write(0xF0, b"\x3C")
        self._write(0xF0, b"\x69")
        self._write(0x3A, b"\x55")      # 扩展命令后再次确认 RGB565
        self._write(0x36, b"\x28")      # MV|BGR，480x320 横屏且不左右镜像
        self._write(0x21)                 # IPS 模组反色显示
        self._write(0x13)
        time.sleep_ms(10)
        self._write(0x29)
        time.sleep_ms(120)

    def _window(self, x1, y1, x2, y2):
        self._write(0x2A, bytes((x1 >> 8, x1 & 0xFF, x2 >> 8, x2 & 0xFF)))
        self._write(0x2B, bytes((y1 >> 8, y1 & 0xFF, y2 >> 8, y2 & 0xFF)))
        self._write(0x2C)

    def flush(self, display, area, color_p):
        """LVGL 9 局部刷新回调；像素缓冲不复制，直接交给 SPI。"""
        try:
            x1, y1 = area.x1, area.y1
            x2, y2 = area.x2, area.y2
            size = (x2 - x1 + 1) * (y2 - y1 + 1) * 2
            self._window(x1, y1, x2, y2)
            self.cs.value(0)
            self.dc.value(1)
            self.spi.write(color_p.__dereference__(size))
            self.cs.value(1)
        except Exception as exc:
            print("LCD flush:", exc)
        finally:
            display.flush_ready()

    def clear(self, color=0):
        self._window(0, 0, self.WIDTH - 1, self.HEIGHT - 1)
        pixel = bytes((color >> 8, color & 0xFF))
        block = pixel * (self.WIDTH * 2)
        self.cs.value(0)
        self.dc.value(1)
        for _ in range(self.HEIGHT):
            self.spi.write(block)
        self.cs.value(1)


class FT6336U:
    ADDRESS = 0x38

    def __init__(self, i2c, width=480, height=320):
        self.i2c = i2c
        self.width = width
        self.height = height
        self.available = False
        try:
            self.available = self.ADDRESS in i2c.scan()
        except OSError:
            pass

    def read(self, indev, data):
        """LVGL 指针输入回调；横屏坐标不做二次旋转。"""
        data.state = 0
        if not self.available:
            data.continue_reading = False
            return
        try:
            raw = self.i2c.readfrom_mem(self.ADDRESS, 0x02, 5)
            touches = raw[0] & 0x0F
            if touches:
                raw_x = ((raw[1] & 0x0F) << 8) | raw[2]
                raw_y = ((raw[3] & 0x0F) << 8) | raw[4]
                # 该横屏模组：X=raw_y，Y=319-raw_x。
                x = raw_y
                y = self.height - 1 - raw_x
                data.point.x = max(0, min(self.width - 1, x))
                data.point.y = max(0, min(self.height - 1, y))
                data.state = 1
        except OSError:
            self.available = False
        data.continue_reading = False

    def retry(self):
        try:
            self.available = self.ADDRESS in self.i2c.scan()
        except OSError:
            self.available = False
        return self.available
