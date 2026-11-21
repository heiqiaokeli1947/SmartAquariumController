"""ESP32-S3 480x320 LVGL 六页本地界面。"""
import time

import machine
import lvgl as lv


class LVGLApp:
    """只读取控制快照并提交命令，不直接操作业务硬件。"""

    WIDTH = 480
    HEIGHT = 320
    # 整屏缓冲（300KB，PSRAM 充足）：整屏失效/滑动时只 flush 1 次，
    # 比分 3 块（128 行）减少 SPI 事务开销，拖动更跟手。
    BUFFER_LINES = 320
    REFRESH_MS = 300
    HOME = 0
    PAGE_NAMES = ("概览", "灯光", "设备", "喂食", "水质", "设置")
    STAGE_NAMES = ("night", "sunrise", "day", "sunset", "night_end")
    # 仅用于下拉显示，与 STAGE_NAMES 索引一一对应；内部配置键仍用 STAGE_NAMES
    STAGE_LABELS = ("夜晚", "日出", "白天", "日落", "夜末")
    AUTH_NAMES = ("auto", "OPEN", "WEP", "WPA", "WPA2", "WPA/WPA2", "WPA2/WPA3")
    AUTH_LABELS = ("自动", "开放", "WEP", "WPA", "WPA2", "WPA/WPA2", "WPA2/WPA3")
    # 控制器原始状态值 → 中文显示映射；未收录的值回退为原文，绝不崩溃
    LEVEL_LABELS = {"normal": "正常", "ok": "正常", "low": "偏低",
                    "high": "偏高", "empty": "缺水", "invalid": "无效"}
    MODE_LABELS = {"auto": "自动", "manual": "手动"}
    PHASE_LABELS = {"unknown": "未知", "time_invalid": "时间无效",
                    "night": "夜晚", "sunrise": "日出", "day": "白天",
                    "sunset": "日落", "night_end": "夜末"}
    PROTECTION_LABELS = {"starting": "启动中", "water_debouncing": "水稳中",
                         "water_low": "缺水", "water_invalid": "水无效",
                         "sensor_invalid": "传感器异常", "overtemperature": "超温"}
    WIFI_STATE_LABELS = {"offline": "离线", "connected": "已连接",
                         "connecting": "连接中", "disconnected": "已断开"}
    CLOCK_SOURCE_LABELS = {"invalid": "无效", "ds3231": "DS3231",
                           "system": "系统", "manual": "手动", "ntp": "NTP"}

    # ---- 设计令牌（浅色清爽主题）----
    C_BG = 0xF4F7FB
    C_CARD = 0xFFFFFF
    C_CARD_ALT = 0xEDF2F8
    C_PRIMARY = 0x0EA5A4
    C_ACCENT = 0x0EA5E9
    C_INK = 0x0F1F2E
    C_DIM = 0x475569
    C_MUTE = 0x94A3B8
    C_BORDER = 0xE2E8F0
    C_TRACK = 0xE6ECF3
    C_OK = 0x059669
    C_WARN = 0xD97706
    C_DANGER = 0xDC2626
    C_INFO = 0x2563EB
    C_WHITE = 0xFFFFFF

    def __init__(self, controller, bsp):
        self.controller = controller
        self.bsp = bsp
        self.display = None
        self.indev = None
        self.buffer = None
        self.screen = None
        self.tileview = None
        self.tiles = []
        self.current_page = self.HOME
        self.last_local_touch = time.ticks_ms()
        self._last_refresh = self.last_local_touch
        self.updating = False
        self._started = False
        self._shot_buf = None
        self._shot_active = False
        self._flush_count = 0
        self._flush_max = 0
        self._th_max = 0
        self._rf_max = 0
        self._fc_diag = 0
        self._indev_max = 0
        self._touch_down = False
        self._state = {}
        self._modal = None
        self.keyboard = None
        self._schedule = None
        self._schedule_index = 0
        self._feeding_duration = 600
        self._wifi_dirty = False
        self._rtc_dirty = False
        self._chip_state = {}
        self._label_colors = {}

        self.page_label = None
        self.top_label = None
        self.notice_label = None
        self.home_labels = {}
        self.light_mode = None
        self.light_phase = None
        self.light_sliders = []
        self.stage_select = None
        self.stage_start = None
        self.stage_sliders = []
        self.pump_controls = {}
        self.heater_controls = {}
        self.feeding_status = None
        self.feeding_lock = None
        self.feeding_duration = None
        self.water_labels = {}
        self.settings_controls = {}
        self.wifi_status = None
        self.rtc_status = None
        self.wifi_ssid = None
        self.wifi_password = None
        self.wifi_auth = None
        self.rtc_input = None

    # ---------- 基础控件与本地操作 ----------

    def _mark_activity(self):
        """仅由本地触摸和本地控件事件调用。"""
        self.last_local_touch = time.ticks_ms()

    def _notice(self, text):
        if self.notice_label is not None:
            self.notice_label.set_text(str(text))

    def _submit(self, command):
        """唯一命令出口；提交后由主循环异步执行。"""
        try:
            self.controller.submit(command, source="lvgl")
            self._notice("Queued: " + command["type"])
        except Exception as exc:
            self._notice("Submit failed: " + str(exc))

    def _hex(self, value):
        return lv.color_hex(value)

    def _label(self, parent, text="", width=None, color=None, font=None):
        label = lv.label(parent)
        label.set_text(str(text))
        if width is not None:
            label.set_width(width)
        label.set_style_text_color(self._hex(color if color is not None else self.C_INK), 0)
        # 默认用含中文的 song_16，避免中文字符渲染成方框；
        # 需要等宽数字/单位的地方显式传入 montserrat_14 覆盖。
        label.set_style_text_font(font if font is not None else lv.font_song_16, 0)
        return label

    def _row(self, parent, height=50):
        row = lv.obj(parent)
        row.set_size(lv.pct(100), height)
        row.set_style_border_width(0, 0)
        row.set_style_bg_opa(lv.OPA.TRANSP, 0)
        row.set_style_pad_all(2, 0)
        row.set_style_pad_column(6, 0)
        row.set_flex_flow(lv.FLEX_FLOW.ROW)
        row.set_flex_align(lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.CENTER,
                           lv.FLEX_ALIGN.CENTER)
        row.remove_flag(lv.obj.FLAG.SCROLLABLE)
        return row

    def _text_row(self, parent, text, height=34):
        row = self._row(parent, height)
        label = self._label(row, text, lv.pct(100), color=self.C_DIM)
        label.set_long_mode(lv.label.LONG_MODE.WRAP)
        return label

    def _card(self, parent, w, h, radius=12, color=None):
        card = lv.obj(parent)
        card.set_size(w, h)
        card.set_style_radius(radius, 0)
        card.set_style_bg_color(self._hex(color if color is not None else self.C_CARD), 0)
        card.set_style_bg_opa(lv.OPA.COVER, 0)
        card.set_style_border_width(1, 0)
        card.set_style_border_color(self._hex(self.C_BORDER), 0)
        card.set_style_pad_all(10, 0)
        card.set_style_pad_row(4, 0)
        card.set_style_pad_column(6, 0)
        card.remove_flag(lv.obj.FLAG.SCROLLABLE)
        card.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        return card

    def _section(self, parent, title, color=None):
        """全宽、高度自适应的分区卡片，首行是分区标题。"""
        card = self._card(parent, lv.pct(100), 10)
        card.set_style_pad_all(8, 0)
        card.set_style_pad_row(6, 0)
        self._label(card, title, color=color if color is not None else self.C_DIM,
                    font=lv.font_song_16)
        try:
            card.set_height(lv.SIZE_CONTENT)
        except Exception:
            card.set_height(260)
        return card

    def _chip(self, parent, text, bg=None, fg=None, width=None):
        label = lv.label(parent)
        label.set_text(str(text))
        bg = bg if bg is not None else self.C_CARD_ALT
        fg = fg if fg is not None else self.C_DIM
        label.set_style_bg_color(self._hex(bg), 0)
        label.set_style_bg_opa(lv.OPA.COVER, 0)
        label.set_style_text_color(self._hex(fg), 0)
        label.set_style_text_font(lv.font_song_16, 0)
        label.set_style_radius(8, 0)
        label.set_style_border_width(0, 0)
        label.set_style_pad_left(8, 0)
        label.set_style_pad_right(8, 0)
        label.set_style_pad_top(2, 0)
        label.set_style_pad_bottom(2, 0)
        if width is not None:
            label.set_width(width)
        return label

    def _bar_meter(self, parent, w, h, color, value=0, lo=0, hi=100):
        bar = lv.bar(parent)
        bar.set_size(w, h)
        bar.set_range(lo, hi)
        bar.set_value(int(value), 0)
        bar.set_style_bg_color(self._hex(self.C_TRACK), lv.PART.MAIN)
        bar.set_style_bg_color(self._hex(color), lv.PART.INDICATOR)
        r = h // 2 if h > 1 else 0
        bar.set_style_radius(r, lv.PART.MAIN)
        bar.set_style_radius(r, lv.PART.INDICATOR)
        bar.set_style_border_width(0, lv.PART.MAIN)
        return bar

    def _arc_ring(self, parent, size, color, value=0, lo=0, hi=100):
        arc = lv.arc(parent)
        arc.set_size(size, size)
        arc.set_angles(135, 45)
        arc.set_range(lo, hi)
        arc.set_value(int(value))
        arc.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
        arc.set_style_arc_color(self._hex(self.C_TRACK), lv.PART.MAIN)
        arc.set_style_arc_color(self._hex(color), lv.PART.INDICATOR)
        arc.set_style_arc_width(16, lv.PART.MAIN)
        arc.set_style_arc_width(16, lv.PART.INDICATOR)
        try:
            arc.set_style_arc_rounded(True, lv.PART.MAIN)
            arc.set_style_arc_rounded(True, lv.PART.INDICATOR)
        except Exception:
            pass
        return arc

    def _metric(self, parent, w, h, title, color, unit="", digits=1,
                lo=0, hi=100, with_bar=True):
        card = self._card(parent, w, h)
        card.set_style_pad_all(9, 0)
        card.set_flex_align(lv.FLEX_ALIGN.SPACE_BETWEEN,
                            lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.START)
        cap = self._label(card, title, color=self.C_MUTE, font=lv.font_song_16)
        mid = lv.obj(card)
        mid.set_size(lv.pct(100), 34)
        mid.set_style_bg_opa(lv.OPA.TRANSP, 0)
        mid.set_style_border_width(0, 0)
        mid.set_style_pad_all(0, 0)
        mid.set_style_pad_column(3, 0)
        mid.remove_flag(lv.obj.FLAG.SCROLLABLE)
        mid.set_flex_flow(lv.FLEX_FLOW.ROW)
        mid.set_flex_align(lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.END,
                           lv.FLEX_ALIGN.END)
        value = self._label(mid, "--", color=self.C_INK, font=lv.font_song_24)
        unit_lbl = self._label(mid, unit, color=self.C_MUTE,
                               font=lv.font_montserrat_14)
        bar = None
        if with_bar:
            bar = self._bar_meter(card, w - 18, 8, color, 0, 0, 100)
        return {"card": card, "value": value, "unit": unit_lbl, "bar": bar,
                "lo": lo, "hi": hi, "digits": digits, "color": color,
                "with_bar": with_bar}

    def _set_metric(self, m, value, text=None, unit=None):
        if value is None:
            if m["value"].get_text() != "--":
                m["value"].set_text("--")
            if m["bar"] is not None and m["bar"].get_value() != 0:
                m["bar"].set_value(0, 0)
            return
        if text is None:
            if m["digits"] <= 0:
                text = "%d" % int(round(float(value)))
            else:
                text = "%.*f" % (m["digits"], float(value))
        if m["value"].get_text() != text:
            m["value"].set_text(text)
        if unit is not None and m["unit"].get_text() != unit:
            m["unit"].set_text(unit)
        if m["bar"] is not None:
            try:
                span = m["hi"] - m["lo"]
                pct = 0 if span == 0 else (float(value) - m["lo"]) / span * 100
                pct = max(0, min(100, pct))
                if m["bar"].get_value() != int(pct):
                    m["bar"].set_value(int(pct), 0)
            except Exception:
                pass

    def _set_chip(self, chip, text, bg=None, fg=None):
        key = id(chip)
        last = self._chip_state.get(key)
        if last is None or last[0] != text:
            chip.set_text(text)
        if bg is not None and (last is None or last[1] != bg):
            chip.set_style_bg_color(self._hex(bg), 0)
        if fg is not None and (last is None or last[2] != fg):
            chip.set_style_text_color(self._hex(fg), 0)
        self._chip_state[key] = (text, bg, fg)

    def _set_label_color(self, label, color):
        key = id(label)
        if self._label_colors.get(key) != color:
            label.set_style_text_color(self._hex(color), 0)
            self._label_colors[key] = color

    def _button(self, parent, text, callback, width=112, height=42,
                filled=True, color=None):
        button = lv.button(parent)
        button.set_size(width, height)
        c = color if color is not None else self.C_PRIMARY
        if filled:
            button.set_style_bg_color(self._hex(c), 0)
            label_color = self.C_WHITE
        else:
            button.set_style_bg_color(self._hex(self.C_CARD_ALT), 0)
            label_color = c
        button.set_style_bg_opa(lv.OPA.COVER, 0)
        button.set_style_radius(10, 0)
        button.set_style_border_width(0, 0)
        label = self._label(button, text, color=label_color, font=lv.font_song_16)
        label.center()

        def clicked(event, callback=callback):
            if self.updating:
                return
            self._mark_activity()
            callback()

        button.add_event_cb(clicked, lv.EVENT.CLICKED, None)
        return button

    def _dropdown(self, parent, options, callback, width=145):
        dropdown = lv.dropdown(parent)
        dropdown.set_width(width)
        dropdown.set_options("\n".join(options))
        dropdown.set_style_bg_color(self._hex(self.C_CARD), 0)
        dropdown.set_style_text_color(self._hex(self.C_INK), 0)
        dropdown.set_style_border_color(self._hex(self.C_BORDER), 0)
        dropdown.set_style_radius(8, 0)
        dropdown.set_style_text_font(lv.font_song_16, 0)
        # 默认箭头 SYMBOL.DOWN(U+F078) 不在 song 字体里会渲染成方框，
        # 改用 song_16 含有的 U+2228 下尖号作为下拉指示。
        dropdown.set_symbol("\u2228")
        try:
            dropdown.get_list().set_style_text_font(lv.font_song_16, 0)
        except Exception:
            pass

        def changed(event, dropdown=dropdown, callback=callback):
            if self.updating:
                return
            self._mark_activity()
            callback(dropdown.get_selected())

        dropdown.add_event_cb(changed, lv.EVENT.VALUE_CHANGED, None)
        return dropdown

    def _switch(self, parent, callback):
        switch = lv.switch(parent)
        try:
            sel = lv.PART.INDICATOR | lv.STATE.CHECKED
            switch.set_style_bg_color(self._hex(self.C_PRIMARY), sel)
        except Exception:
            pass

        def changed(event, switch=switch, callback=callback):
            if self.updating:
                return
            self._mark_activity()
            callback(switch.has_state(lv.STATE.CHECKED))

        switch.add_event_cb(changed, lv.EVENT.VALUE_CHANGED, None)
        return switch

    def _slider(self, parent, title, minimum, maximum, callback,
                unit="%", scale=1, width=220, color=None):
        row = self._row(parent)
        self._label(row, title, 100, color=self.C_DIM, font=lv.font_song_16)
        slider = lv.slider(row)
        slider.set_width(width)
        slider.set_range(minimum, maximum)
        c = color if color is not None else self.C_PRIMARY
        slider.set_style_bg_color(self._hex(self.C_TRACK), lv.PART.MAIN)
        slider.set_style_bg_color(self._hex(c), lv.PART.INDICATOR)
        slider.set_style_bg_color(self._hex(c), lv.PART.KNOB)
        value_label = self._label(row, "--", 78, color=self.C_INK,
                                  font=lv.font_song_16)
        control = {"slider": slider, "label": value_label,
                   "unit": unit, "scale": scale}

        def changed(event, control=control):
            if self.updating:
                return
            self._mark_activity()
            raw = control["slider"].get_value()
            control["label"].set_text(self._slider_text(control, raw))

        def released(event, control=control, callback=callback):
            if self.updating:
                return
            self._mark_activity()
            raw = control["slider"].get_value()
            control["label"].set_text(self._slider_text(control, raw))
            callback(raw)

        slider.add_event_cb(changed, lv.EVENT.VALUE_CHANGED, None)
        # 拖动过程只更新显示，松手才提交并写数据库。
        slider.add_event_cb(released, lv.EVENT.RELEASED, None)
        return control

    @staticmethod
    def _slider_text(control, raw):
        if control["scale"] == 1:
            return "%d%s" % (raw, control["unit"])
        return "%.1f%s" % (raw / control["scale"], control["unit"])

    def _set_slider(self, control, value):
        if value is None:
            if control["label"].get_text() != "--":
                control["label"].set_text("--")
            return
        raw = int(round(float(value) * control["scale"]))
        # 拖动期间不用旧快照覆盖用户手势；值未变则不重复设置，避免失效与动画。
        if not control["slider"].has_state(lv.STATE.PRESSED):
            if control["slider"].get_value() != raw:
                control["slider"].set_value(raw, 0)
            text = self._slider_text(control, raw)
            if control["label"].get_text() != text:
                control["label"].set_text(text)

    def _set_dropdown(self, dropdown, selected):
        if not dropdown.has_state(lv.STATE.PRESSED) and dropdown.get_selected() != selected:
            dropdown.set_selected(selected)

    def _set_switch(self, switch, checked):
        if switch.has_state(lv.STATE.PRESSED):
            return
        is_checked = switch.has_state(lv.STATE.CHECKED)
        if checked and not is_checked:
            switch.add_state(lv.STATE.CHECKED)
        elif not checked and is_checked:
            switch.remove_state(lv.STATE.CHECKED)

    def _set_text(self, textarea, text):
        if not textarea.has_state(lv.STATE.FOCUSED) and textarea.get_text() != text:
            textarea.set_text(text)

    def _textarea(self, parent, text="", width=260, max_length=64,
                  password=False, on_edit=None):
        textarea = lv.textarea(parent)
        textarea.set_size(width, 40)
        textarea.set_one_line(True)
        textarea.set_max_length(max_length)
        textarea.set_text(text)
        textarea.set_style_text_font(lv.font_song_16, 0)
        if password:
            textarea.set_password_mode(True)

        def show(event, textarea=textarea):
            if self.updating:
                return
            self._mark_activity()
            if self.keyboard is not None:
                self.keyboard.set_textarea(textarea)
                self.keyboard.remove_flag(lv.obj.FLAG.HIDDEN)

        def edited(event, on_edit=on_edit):
            if self.updating:
                return
            self._mark_activity()
            if on_edit is not None:
                on_edit()

        textarea.add_event_cb(show, lv.EVENT.FOCUSED, None)
        textarea.add_event_cb(show, lv.EVENT.CLICKED, None)
        textarea.add_event_cb(edited, lv.EVENT.VALUE_CHANGED, None)
        return textarea

    # ---------- 启动、显示和页面框架 ----------

    def start(self):
        if self._started:
            return
        # 固件里的 LVGL 是常驻模块，REPL 重跑时不能重复 init。
        if not lv.is_initialized():
            lv.init()
        # 推进 LVGL 内部时钟：渲染、触摸与动画定时器都依赖它。
        # 本固件必须用 lv.task_handler 驱动渲染，而 task_handler 只有在 tick
        # 前进时才会触发 flush；缺这个定时器会整屏黑、触摸无响应。
        if not getattr(self, "_tick_timer", None):
            self._tick_timer = machine.Timer(-1)
            self._tick_timer.init(
                period=5, mode=machine.Timer.PERIODIC,
                callback=lambda t: lv.tick_inc(5))
        if self.bsp.display is None and not self.bsp.init_display():
            raise RuntimeError("display unavailable: " + str(self.bsp.display_error))
        if self.bsp.display is None or self.bsp.touch is None:
            raise RuntimeError("display or touch unavailable")

        self.display = lv.display_create(self.WIDTH, self.HEIGHT)
        # ST7796 接收大端 RGB565，由 LVGL 渲染阶段完成字节交换。
        self.display.set_color_format(lv.COLOR_FORMAT.RGB565_SWAPPED)
        self.display.set_flush_cb(self._flush)
        # 单缓冲局部刷新，第二缓冲必须为 None。
        self.buffer = bytearray(self.WIDTH * self.BUFFER_LINES * 2)
        self.display.set_buffers(self.buffer, None, len(self.buffer),
                                 lv.DISPLAY_RENDER_MODE.PARTIAL)

        self.indev = lv.indev_create()
        self.indev.set_type(lv.INDEV_TYPE.POINTER)
        self.indev.set_display(self.display)
        self.indev.set_read_cb(self._read_touch)

        self.screen = lv.screen_active()
        self.screen.set_style_bg_color(self._hex(self.C_BG), 0)
        self.screen.remove_flag(lv.obj.FLAG.SCROLLABLE)
        # 屏幕根级默认文本字体设为含中文的 song_16，级联兜底；
        # 等宽数字/单位处用 montserrat_14 显式覆盖。
        self.screen.set_style_text_font(lv.font_song_16, 0)
        self._build_ui()
        now = time.ticks_ms()
        self.last_local_touch = now
        self._last_refresh = time.ticks_add(now, -self.REFRESH_MS)
        self._started = True

    def _read_touch(self, indev, data):
        # 驱动回调不做二次坐标旋转，480x320 与触摸坐标一致。
        _i0 = time.ticks_ms()
        self.bsp.touch.read(indev, data)
        _id = time.ticks_diff(time.ticks_ms(), _i0)
        if _id > self._indev_max:
            self._indev_max = _id
        self._touch_down = bool(data.state)
        if data.state:
            self._mark_activity()

    def _build_ui(self):
        top = lv.obj(self.screen)
        top.set_size(self.WIDTH, 32)
        top.set_pos(0, 0)
        top.set_style_bg_color(self._hex(self.C_CARD), 0)
        top.set_style_bg_opa(lv.OPA.COVER, 0)
        top.set_style_border_side(lv.BORDER_SIDE.BOTTOM, 0)
        top.set_style_border_width(1, 0)
        top.set_style_border_color(self._hex(self.C_BORDER), 0)
        top.set_style_radius(0, 0)
        top.set_style_pad_all(0, 0)
        top.remove_flag(lv.obj.FLAG.SCROLLABLE)
        self.page_label = self._label(top, self.PAGE_NAMES[0], color=self.C_INK,
                                      font=lv.font_song_16)
        self.page_label.set_pos(10, 5)
        self.top_label = self._label(top, "--:--:--  WiFi:--", color=self.C_DIM,
                                     font=lv.font_montserrat_14)
        self.top_label.set_pos(248, 7)
        # 页指示点
        self.page_dots = []
        for i in range(6):
            d = lv.obj(top)
            d.set_size(8, 8)
            d.set_pos(11 + i * 14, 22)
            d.set_style_radius(4, 0)
            d.set_style_border_width(0, 0)
            d.set_style_bg_opa(lv.OPA.COVER, 0)
            d.set_style_bg_color(self._hex(self.C_PRIMARY if i == 0 else self.C_MUTE), 0)
            d.remove_flag(lv.obj.FLAG.SCROLLABLE)
            self.page_dots.append(d)

        self.tileview = lv.tileview(self.screen)
        self.tileview.set_size(self.WIDTH, 270)
        self.tileview.set_pos(0, 32)
        self.tileview.set_style_bg_color(self._hex(self.C_BG), 0)
        self.tileview.add_event_cb(self._tile_changed, lv.EVENT.VALUE_CHANGED, None)
        for index in range(6):
            direction = 0
            if index < 5:
                direction |= lv.DIR.RIGHT
            if index > 0:
                direction |= lv.DIR.LEFT
            tile = self.tileview.add_tile(index, 0, direction)
            tile.set_flex_flow(lv.FLEX_FLOW.COLUMN)
            tile.set_flex_align(lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.CENTER,
                                lv.FLEX_ALIGN.CENTER)
            tile.set_style_pad_all(6, 0)
            tile.set_style_pad_row(5, 0)
            tile.set_style_bg_color(self._hex(self.C_BG), 0)
            tile.set_style_border_width(0, 0)
            self.tiles.append(tile)

        self._build_overview(self.tiles[0])
        self._build_light(self.tiles[1])
        self._build_devices(self.tiles[2])
        self._build_feeding(self.tiles[3])
        self._build_water(self.tiles[4])
        self._build_settings(self.tiles[5])

        self.notice_label = self._label(
            self.screen, "Swipe horizontally. Commands run in background.",
            472, color=self.C_MUTE, font=lv.font_montserrat_14)
        self.notice_label.set_pos(4, 304)

        self.keyboard = lv.keyboard(self.screen)
        self.keyboard.set_size(self.WIDTH, 160)
        self.keyboard.align(lv.ALIGN.BOTTOM_MID, 0, 0)
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)
        self.keyboard.add_event_cb(self._keyboard_done, lv.EVENT.READY, None)
        self.keyboard.add_event_cb(self._keyboard_done, lv.EVENT.CANCEL, None)

    def _keyboard_done(self, event):
        self._mark_activity()
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)
        self.keyboard.set_textarea(None)

    def _refresh_dots(self):
        name = self.PAGE_NAMES[self.current_page]
        if self.page_label is not None and self.page_label.get_text() != name:
            self.page_label.set_text(name)
        for i, d in enumerate(self.page_dots):
            d.set_style_bg_color(
                self._hex(self.C_PRIMARY if i == self.current_page else self.C_MUTE), 0)

    def _tile_changed(self, event):
        active = self.tileview.get_tile_active()
        for index, tile in enumerate(self.tiles):
            if tile == active:
                self.current_page = index
                break
        # 切到新页后立即刷新，避免新页内容最长陈旧 300ms。
        self._last_refresh = time.ticks_add(time.ticks_ms(), -self.REFRESH_MS)
        self._refresh_dots()
        if not self.updating:
            self._mark_activity()

    def _set_page(self, index):
        previous = self.updating
        self.updating = True
        try:
            self.current_page = index
            self.tileview.set_tile(self.tiles[index], 0)
            self._refresh_dots()
        finally:
            self.updating = previous

    # ---------- 六页内容 ----------

    def _build_overview(self, page):
        ov = {}
        # 第一行：四张指标卡
        row1 = self._row(page, 120)
        w = (468 - 4 - 3 * 6) // 4
        ov["temp"] = self._metric(row1, w, 120, "水温", self.C_INFO, "°C", 1, 20, 32)
        ov["tds"] = self._metric(row1, w, 120, "TDS", self.C_ACCENT, "ppm", 0, 0, 1000)
        ov["env"] = self._metric(row1, w, 120, "环境", self.C_OK, "°C", 1, 10, 40)
        ov["level"] = self._metric(row1, w, 120, "水位", self.C_PRIMARY, "", 0, 0, 100)
        # 第二行：光照卡 + 喂食卡
        row2 = self._row(page, 96)
        lc = self._card(row2, 294, 96)
        lc.set_style_pad_all(9, 0)
        lc.set_flex_align(lv.FLEX_ALIGN.SPACE_BETWEEN,
                          lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.START)
        self._label(lc, "光照", color=self.C_MUTE, font=lv.font_song_16)
        lbody = lv.obj(lc)
        lbody.set_size(lv.pct(100), 62)
        lbody.set_style_bg_opa(lv.OPA.TRANSP, 0)
        lbody.set_style_border_width(0, 0)
        lbody.set_style_pad_all(0, 0)
        lbody.set_style_pad_column(6, 0)
        lbody.remove_flag(lv.obj.FLAG.SCROLLABLE)
        lbody.set_flex_flow(lv.FLEX_FLOW.ROW)
        lbody.set_flex_align(lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.END,
                             lv.FLEX_ALIGN.END)
        ov["light_total"] = self._label(lbody, "--", color=self.C_INK,
                                        font=lv.font_song_24)
        self._label(lbody, "%", color=self.C_MUTE, font=lv.font_montserrat_14)
        lbars = lv.obj(lbody)
        lbars.set_size(150, 62)
        lbars.set_style_bg_opa(lv.OPA.TRANSP, 0)
        lbars.set_style_border_width(0, 0)
        lbars.set_style_pad_all(0, 0)
        lbars.set_style_pad_row(4, 0)
        lbars.remove_flag(lv.obj.FLAG.SCROLLABLE)
        lbars.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        ch_colors = [0x0EA5E9, 0xF59E0B, 0x8B5CF6, 0xEC4899]
        ov["light_bars"] = []
        for i in range(4):
            ov["light_bars"].append(self._bar_meter(lbars, 150, 8, ch_colors[i], 0, 0, 100))
        fc = self._card(row2, 164, 96)
        fc.set_style_pad_all(9, 0)
        fc.set_flex_align(lv.FLEX_ALIGN.SPACE_BETWEEN,
                          lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.START)
        self._label(fc, "喂食", color=self.C_MUTE, font=lv.font_song_16)
        self._button(fc, "立即喂食", self._show_feeding_confirm, 146, 54)
        ov["feed_chip"] = self._chip(fc, "待机")
        # 第三行：状态药丸（构建默认中文，刷新时覆盖为实时值）
        row3 = self._row(page, 30)
        chip_defaults = {"pump": "水泵 --", "air": "气泵 --",
                         "heater": "加热 --", "feed": "喂食 --",
                         "alarm": "告警 --"}
        for key, txt in chip_defaults.items():
            ov["chip_" + key] = self._chip(row3, txt)
        self.overview = ov

    def _build_light(self, page):
        ch_colors = [self.C_ACCENT, self.C_WARN, 0x8B5CF6, 0xEC4899]
        sec = self._section(page, "实时灯光")
        head = self._row(sec, 40)
        self._label(head, "模式", 60, color=self.C_DIM, font=lv.font_song_16)
        self.light_mode = self._dropdown(head, ("自动", "手动"), self._set_light_mode, 120)
        self.light_phase = self._chip(head, "阶段 --")
        for channel in range(4):
            control = self._slider(
                sec, "通道 %d" % (channel + 1), 0, 100,
                lambda raw, channel=channel: self._set_light_target(channel, raw),
                "%", 1, 230, ch_colors[channel])
            self.light_sliders.append(control)

        sec2 = self._section(page, "日光计划")
        head2 = self._row(sec2, 40)
        self.stage_select = self._dropdown(head2, self.STAGE_LABELS,
                                           self._select_schedule_stage, 180)
        self._button(head2, "保存计划", self._save_schedule, 120)
        row = self._row(sec2, 44)
        self._label(row, "开始时间", 80, color=self.C_DIM, font=lv.font_song_16)
        self.stage_start = self._textarea(row, "00:00", 120, 5)
        for channel in range(4):
            control = self._slider(
                sec2, "计划 %d" % (channel + 1), 0, 100,
                lambda raw, channel=channel: self._edit_schedule_target(channel, raw),
                "%", 1, 230, ch_colors[channel])
            self.stage_sliders.append(control)

    def _build_devices(self, page):
        for name, title in (("water_pump", "水泵"), ("air_pump", "气泵")):
            sec = self._section(page, title)
            head = self._row(sec, 40)
            mode = self._dropdown(
                head, ("自动", "手动"),
                lambda selected, name=name: self._set_pump_mode(name, selected), 120)
            actual = self._chip(head, "实际 --")
            target = self._slider(sec, "目标转速", 0, 100,
                                  lambda raw, name=name: self._set_pump_target(name, raw))
            auto = self._slider(sec, "自动转速", 0, 100,
                                lambda raw, name=name: self._set_pump_auto(name, raw))
            self.pump_controls[name] = {"mode": mode, "actual": actual,
                                        "target": target, "auto": auto}

        sec = self._section(page, "加热棒")
        head = self._row(sec, 40)
        heater_mode = self._dropdown(head, ("自动", "手动"), self._set_heater_mode, 120)
        heater_actual = self._chip(head, "实际 --")
        target_temp = self._slider(sec, "目标温度", 50, 400,
                                   self._set_heater_target, "°C", 10)
        manual_output = self._slider(sec, "手动功率", 0, 100, self._set_heater_manual)
        hysteresis = self._slider(sec, "温度回差", 1, 50,
                                  self._set_heater_hysteresis, "°C", 10)
        max_output = self._slider(sec, "最大功率", 0, 100, self._set_heater_max_output)
        protection = self._chip(sec, "保护 --")
        self.heater_controls = {
            "mode": heater_mode, "actual": heater_actual,
            "target_temp": target_temp, "manual_output": manual_output,
            "hysteresis": hysteresis, "max_output": max_output,
            "protection": protection,
        }

    def _build_feeding(self, page):
        sec = self._section(page, "喂食控制")
        self.feeding_status = self._label(sec, "喂食待机", color=self.C_INK,
                                          font=lv.font_song_24)
        self.feeding_lock = self._chip(sec, "水泵锁定: 否")
        self.feeding_duration = self._slider(sec, "喂食时长", 10, 3600,
                                             self._set_feeding_duration, "秒")
        row = self._row(sec, 52)
        self._button(row, "开始喂食", self._show_feeding_confirm, 200, 48)
        self._button(row, "取消喂食", self._cancel_feeding, 140, 48,
                     filled=False, color=self.C_DANGER)
        self._text_row(sec, "开始需确认；灯光与加热继续运行。", 40)

    def _build_water(self, page):
        w = {}
        row1 = self._row(page, 120)
        cw = (468 - 4 - 18) // 4
        w["tds"] = self._metric(row1, cw, 120, "TDS", self.C_ACCENT, "ppm", 0, 0, 1000)
        w["cond"] = self._metric(row1, cw, 120, "电导率", self.C_PRIMARY, "uS", 0, 0, 2000)
        w["temp"] = self._metric(row1, cw, 120, "水温", self.C_INFO, "°C", 1, 20, 32)
        w["env"] = self._metric(row1, cw, 120, "环境", self.C_OK, "°C", 1, 10, 40)

        sec = self._section(page, "水位")
        head = self._row(sec, 44)
        w["level_label"] = self._label(head, "--", color=self.C_INK,
                                       font=lv.font_song_24)
        w["level_chip"] = self._chip(head, "--")
        for name, label, color in (("high", "高位", self.C_ACCENT),
                                   ("low", "低位", self.C_WARN)):
            br = self._row(sec, 36)
            self._label(br, label, 50, color=self.C_DIM, font=lv.font_song_16)
            w[name + "_bar"] = self._bar_meter(br, 240, 10, color, 0, 0, 4095)
            w[name + "_val"] = self._label(br, "--", 110, color=self.C_INK,
                                           font=lv.font_montserrat_14)

        sec2 = self._section(page, "传感器健康")
        r = self._row(sec2, 32)
        for key in ("tds_v", "water_v", "env_v", "hum_v"):
            w[key] = self._chip(r, "--")
        r2 = self._row(sec2, 32)
        w["err_tds"] = self._chip(r2, "TDS 错误 --")
        w["err_hdc"] = self._chip(r2, "HDC 错误 --")
        self.water_ui = w

    def _build_settings(self, page):
        sec = self._section(page, "连接状态")
        self.wifi_status = self._text_row(sec, "WiFi: --", 44)
        self.rtc_status = self._text_row(sec, "RTC: --", 52)

        sec2 = self._section(page, "通用设置")
        row = self._row(sec2, 40)
        self._label(row, "断电恢复", 150, color=self.C_DIM, font=lv.font_song_16)
        self.settings_controls["power_resume"] = self._switch(row, self._set_power_resume)
        self.settings_controls["backlight"] = self._slider(
            sec2, "背光亮度", 0, 100, self._set_backlight)
        self.settings_controls["home_timeout"] = self._slider(
            sec2, "主页超时", 5, 300, self._set_home_timeout, "秒")
        self.settings_controls["feeding_duration"] = self._slider(
            sec2, "喂食时长", 10, 3600, self._set_feeding_duration, "秒")
        self.settings_controls["timezone"] = self._slider(
            sec2, "时区", -12, 14, self._set_timezone, "")

        sec3 = self._section(page, "WiFi")
        row = self._row(sec3, 44)
        self._label(row, "SSID", 60, color=self.C_DIM, font=lv.font_song_16)
        self.wifi_ssid = self._textarea(row, "", 340, 32, on_edit=self._mark_wifi_dirty)
        row = self._row(sec3, 44)
        self._label(row, "密码", 60, color=self.C_DIM, font=lv.font_song_16)
        self.wifi_password = self._textarea(row, "", 340, 64, True,
                                            on_edit=self._mark_wifi_dirty)
        row = self._row(sec3, 44)
        self._label(row, "加密", 60, color=self.C_DIM, font=lv.font_song_16)
        self.wifi_auth = self._dropdown(row, self.AUTH_LABELS, self._select_wifi_auth, 180)
        self._button(row, "保存 WiFi", self._save_wifi, 130)

        sec4 = self._section(page, "时间")
        row = self._row(sec4, 44)
        self.rtc_input = self._textarea(row, "2026-01-01 00:00:00", 300, 19,
                                        on_edit=self._mark_rtc_dirty)
        self._button(row, "设置 RTC", self._save_rtc, 130)
        self._text_row(sec4, "格式: YYYY-MM-DD HH:MM:SS (本地时间)", 40)

    # ---------- 灯光和计划编辑命令 ----------

    def _set_light_mode(self, selected):
        self._submit({"type": "set_light", "mode": "auto" if selected == 0 else "manual"})

    def _set_light_target(self, channel, raw):
        # 调整亮度即进入手动，避免自动日光在下一周期覆盖用户目标。
        self._submit({"type": "set_light", "mode": "manual",
                      "channel": channel + 1, "value": raw})

    @staticmethod
    def _time_text(value):
        if isinstance(value, str):
            return value
        value = int(value)
        return "%02d:%02d" % (value // 60, value % 60)

    @staticmethod
    def _parse_minutes(text):
        parts = text.strip().split(":")
        if len(parts) != 2:
            raise ValueError("time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("invalid time")
        return hour * 60 + minute

    def _schedule_from_state(self):
        configured = self._state.get("config", {}).get("schedule")
        if configured is None:
            configured = self._state.get("light", {}).get("schedule", [])
        defaults = (0, 300, 720, 1020, 1439)
        schedule = []
        for index, name in enumerate(self.STAGE_NAMES):
            source = configured[index] if index < len(configured) else {}
            targets = list(source.get("targets", [0, 0, 0, 0]))
            schedule.append({"name": name,
                             "start": source.get("start", defaults[index]),
                             "targets": targets})
        return schedule

    def _ensure_schedule(self):
        if self._schedule is None:
            self._schedule = self._schedule_from_state()
            self._schedule_index = 0
            self._load_schedule_stage()

    def _load_schedule_stage(self):
        if self._schedule is None or self.stage_start is None:
            return
        stage = self._schedule[self._schedule_index]
        previous = self.updating
        self.updating = True
        try:
            self.stage_select.set_selected(self._schedule_index)
            self.stage_start.set_text(self._time_text(stage["start"]))
            for channel, control in enumerate(self.stage_sliders):
                self._set_slider(control, stage["targets"][channel])
        finally:
            self.updating = previous

    def _capture_schedule_start(self):
        if self._schedule is None:
            return
        start = self._parse_minutes(self.stage_start.get_text())
        self._schedule[self._schedule_index]["start"] = start

    def _select_schedule_stage(self, selected):
        self._ensure_schedule()
        try:
            self._capture_schedule_start()
        except ValueError as exc:
            self._notice("Plan start: " + str(exc))
        self._schedule_index = selected
        self._load_schedule_stage()

    def _edit_schedule_target(self, channel, raw):
        self._ensure_schedule()
        self._schedule[self._schedule_index]["targets"][channel] = raw

    def _save_schedule(self):
        self._ensure_schedule()
        try:
            self._capture_schedule_start()
            schedule = []
            for stage in self._schedule:
                schedule.append({"name": stage["name"], "start": stage["start"],
                                 "targets": list(stage["targets"])})
        except ValueError as exc:
            self._notice("Plan not submitted: " + str(exc))
            return
        self._submit({"type": "set_config", "values": {"schedule": schedule}})

    # ---------- 双泵、加热和喂鱼命令 ----------

    def _set_pump_mode(self, name, selected):
        self._submit({"type": "set_pump", "pump": name,
                      "mode": "auto" if selected == 0 else "manual"})

    def _set_pump_target(self, name, raw):
        self._submit({"type": "set_pump", "pump": name, "target": raw})

    def _set_pump_auto(self, name, raw):
        self._submit({"type": "set_pump", "pump": name, "auto_speed": raw})

    def _set_heater_mode(self, selected):
        self._submit({"type": "set_heater", "mode": "auto" if selected == 0 else "manual"})

    def _set_heater_target(self, raw):
        self._submit({"type": "set_heater", "target_temp": raw / 10.0})

    def _set_heater_manual(self, raw):
        self._submit({"type": "set_heater", "manual_output": raw})

    def _set_heater_hysteresis(self, raw):
        self._submit({"type": "set_heater", "hysteresis": raw / 10.0})

    def _set_heater_max_output(self, raw):
        self._submit({"type": "set_heater", "max_output": raw})

    def _set_feeding_duration(self, raw):
        self._feeding_duration = raw
        self._submit({"type": "set_config", "values": {"feeding_duration": raw}})

    def _show_feeding_confirm(self):
        if self.keyboard is not None:
            self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)
        self._close_modal()
        modal = lv.obj(self.screen)
        self._modal = modal
        modal.set_size(self.WIDTH, self.HEIGHT)
        modal.set_pos(0, 0)
        modal.set_style_bg_color(lv.color_hex(0x202830), 0)
        modal.set_style_border_width(0, 0)
        modal.remove_flag(lv.obj.FLAG.SCROLLABLE)
        panel = lv.obj(modal)
        panel.set_size(380, 190)
        panel.center()
        panel.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        panel.set_flex_align(lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER,
                             lv.FLEX_ALIGN.CENTER)
        panel.set_style_pad_row(12, 0)
        self._label(panel, "开始喂食 %d 秒？" % self._feeding_duration)
        self._label(panel, "水泵和气泵将锁定关闭。")
        row = lv.obj(panel)
        row.set_size(330, 58)
        row.set_style_border_width(0, 0)
        row.set_flex_flow(lv.FLEX_FLOW.ROW)
        row.set_flex_align(lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER,
                           lv.FLEX_ALIGN.CENTER)
        row.set_style_pad_column(20, 0)
        row.remove_flag(lv.obj.FLAG.SCROLLABLE)
        self._button(row, "取消", self._close_modal, 130)
        self._button(row, "确认", self._confirm_feeding, 130)

    def _close_modal(self):
        if self._modal is not None:
            self._modal.delete()
            self._modal = None

    def _confirm_feeding(self):
        duration = self._feeding_duration
        self._close_modal()
        self._submit({"type": "start_feeding", "duration": duration})

    def _cancel_feeding(self):
        self._submit({"type": "cancel_feeding"})

    # ---------- 设置、WiFi 和 RTC 命令 ----------

    def _set_power_resume(self, checked):
        self._submit({"type": "set_config", "values": {"power_resume": checked}})

    def _set_backlight(self, raw):
        self._submit({"type": "set_config", "values": {"backlight": raw}})

    def _set_home_timeout(self, raw):
        self._submit({"type": "set_config", "values": {"home_timeout": raw}})

    def _set_timezone(self, raw):
        self._submit({"type": "set_config", "values": {"timezone": raw}})

    def _mark_wifi_dirty(self):
        self._wifi_dirty = True

    def _select_wifi_auth(self, selected):
        self._wifi_dirty = True

    def _save_wifi(self):
        selected = self.wifi_auth.get_selected()
        auth = self.AUTH_NAMES[selected] if 0 <= selected < len(self.AUTH_NAMES) else "auto"
        ssid = self.wifi_ssid.get_text().strip()
        password = self.wifi_password.get_text()
        self._wifi_dirty = False
        self._submit({"type": "wifi_config", "ssid": ssid,
                      "password": password, "auth": auth})

    def _mark_rtc_dirty(self):
        self._rtc_dirty = True

    def _save_rtc(self):
        try:
            parts = self.rtc_input.get_text().strip().replace("T", " ").split()
            if len(parts) != 2:
                raise ValueError("use YYYY-MM-DD HH:MM:SS")
            date = [int(value) for value in parts[0].split("-")]
            clock = [int(value) for value in parts[1].split(":")]
            if len(date) != 3 or len(clock) != 3:
                raise ValueError("use YYYY-MM-DD HH:MM:SS")
            values = date + clock
        except ValueError as exc:
            self._notice("RTC not submitted: " + str(exc))
            return
        self._rtc_dirty = False
        self._submit({"type": "set_time", "datetime": values})

    # ---------- 周期刷新 ----------

    @staticmethod
    def _value(value, unit="", digits=1):
        if value is None:
            return "--"
        try:
            pattern = "%." + str(digits) + "f%s"
            return pattern % (float(value), unit)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _cn(table, value, default=None):
        """状态值查中文映射；None 用 default（默认 --），未收录回退原文。"""
        if value is None:
            return default if default is not None else "--"
        return table.get(value, default if default is not None else str(value))

    @staticmethod
    def _bool_cn(value, true_txt="开", false_txt="关", none_txt="--"):
        if value is None:
            return none_txt
        return true_txt if value else false_txt

    def _refresh(self, state):
        previous = self.updating
        self.updating = True
        try:
            self._refresh_top(state)
            # 只刷新当前可见页：隐藏页的 label/slider 一旦 set_text/set_value 就会
            # 触发失效与布局重算，六页全刷会让 task_handler 每 300ms 卡顿 ~90ms。
            page = self.current_page
            if page == 0:
                self._refresh_overview(state)
            elif page == 1:
                self._refresh_light(state)
            elif page == 2:
                self._refresh_devices(state)
            elif page == 3:
                self._refresh_feeding(state)
            elif page == 4:
                self._refresh_water(state)
            else:
                self._refresh_settings(state)
        finally:
            self.updating = previous

    def _refresh_top(self, state):
        clock = state.get("time", {})
        wifi = state.get("wifi", {})
        rtc = "RTC:OK" if clock.get("rtc_valid") else "RTC:--"
        if wifi.get("connected"):
            wifi_text = wifi.get("ip") or "online"
        else:
            wifi_text = wifi.get("state", "offline")
        alarms = state.get("alarms", [])
        alarm_text = " AL:%d" % len(alarms) if alarms else ""
        self.top_label.set_text("%s %s WiFi:%s%s" %
                                (clock.get("time", "--:--:--"), rtc, wifi_text, alarm_text))

    def _refresh_overview(self, state):
        ov = self.overview
        sensors = state.get("sensors", {})
        water = state.get("water", {})
        light = state.get("light", {})
        pumps = state.get("pumps", {})
        heater = state.get("heater", {})
        feeding = state.get("feeding", {})

        self._set_metric(ov["temp"], sensors.get("water_temp"))
        self._set_metric(ov["tds"], sensors.get("tds"))
        env_temp = sensors.get("env_temp")
        if env_temp is None:
            self._set_metric(ov["env"], None)
        else:
            hum = sensors.get("humidity")
            unit = "°C  %s%%" % self._value(hum, "", 0) if hum is not None else "°C"
            self._set_metric(ov["env"], env_temp, unit=unit)
        # 水位：文字 + 颜色随状态变化，仅在变化时重设颜色避免持续失效
        level = water.get("level")
        if level != ov.get("_last_level"):
            ov["_last_level"] = level
            if level:
                mapping = {"normal": (100, self.C_OK), "ok": (100, self.C_OK),
                           "low": (35, self.C_WARN), "high": (80, self.C_WARN),
                           "empty": (10, self.C_DANGER)}
                pct, color = mapping.get(level, (60, self.C_MUTE))
                self._set_metric(ov["level"], pct, text=self._cn(self.LEVEL_LABELS, level))
                ov["level"]["bar"].set_style_bg_color(self._hex(color), lv.PART.INDICATOR)
            else:
                self._set_metric(ov["level"], None)
        # 光照：总亮度 + 4 通道条
        actual = light.get("actual", [])
        if actual:
            total = sum(actual) // len(actual)
            if ov["light_total"].get_text() != str(total):
                ov["light_total"].set_text(str(total))
            for i, bar in enumerate(ov["light_bars"]):
                v = int(actual[i]) if i < len(actual) else 0
                if bar.get_value() != v:
                    bar.set_value(v, 0)
        else:
            if ov["light_total"].get_text() != "--":
                ov["light_total"].set_text("--")
            for bar in ov["light_bars"]:
                if bar.get_value() != 0:
                    bar.set_value(0, 0)
        # 状态药丸
        wp = pumps.get("water_pump", {})
        ap = pumps.get("air_pump", {})
        w_run = (wp.get("actual") or 0) > 0
        a_run = (ap.get("actual") or 0) > 0
        self._set_chip(ov["chip_pump"], "水泵 %s" % self._value(wp.get("actual"), "%", 0),
                       self.C_OK if w_run else self.C_CARD_ALT,
                       self.C_WHITE if w_run else self.C_DIM)
        self._set_chip(ov["chip_air"], "气泵 %s" % self._value(ap.get("actual"), "%", 0),
                       self.C_OK if a_run else self.C_CARD_ALT,
                       self.C_WHITE if a_run else self.C_DIM)
        heating = bool(heater.get("heat_enabled"))
        self._set_chip(ov["chip_heater"], "加热 %s" % self._cn(self.MODE_LABELS, heater.get("mode")),
                       self.C_OK if heating else self.C_CARD_ALT,
                       self.C_WHITE if heating else self.C_DIM)
        if feeding.get("active"):
            rem = int(feeding.get("remaining", 0))
            txt = "喂食 %02d:%02d" % (rem // 60, rem % 60)
            self._set_chip(ov["chip_feed"], txt, self.C_WARN, self.C_WHITE)
            self._set_chip(ov["feed_chip"], "喂食中", self.C_WARN, self.C_WHITE)
        else:
            self._set_chip(ov["chip_feed"], "喂食 待机", self.C_CARD_ALT, self.C_DIM)
            self._set_chip(ov["feed_chip"], "待机", self.C_CARD_ALT, self.C_DIM)
        alarms = state.get("alarms", [])
        if alarms:
            self._set_chip(ov["chip_alarm"], "告警 %d" % len(alarms),
                           self.C_DANGER, self.C_WHITE)
        else:
            self._set_chip(ov["chip_alarm"], "告警 0", self.C_CARD_ALT, self.C_DIM)

    def _refresh_light(self, state):
        light = state.get("light", {})
        self._set_dropdown(self.light_mode, 0 if light.get("mode", "auto") == "auto" else 1)
        self.light_phase.set_text("阶段: " + self._cn(self.PHASE_LABELS, light.get("phase")))
        targets = light.get("targets", [])
        for channel, control in enumerate(self.light_sliders):
            self._set_slider(control, targets[channel] if channel < len(targets) else None)
        if self._schedule is None:
            self._ensure_schedule()

    def _refresh_devices(self, state):
        pumps = state.get("pumps", {})
        for name, controls in self.pump_controls.items():
            pump = pumps.get(name, {})
            self._set_dropdown(controls["mode"], 0 if pump.get("mode", "auto") == "auto" else 1)
            controls["actual"].set_text(
                "实际 %s  上限 %s" %
                (self._value(pump.get("actual"), " %"),
                 self._value(pump.get("max_speed"), " %", 0)))
            self._set_slider(controls["target"], pump.get("target"))
            self._set_slider(controls["auto"], pump.get("auto_speed"))

        heater = state.get("heater", {})
        controls = self.heater_controls
        self._set_dropdown(controls["mode"], 0 if heater.get("mode", "auto") == "auto" else 1)
        controls["actual"].set_text("实际 " + self._value(heater.get("actual"), " %"))
        self._set_slider(controls["target_temp"], heater.get("target_temp"))
        self._set_slider(controls["manual_output"], heater.get("manual_output"))
        self._set_slider(controls["hysteresis"], heater.get("hysteresis"))
        self._set_slider(controls["max_output"], heater.get("max_output"))
        controls["protection"].set_text(
            "保护 %s  加热 %s  上限 %s" %
            (self._cn(self.PROTECTION_LABELS, heater.get("protection"), "无"),
             self._bool_cn(heater.get("heat_enabled")),
             self._value(heater.get("max_temp"), " °C")))

    def _refresh_feeding(self, state):
        feeding = state.get("feeding", {})
        config = state.get("config", {})
        duration = config.get("feeding_duration", feeding.get("duration", 600))
        self._feeding_duration = int(duration)
        self._set_slider(self.feeding_duration, duration)
        self._set_slider(self.settings_controls["feeding_duration"], duration)
        if feeding.get("active"):
            remaining = int(feeding.get("remaining", 0))
            self.feeding_status.set_text("喂食中  剩余 %02d:%02d" %
                                         (remaining // 60, remaining % 60))
        else:
            self.feeding_status.set_text("待机  预设 %s 秒" % duration)
        self.feeding_lock.set_text("水泵锁定: %s" %
                                   ("是" if feeding.get("pumps_locked") else "否"))

    def _refresh_water(self, state):
        w = self.water_ui
        sensors = state.get("sensors", {})
        water = state.get("water", {})

        self._set_metric(w["tds"], sensors.get("tds"))
        self._set_metric(w["cond"], sensors.get("conductivity"))
        self._set_metric(w["temp"], sensors.get("water_temp"))
        env_temp = sensors.get("env_temp")
        if env_temp is None:
            self._set_metric(w["env"], None)
        else:
            hum = sensors.get("humidity")
            unit = "°C  %s%%" % self._value(hum, "", 0) if hum is not None else "°C"
            self._set_metric(w["env"], env_temp, unit=unit)

        level = water.get("level")
        if level != w.get("_last_level"):
            w["_last_level"] = level
            if level:
                mapping = {"normal": (self.C_OK, "正常"), "ok": (self.C_OK, "正常"),
                           "low": (self.C_WARN, "偏低"), "high": (self.C_WARN, "偏高"),
                           "empty": (self.C_DANGER, "缺水")}
                color, name = mapping.get(level, (self.C_MUTE, str(level)))
                if w["level_label"].get_text() != name:
                    w["level_label"].set_text(name)
                w["level_label"].set_style_text_color(self._hex(color), 0)
                self._set_chip(w["level_chip"],
                               "有效" if water.get("valid") else "无效",
                               self.C_OK if water.get("valid") else self.C_MUTE,
                               self.C_WHITE if water.get("valid") else self.C_DIM)
            else:
                if w["level_label"].get_text() != "--":
                    w["level_label"].set_text("--")
        hr = water.get("high_raw")
        if hr is not None and w["high_bar"].get_value() != int(hr):
            w["high_bar"].set_value(int(hr), 0)
        ht = self._value(hr, "", 0)
        if w["high_val"].get_text() != ht:
            w["high_val"].set_text(ht)
        lr = water.get("low_raw")
        if lr is not None and w["low_bar"].get_value() != int(lr):
            w["low_bar"].set_value(int(lr), 0)
        lt = self._value(lr, "", 0)
        if w["low_val"].get_text() != lt:
            w["low_val"].set_text(lt)

        def valid_chip(chip, ok, name):
            if ok:
                self._set_chip(chip, name + " 正常", self.C_OK, self.C_WHITE)
            else:
                self._set_chip(chip, name + " 失效", self.C_DANGER, self.C_WHITE)
        valid_chip(w["tds_v"], sensors.get("tds_valid", False), "TDS")
        valid_chip(w["water_v"], sensors.get("water_temp_valid", False), "水温")
        valid_chip(w["env_v"], sensors.get("env_temp_valid", False), "环境")
        valid_chip(w["hum_v"], sensors.get("humidity_valid", False), "湿度")
        errors = sensors.get("errors", {})
        et = errors.get("tds")
        self._set_chip(w["err_tds"], "TDS: " + (str(et) if et else "正常"),
                       self.C_CARD_ALT if not et else self.C_DANGER,
                       self.C_DIM if not et else self.C_WHITE)
        eh = errors.get("hdc1080")
        self._set_chip(w["err_hdc"], "HDC: " + (str(eh) if eh else "正常"),
                       self.C_CARD_ALT if not eh else self.C_DANGER,
                       self.C_DIM if not eh else self.C_WHITE)

    def _refresh_settings(self, state):
        config = state.get("config", {})
        wifi = state.get("wifi", {})
        clock = state.get("time", {})
        controls = self.settings_controls
        self._set_switch(controls["power_resume"], config.get("power_resume", True))
        self._set_slider(controls["backlight"], config.get("backlight", 70))
        self._set_slider(controls["home_timeout"], config.get("home_timeout", 30))
        self._set_slider(controls["timezone"], config.get("timezone", 8))

        wifi_txt = ("WiFi: %s  SSID: %s  IP: %s  AP: %s (%s)" %
                    (self._cn(self.WIFI_STATE_LABELS, wifi.get("state")),
                     wifi.get("ssid") or "--",
                     wifi.get("ip") or "--", wifi.get("ap_ssid") or "关闭",
                     wifi.get("ap_ip") or "--"))
        if self.wifi_status.get_text() != wifi_txt:
            self.wifi_status.set_text(wifi_txt)
        wc = self.C_OK if wifi.get("connected") else self.C_DANGER
        self._set_label_color(self.wifi_status, wc)
        rtc_txt = ("RTC: %s  %s %s  源:%s  同步:%s" %
                   (self._bool_cn(clock.get("rtc_valid"), "正常", "失效"),
                    clock.get("date", "--"),
                    clock.get("time", "--"),
                    self._cn(self.CLOCK_SOURCE_LABELS, clock.get("source")),
                    clock.get("last_sync") or "--"))
        if self.rtc_status.get_text() != rtc_txt:
            self.rtc_status.set_text(rtc_txt)
        rc = self.C_OK if clock.get("rtc_valid") else self.C_WARN
        self._set_label_color(self.rtc_status, rc)
        if not self._wifi_dirty:
            self._set_text(self.wifi_ssid, config.get("wifi_ssid", wifi.get("ssid", "")))
            auth = config.get("wifi_auth", wifi.get("auth", "auto"))
            selected = self.AUTH_NAMES.index(auth) if auth in self.AUTH_NAMES else 0
            self._set_dropdown(self.wifi_auth, selected)
        if not self._rtc_dirty:
            self._set_text(self.rtc_input, "%s %s" %
                           (clock.get("date", "----/--/--"), clock.get("time", "--:--:--")))

    def _reset_drafts(self):
        """自动回主页时丢弃未提交草稿，不触碰已运行任务。"""
        self._schedule = None
        self._wifi_dirty = False
        self._rtc_dirty = False
        previous = self.updating
        self.updating = True
        try:
            self.wifi_password.set_text("")
            self._refresh_settings(self._state)
        finally:
            self.updating = previous

    def _auto_home(self, now, state):
        # 主页不启动计时；Web 快照和 Web 命令不会更新 last_local_touch。
        if self.current_page == self.HOME:
            return
        try:
            timeout = int(state.get("config", {}).get("home_timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        if time.ticks_diff(now, self.last_local_touch) < timeout * 1000:
            return
        self._close_modal()
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)
        self.keyboard.set_textarea(None)
        self._set_page(self.HOME)
        self._reset_drafts()
        self._notice("Auto home: unsubmitted edits discarded; background tasks unchanged.")

    def _flush(self, disp, area, color_p):
        """LVGL flush 回调：先把画面推到屏幕，截图模式下同步复制到全屏缓冲。"""
        _f0 = time.ticks_ms()
        self._flush_count += 1
        self._fc_diag += 1
        self.bsp.display.flush(disp, area, color_p)
        if self._shot_active and self._shot_buf is not None:
            try:
                x1, y1, x2, y2 = area.x1, area.y1, area.x2, area.y2
                w = x2 - x1 + 1
                h = y2 - y1 + 1
                if w <= 0 or h <= 0:
                    return
                data = color_p.__dereference__(w * h * 2)
                mv = memoryview(self._shot_buf)
                width = self.WIDTH
                for r in range(h):
                    dst = ((y1 + r) * width + x1) * 2
                    src = r * w * 2
                    mv[dst:dst + w * 2] = data[src:src + w * 2]
            except Exception as exc:
                print("shot copy:", exc)
        _fd = time.ticks_diff(time.ticks_ms(), _f0)
        if _fd > self._flush_max:
            self._flush_max = _fd

    def take_screenshot(self, path):
        """触发一次整屏截图并写盘（RGB565_SWAPPED 原始字节）。

        不切换 display 缓冲（运行时切换会在活跃渲染下 panic），而是激活
        flush 复制，让正常渲染逐块把像素拼进全屏缓冲。外部通过创建根目录
        下的 ``screenshot_request`` 文件触发；诊断写入 ``/screenshot_diag``。
        """
        import gc
        if not self._started:
            return None
        gc.collect()
        self._flush_count = 0
        self._shot_buf = bytearray(self.WIDTH * self.HEIGHT * 2)
        self._shot_active = True
        self.screen.invalidate()
        for _ in range(30):
            lv.task_handler()
        fc = self._flush_count
        self._shot_active = False
        big = self._shot_buf
        self._shot_buf = None
        big_nz = sum(1 for b in big if b)
        try:
            with open(path, "wb") as handle:
                handle.write(big)
        except Exception:
            pass
        try:
            with open("/screenshot_diag", "w") as h:
                h.write("fc=%d big_nz=%d free=%d" % (fc, big_nz, gc.mem_free()))
        except Exception:
            pass
        return len(big)

    def tick(self):
        if not self._started:
            return
        _th0 = time.ticks_ms()
        lv.task_handler()
        _th = time.ticks_diff(time.ticks_ms(), _th0)
        if _th > self._th_max:
            self._th_max = _th
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_refresh) < self.REFRESH_MS:
            return
        self._last_refresh = now
        # 触摸/滑动期间暂停刷新：避免 get_state+重绘与滚动动画抢 CPU，松手后立即恢复。
        if self._touch_down:
            return
        try:
            state = self.controller.get_state()
        except Exception as exc:
            self._notice("State read failed: " + str(exc))
            return
        self._state = state
        _rf0 = time.ticks_ms()
        self._refresh(state)
        self._auto_home(now, state)
        _rf = time.ticks_diff(time.ticks_ms(), _rf0)
        if _rf > self._rf_max:
            self._rf_max = _rf
