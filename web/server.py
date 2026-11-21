"""手机 Web 服务：复用阅读灯项目的 Microdot 路由架构。

网络线程只提交命令和读取快照，不直接访问 GPIO、PWM、数据库或 LVGL。
"""
import time

import _thread

from lib.microdot import Microdot, send_file


def _web_root():
    """兼容设备相对路径与主机绝对路径，定位 web 目录。"""
    path = __file__
    index = path.rfind("/")
    if index < 0:
        index = path.rfind("\\")
    return path[:index] if index >= 0 else "."


WEB_ROOT = _web_root()


class WebServer:
    def __init__(self, controller, host="0.0.0.0", port=80):
        self.controller = controller
        self.host = host
        self.port = port
        self.app = Microdot()
        self.running = False
        self._register_routes()

    def start(self):
        if self.running:
            return
        self.running = True
        try:
            _thread.stack_size(32 * 1024)
        except (OSError, ValueError):
            pass
        _thread.start_new_thread(self._run, ())

    def stop(self):
        self.running = False
        try:
            self.app.shutdown()
            if self.app.server is not None:
                self.app.server.close()
        except OSError:
            pass

    def _run(self):
        try:
            self.app.run(host=self.host, port=self.port, debug=False, ssl=None)
        except Exception as exc:
            self.running = False
            print("Web server:", exc)

    def _register_routes(self):
        app = self.app

        @app.before_request
        def note_activity(request):
            # 仅刷新配置 AP 的活动计时，不参与 LVGL 本地触摸计时。
            self.controller.note_web_activity()

        @app.get("/")
        def index(request):
            return self._index()

        @app.get("/index.html")
        def index_file(request):
            return self._index()

        @app.get("/static/<path:path>")
        def static_file(request, path):
            if ".." in path:
                return {"ok": False, "message": "invalid path"}, 404
            try:
                return send_file(WEB_ROOT + "/static/" + path, max_age=0)
            except OSError:
                return {"ok": False, "message": "not found"}, 404

        @app.get("/api/state")
        def state(request):
            return self.controller.get_state()

        @app.get("/api/config")
        def config_get(request):
            return {"ok": True, "data": self.controller.get_config()}

        @app.post("/api/command")
        def command(request):
            body = self._json_object(request)
            if body is None or not body.get("type"):
                return {"ok": False, "message": "command type required"}, 400
            return self._execute(body)

        @app.post("/api/config")
        def config_post(request):
            body = self._json_object(request)
            if body is None:
                return {"ok": False, "message": "JSON object required"}, 400
            values = body.get("values", body)
            if not isinstance(values, dict):
                return {"ok": False, "message": "config values object required"}, 400
            return self._execute({"type": "set_config", "values": values})

        @app.get("/api/wifi/scan")
        def wifi_scan(request):
            return self._execute({"type": "wifi_scan"}, 12000)

        @app.post("/api/wifi/config")
        def wifi_config(request):
            body = self._json_object(request)
            if body is None:
                return {"ok": False, "message": "JSON object required"}, 400
            return self._execute({"type": "wifi_config",
                                  "ssid": body.get("ssid", ""),
                                  "password": body.get("password", ""),
                                  "auth": body.get("auth", "auto")})

        @app.errorhandler(Exception)
        def server_error(request, exc):
            print("Web request:", exc)
            return {"ok": False, "message": str(exc)}, 500

    @staticmethod
    def _json_object(request):
        """非法 JSON 直接返回 400，避免 Microdot 打印无意义的异常栈。"""
        try:
            body = request.json
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _index():
        return send_file(WEB_ROOT + "/static/index.html", max_age=0)

    def _execute(self, command, timeout_ms=4000):
        """提交到主循环命令队列，并在本请求线程内等待执行结果。"""
        command_id = self.controller.submit(command, source="web")
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            result = self.controller.command_result(command_id)
            if result is not None:
                return result, (200 if result["ok"] else 400)
            time.sleep_ms(20)
        return {"ok": True, "accepted": True, "id": command_id,
                "message": "command queued"}, 202
