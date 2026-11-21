"""喂鱼倒计时；状态仅驻留内存，重启不续行。"""
import time


class FeedingController:
    def __init__(self, duration=600):
        self.duration = self._duration(duration)
        self.active = False
        self.end_at = 0
        self.saved_pumps = None

    @staticmethod
    def _duration(value):
        value = int(value)
        if not 10 <= value <= 3600:
            raise ValueError("feeding duration must be 10..3600 seconds")
        return value

    def set_duration(self, value):
        self.duration = self._duration(value)

    def start(self, duration, pumps):
        if self.active:
            return False
        self.duration = self._duration(duration)
        self.saved_pumps = {}
        for name, pump in pumps.items():
            self.saved_pumps[name] = {
                "mode": pump["mode"], "target": pump["target"],
                "auto_speed": pump["auto_speed"],
            }
        self.active = True
        self.end_at = time.ticks_add(time.ticks_ms(), self.duration * 1000)
        return True

    def cancel(self):
        if not self.active:
            return None
        saved = self.saved_pumps
        self.active = False
        self.saved_pumps = None
        return saved

    def update(self):
        if self.active and time.ticks_diff(time.ticks_ms(), self.end_at) >= 0:
            return self.cancel()
        return None

    def snapshot(self):
        remaining = max(0, time.ticks_diff(self.end_at, time.ticks_ms()) // 1000) if self.active else 0
        return {"active": self.active, "duration": self.duration,
                "remaining": remaining, "pumps_locked": self.active}
