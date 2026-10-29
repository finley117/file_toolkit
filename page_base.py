"""四个页签的共同基类。

所有页签都是「放一个 Frame 进 Notebook，界面里只跟 self.app 打交道」。基类把重复的初始化
（保存 app 引用、把 ttk/tk/mb/fd/root 从 app 取过来、建运行锁）收归一处，并给
handle / save_cfg / load_cfg 提供空默认实现，这样 App 就能无差别地遍历所有页签调用，
不用再写 `self.chk.xxx / self.ded.xxx` 这种特判。

继承时只要实现 `_build_ui(parent)`（建界面）和 `set_enabled(flag)`（运行锁定时禁用控件）。
"""

import threading


class PageBase:
    tag = ""  # 子线程回传消息时用来派发到本页签

    def __init__(self, parent, app):
        self.app = app
        self.parent = parent
        self.ttk = app.ttk
        self.tk = app.tk
        self.mb = app.mb
        self.fd = app.fd
        self.root = app.root
        self.lock = threading.Lock()
        self.running = False

    def _build_ui(self, parent):
        raise NotImplementedError

    def set_enabled(self, flag):
        raise NotImplementedError

    def handle(self, kind, payload):
        """子线程通过 app.post 回传的消息；不需要接收的页签留空即可。"""

    def save_cfg(self):
        """关窗时写回配置；不需要落盘的页签留空即可。"""

    def load_cfg(self):
        """启动时读回配置；不需要的页签留空即可。"""
