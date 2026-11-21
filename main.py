"""SmartAquariumController 启动与主循环。

LVGL task_handler 以 ~50 Hz 驱动渲染/触摸轮询；控制器逻辑保持 10 Hz。
"""
import gc
import os
import sys
import time
import machine

from domain.controller import Controller
from domain.db import Database
from driver.bsp import BSP


def main():
    bsp = BSP(init_display=False)
    try:
        database = Database()
        controller = Controller(bsp, database)

        ui = None
        if bsp.init_display():
            try:
                from ui.lvgl_app import LVGLApp
                ui = LVGLApp(controller, bsp)
                ui.start()
                bsp.set_output("backlight", float(controller.system.get("backlight", 70)))
            except MemoryError:
                # 软复位会保留 LVGL 状态，旧显示回调已失效，只能硬复位恢复。
                print("LVGL stale after soft reset, hard reset")
                machine.reset()
            except Exception as exc:
                print("LVGL disabled:", exc)
                sys.print_exception(exc)
                ui = None

        controller.start_services()
        try:
            from web.server import WebServer
            WebServer(controller).start()
        except Exception as exc:
            print("Web disabled:", exc)
            sys.print_exception(exc)

        try:
            watchdog = machine.WDT(timeout=10000)
        except (OSError, ValueError):
            watchdog = None
        # LVGL task_handler 必须高频调用才能驱动渲染与触摸轮询（~50 Hz），
        # 但控制器逻辑保持 100 ms 节奏，避免控制循环过载。
        TICK_MS = 20
        CTRL_MS = 100
        now = time.ticks_ms()
        next_tick = now
        next_ctrl = now
        gc.collect()
        # --- 性能诊断：记录主循环各阶段最大耗时，每 5 秒打印 DIAG 行 ---
        _diag = {"ml": 0, "mt": 0, "mc": 0, "n": 0,
                 "at": time.ticks_add(time.ticks_ms(), 5000)}
        _shot_check = time.ticks_ms()
        while True:
            _loop_t0 = time.ticks_ms()
            if ui is not None:
                try:
                    _tt0 = time.ticks_ms()
                    ui.tick()
                    _td = time.ticks_diff(time.ticks_ms(), _tt0)
                    if _td > _diag["mt"]:
                        _diag["mt"] = _td
                    # 外部触发的截图请求：每 500ms 用 os.stat 探测一次。注意：本板
                    # littlefs 的 os.listdir("/") 遍历耗时 ~110ms，绝不能每轮调用，
                    # 否则主循环被锁死在 ~5Hz，滑动/触摸全部饿死。
                    _now = time.ticks_ms()
                    if time.ticks_diff(_now, _shot_check) >= 500:
                        _shot_check = _now
                        try:
                            os.stat("/screenshot_request")
                        except OSError:
                            pass
                        else:
                            try:
                                os.remove("/screenshot_request")
                                try:
                                    n = ui.take_screenshot("/screenshot.raw")
                                    with open("/screenshot_done", "w") as h:
                                        h.write(str(n))
                                except Exception as exc:
                                    with open("/screenshot_done", "w") as h:
                                        h.write("ERR " + str(exc))
                            except Exception:
                                pass
                except Exception as exc:
                    # 显示异常只停用界面，不影响加热、水泵等底层控制。
                    print("LVGL runtime disabled:", exc)
                    sys.print_exception(exc)
                    ui = None
            now = time.ticks_ms()
            if time.ticks_diff(next_ctrl, now) <= 0:
                _ct0 = time.ticks_ms()
                controller.step()
                _cd = time.ticks_diff(time.ticks_ms(), _ct0)
                if _cd > _diag["mc"]:
                    _diag["mc"] = _cd
                next_ctrl = time.ticks_add(next_ctrl, CTRL_MS)
            if watchdog is not None:
                watchdog.feed()
            _loop_ms = time.ticks_diff(time.ticks_ms(), _loop_t0)
            _diag["n"] += 1
            if _loop_ms > _diag["ml"]:
                _diag["ml"] = _loop_ms
            if time.ticks_diff(time.ticks_ms(), _diag["at"]) >= 0:
                _th = _rf = _fl = _fc = _idv = 0
                if ui is not None:
                    _th = ui._th_max
                    _rf = ui._rf_max
                    _fl = ui._flush_max
                    _fc = ui._fc_diag
                    _idv = ui._indev_max
                    ui._th_max = ui._rf_max = ui._flush_max = 0
                    ui._fc_diag = 0
                    ui._indev_max = 0
                print("DIAG max_loop=%d max_tick=%d max_ctrl=%d th=%d rf=%d fl=%d fc=%d idv=%d n=%d free=%d"
                      % (_diag["ml"], _diag["mt"], _diag["mc"],
                         _th, _rf, _fl, _fc, _idv, _diag["n"], gc.mem_free()))
                _diag["ml"] = _diag["mt"] = _diag["mc"] = 0
                _diag["n"] = 0
                _diag["at"] = time.ticks_add(time.ticks_ms(), 5000)
            next_tick = time.ticks_add(next_tick, TICK_MS)
            delay = time.ticks_diff(next_tick, time.ticks_ms())
            if delay > 0:
                time.sleep_ms(delay)
            if delay < -1000:
                # 严重滞后（长时间 GC/渲染卡顿）时重置基准，避免补偿式狂奔。
                next_tick = time.ticks_ms()
                next_ctrl = time.ticks_ms()
    except Exception:
        bsp.safe_outputs()
        raise


main()
