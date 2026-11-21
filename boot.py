"""上电最早建立安全输出，避免配置加载前功率器件误动作。"""
import gc
from machine import Pin

# LED1~4、双泵、加热、背光均为高电平有效驱动。
for _pin in (4, 5, 12, 13, 14, 21, 7, 17):
    Pin(_pin, Pin.OUT, value=0)

gc.collect()
