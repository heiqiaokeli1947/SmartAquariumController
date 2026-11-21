"""四路 LED 日光节律、校准和平滑输出。"""


class LightController:
    def __init__(self, mode, targets, schedule, curves):
        self.mode = mode if mode in ("auto", "manual") else "auto"
        self.targets = self._targets(targets)
        self.actual = [0.0, 0.0, 0.0, 0.0]
        self.schedule = schedule
        self.curves = curves
        self.phase = "unknown"
        self.fade_step = 1.0

    @staticmethod
    def _targets(values):
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            raise ValueError("four LED targets required")
        return [max(0.0, min(100.0, float(value))) for value in values]

    def set(self, mode=None, targets=None):
        if mode is not None:
            if mode not in ("auto", "manual"):
                raise ValueError("invalid light mode")
            self.mode = mode
        if targets is not None:
            self.targets = self._targets(targets)

    def set_schedule(self, schedule):
        if not isinstance(schedule, list) or len(schedule) != 5:
            raise ValueError("five daylight stages required")
        self.schedule = schedule

    @staticmethod
    def _interpolate(start, end, ratio):
        return start + (end - start) * ratio

    def _daylight(self, minutes):
        stages = sorted(self.schedule, key=lambda item: item["start"])
        for index, stage in enumerate(stages):
            following = stages[(index + 1) % len(stages)]
            end = following["start"]
            if index == len(stages) - 1:
                end += 1440
            point = minutes
            if index == len(stages) - 1 and point < stage["start"]:
                point += 1440
            if stage["start"] <= point < end:
                span = max(1, end - stage["start"])
                ratio = (point - stage["start"]) / span
                values = [self._interpolate(stage["targets"][ch], following["targets"][ch], ratio)
                          for ch in range(4)]
                self.phase = stage["name"]
                return values
        self.phase = "unknown"
        return list(self.targets)

    def _calibrated(self, channel, value):
        if value <= 0:
            return 0.0
        curve = self.curves[channel]
        position = max(0.0, min(100.0, value)) / 10.0
        index = min(9, int(position))
        fraction = position - index
        return self._interpolate(curve[index], curve[index + 1], fraction)

    def update(self, minutes_of_day):
        if self.mode == "auto" and minutes_of_day is not None:
            self.targets = self._daylight(minutes_of_day)
        elif self.mode == "auto":
            self.phase = "time_invalid"
        for channel in range(4):
            difference = self.targets[channel] - self.actual[channel]
            if abs(difference) <= self.fade_step:
                self.actual[channel] = self.targets[channel]
            else:
                self.actual[channel] += self.fade_step if difference > 0 else -self.fade_step
        return [self._calibrated(channel, self.actual[channel]) for channel in range(4)]

    def snapshot(self):
        return {
            "mode": self.mode, "targets": [round(value, 1) for value in self.targets],
            "actual": [round(value, 1) for value in self.actual], "phase": self.phase,
            "schedule": self.schedule,
        }
