"""水泵与气泵独立自动/手动调速，喂鱼时强制关闭。"""


class PumpController:
    def __init__(self, config, limits):
        self.channels = {}
        for name in ("water_pump", "air_pump"):
            self.channels[name] = {
                "mode": config[name + "_mode"],
                "target": float(config[name + "_target"]),
                "auto_speed": float(config[name + "_auto"]),
                "actual": 0.0,
                "max_speed": float(limits[name + "_max"]),
            }
        self.feeding_active = False
        self.fade_step = 1.0

    def set(self, name, mode=None, target=None, auto_speed=None):
        if self.feeding_active:
            return False
        if name not in self.channels:
            raise ValueError("unknown pump")
        pump = self.channels[name]
        if mode is not None:
            if mode not in ("auto", "manual"):
                raise ValueError("invalid pump mode")
            pump["mode"] = mode
        if target is not None:
            pump["target"] = max(0.0, min(100.0, float(target)))
        if auto_speed is not None:
            pump["auto_speed"] = max(0.0, min(100.0, float(auto_speed)))
        return True

    def restore(self, saved):
        for name, state in saved.items():
            if name in self.channels:
                self.channels[name]["mode"] = state["mode"]
                self.channels[name]["target"] = float(state["target"])
                if "auto_speed" in state:
                    self.channels[name]["auto_speed"] = float(state["auto_speed"])

    def update(self, feeding_active):
        self.feeding_active = feeding_active
        outputs = {}
        for name, pump in self.channels.items():
            if feeding_active:
                pump["actual"] = 0.0
                outputs[name] = 0.0
                continue
            if pump["mode"] == "auto":
                desired = pump["auto_speed"]
            else:
                desired = pump["target"]
            desired = min(desired, pump["max_speed"])
            difference = desired - pump["actual"]
            if abs(difference) <= self.fade_step:
                pump["actual"] = desired
            else:
                pump["actual"] += self.fade_step if difference > 0 else -self.fade_step
            outputs[name] = pump["actual"]
        return outputs

    def snapshot(self):
        return dict((name, {
            "mode": pump["mode"], "target": round(pump["target"], 1),
            "auto_speed": round(pump["auto_speed"], 1), "actual": round(pump["actual"], 1),
            "max_speed": round(pump["max_speed"], 1),
        }) for name, pump in self.channels.items())
