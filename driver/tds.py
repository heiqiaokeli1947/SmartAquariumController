"""UART TDS/NTC 模块非阻塞协议驱动。"""
import time
from machine import UART


class TDSModule:
    FRAME_HEAD = 0x55
    FRAME_LEN = 8
    RESPONSE_LEN = 11
    CMD_READ_DATA = 0x05
    CMD_RESPONSE = 0x85

    def __init__(self, tx_pin=1, rx_pin=2, uart_id=1, baudrate=9600):
        self.uart = UART(uart_id, baudrate=baudrate, tx=tx_pin, rx=rx_pin,
                         timeout=0, timeout_char=0)
        self.rx_buf = bytearray()
        self.waiting = False
        self.sent_at = 0
        self.channel = 1

    @staticmethod
    def checksum(data):
        return sum(data) & 0xFF

    def start_read(self, channel=1):
        if self.waiting:
            return False
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        frame = bytearray((self.FRAME_HEAD, 7, self.CMD_READ_DATA,
                           channel, 0, 0, 0, 0))
        frame[-1] = self.checksum(frame[:-1])
        self.rx_buf = bytearray()
        self.uart.write(frame)
        self.channel = channel
        self.sent_at = time.ticks_ms()
        self.waiting = True
        return True

    def poll(self):
        """返回一帧有效数据、None（等待）或 False（超时/错误帧）。"""
        if not self.waiting:
            return None
        if time.ticks_diff(time.ticks_ms(), self.sent_at) > 1200:
            self.waiting = False
            return False
        data = self.uart.read()
        if data:
            self.rx_buf.extend(data)
        if len(self.rx_buf) < self.RESPONSE_LEN:
            return None
        start = self.rx_buf.find(b"\x55")
        if start < 0:
            self.rx_buf = bytearray()
            return False
        if start:
            del self.rx_buf[:start]
        if len(self.rx_buf) < self.RESPONSE_LEN:
            return None
        frame = bytes(self.rx_buf[:self.RESPONSE_LEN])
        del self.rx_buf[:self.RESPONSE_LEN]
        if self.checksum(frame[:-1]) != frame[-1] or frame[2] != self.CMD_RESPONSE:
            return False
        if frame[3] != self.channel:
            return False
        self.waiting = False
        conductivity = ((frame[4] << 8) | frame[5]) / 10.0
        raw_temp = (frame[6] << 8) | frame[7]
        if raw_temp > 32767:
            raw_temp -= 65536
        temperature = raw_temp / 10.0
        return {
            "conductivity": conductivity,
            "water_temp": temperature,
            "tds": conductivity / 2.0,
        }

    def cancel(self):
        self.waiting = False
        self.rx_buf = bytearray()
