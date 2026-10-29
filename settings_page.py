"""设置页：放全局配置（哈希算法、线程数量、ffprobe 路径）。

这三项原本挤在顶部工具条，挪到独立页签后顶部更清爽。
为了让各业务页签继续用 `self.app.var_algo` / `self.app.var_workers` 读取（不改它们的代码），
这里把 tk 变量挂回 `app`。
"""

import tkinter as tk

from common import ALGOS, DEFAULT_ALGO
from page_base import PageBase


class SettingsPage(PageBase):
    tag = "set"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        # 挂回 App：业务页签仍用 self.app.var_algo / var_workers 读取；
        # ffprobe 路径也让 App 继续经 self.var_ffprobe 读写
        self.var_algo = tk.StringVar(value=DEFAULT_ALGO)
        self.var_workers = tk.IntVar(value=4)
        self.var_ffprobe = tk.StringVar()
        app.var_algo = self.var_algo
        app.var_workers = self.var_workers
        app.var_ffprobe = self.var_ffprobe
        self._build_ui(parent)

    def _build_ui(self, parent):
        ttk = self.ttk
        self.frame = ttk.Frame(parent)
        f = self.frame
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="哈希算法").grid(row=0, column=0, sticky="w",
                                           padx=10, pady=(14, 4))
        cb = ttk.Combobox(f, width=16, state="readonly",
                          textvariable=self.var_algo, values=list(ALGOS.keys()))
        cb.grid(row=0, column=1, sticky="w", padx=(10, 2), pady=(14, 4))
        self._algo_cb = cb
        # 性能测试按钮紧挨选择框
        ttk.Button(f, text="性能测试", width=10,
                   command=self.app._bench).grid(row=0, column=2, sticky="w",
                                                 padx=(2, 10), pady=(14, 4))
        ttk.Label(f, text="查重清理 / 查重(关键字) 计算哈希所用的算法").grid(
            row=1, column=1, columnspan=3, sticky="w", padx=10, pady=(0, 12))

        ttk.Label(f, text="线程数量").grid(row=2, column=0, sticky="w",
                                           padx=10, pady=4)
        sp = ttk.Spinbox(f, from_=1, to=32, width=6,
                         textvariable=self.var_workers)
        sp.grid(row=2, column=1, sticky="w", padx=10, pady=4)
        self._workers_sp = sp
        ttk.Label(f, text="1~32；照片/视频元数据读取与查重哈希的并发数").grid(
            row=3, column=1, columnspan=3, sticky="w", padx=10, pady=(0, 12))

        ttk.Label(f, text="ffprobe 路径").grid(row=4, column=0, sticky="w",
                                               padx=10, pady=4)
        entry = ttk.Entry(f, textvariable=self.var_ffprobe, width=44)
        entry.grid(row=4, column=1, sticky="w", padx=10, pady=4)
        entry.bind("<FocusOut>", self.app._apply_ffprobe)
        entry.bind("<Return>", self.app._apply_ffprobe)
        self._ff_entry = entry
        self._ff_pick = ttk.Button(f, text="选择…", width=8,
                                   command=self.app._pick_ffprobe)
        self._ff_pick.grid(row=4, column=2, sticky="w", padx=(2, 4), pady=4)
        self._ff_clear = ttk.Button(f, text="清空", width=6,
                                    command=self.app._clear_ffprobe)
        self._ff_clear.grid(row=4, column=3, sticky="w", padx=(2, 0), pady=4)
        ttk.Label(f, text="留空＝自动查找（exe 同级 / tools 子目录）；都没有则退回内置解析").grid(
            row=5, column=1, columnspan=3, sticky="w", padx=10, pady=(0, 12))

    def set_enabled(self, flag):
        self._algo_cb.configure(state="readonly" if flag else "disabled")
        self._workers_sp.configure(state="normal" if flag else "disabled")
        self._ff_entry.configure(state="normal" if flag else "disabled")
        st = "normal" if flag else "disabled"
        self._ff_pick.configure(state=st)
        self._ff_clear.configure(state=st)
