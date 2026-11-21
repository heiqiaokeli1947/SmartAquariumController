"""DS3231 主时钟、系统时间和非阻塞 SNTP 校准。"""
import socket
import struct
import time
import machine

from driver.ds3231 import DS3231


class TimeService:
    NTP_HOST = "ntp.aliyun.com"
    NTP_DELTA = 2208988800

    def __init__(self, ds3231, timezone=8):
        self.ds3231 = ds3231
        self.timezone = timezone
        self.rtc_valid = False
        self.time_valid = False
        self.source = "invalid"
        self._socket = None
        self._ntp_started = 0
        self._last_attempt = 0
        self._last_success_tick = 0
        self.last_sync = None

    def initialize(self):
        try:
            value = self.ds3231.datetime()
            if self.ds3231.valid(value):
                weekday = max(0, min(6, value[3] - 1))
                machine.RTC().datetime((value[0], value[1], value[2], weekday,
                                        value[4], value[5], value[6], 0))
                self.rtc_valid = True
                self.time_valid = True
                self.source = "ds3231"
                return True
        except (OSError, ValueError) as exc:
            print("DS3231 read:", exc)
        try:
            value = machine.RTC().datetime()
            if DS3231.valid(value):
                self.time_valid = True
                self.source = "system"
        except (OSError, ValueError):
            pass
        return False

    def set_datetime(self, year, month, day, hour, minute, second):
        """写入本地时间；DS3231 失败时仍保留本次启动内的系统时间。"""
        if not DS3231.valid((year, month, day, 1, hour, minute, second, 0)):
            raise ValueError("invalid local datetime")
        probe = (year, month, day, hour, minute, second, 0, 0)
        weekday = time.localtime(time.mktime(probe))[6]
        machine.RTC().datetime((year, month, day, weekday, hour, minute, second, 0))
        self.time_valid = True
        self.source = "manual"
        try:
            self.ds3231.datetime((year, month, day, weekday + 1,
                                  hour, minute, second, 0))
            self.rtc_valid = True
        except (OSError, ValueError) as exc:
            self.rtc_valid = False
            print("DS3231 write:", exc)
        return self.rtc_valid

    def set_utc_seconds(self, seconds):
        local = time.gmtime(seconds + self.timezone * 3600)
        self.set_datetime(local[0], local[1], local[2], local[3], local[4], local[5])
        self.source = "ntp"
        self.last_sync = "%04d-%02d-%02d %02d:%02d:%02d" % local[:6]

    def _close_ntp(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None

    def start_ntp(self):
        if self._socket is not None:
            return False
        self._last_attempt = time.ticks_ms()
        try:
            address = socket.getaddrinfo(self.NTP_HOST, 123)[0][-1]
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0)
            sock.sendto(b"\x1b" + bytes(47), address)
            self._socket = sock
            self._ntp_started = time.ticks_ms()
            return True
        except (OSError, IndexError) as exc:
            print("NTP start:", exc)
            self._close_ntp()
            return False

    def poll_ntp(self):
        if self._socket is None:
            return False
        try:
            data = self._socket.recv(48)
            if len(data) == 48:
                seconds = struct.unpack("!I", data[40:44])[0]
                if seconds > self.NTP_DELTA:
                    self.set_utc_seconds(seconds - self.NTP_DELTA)
                    self._last_success_tick = time.ticks_ms()
                    self._close_ntp()
                    return True
            self._close_ntp()
        except OSError:
            if time.ticks_diff(time.ticks_ms(), self._ntp_started) > 4000:
                self._close_ntp()
        return False

    def update(self, wifi_connected):
        if self.poll_ntp():
            return True
        if not wifi_connected or self._socket is not None:
            return False
        now = time.ticks_ms()
        interval = 21600000 if self._last_success_tick else 60000
        if self._last_attempt == 0 or time.ticks_diff(now, self._last_attempt) >= interval:
            self.start_ntp()
        return False

    def minutes_of_day(self):
        if not self.time_valid:
            return None
        value = time.localtime()
        return value[3] * 60 + value[4]

    def snapshot(self):
        if self.time_valid:
            value = time.localtime()
            date = "%04d-%02d-%02d" % value[:3]
            clock = "%02d:%02d:%02d" % value[3:6]
        else:
            date = "----/--/--"
            clock = "--:--:--"
        return {
            "valid": self.time_valid, "rtc_valid": self.rtc_valid,
            "source": self.source, "date": date, "time": clock,
            "ntp_syncing": self._socket is not None, "last_sync": self.last_sync,
            "timezone": self.timezone,
        }
