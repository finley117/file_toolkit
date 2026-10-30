"""查重清理页（第 1 个页签）的界面。

内核（扫描 / 哈希 / 分组 / 执行）仍在 file_toolkit.py，这里只放界面与交互。
本模块由 App._build() 在运行时导入，避免与 file_toolkit 形成循环依赖。
"""

import os
import subprocess
import threading
import time
import traceback
from datetime import datetime

import config
from common import (APP, SRC_BASE, SRC_EXTRA, checker_execute, checker_groups,
                    checker_plan, checker_space, preflight, scan_tree)
from page_base import PageBase
from util import human_size, is_drive_root, long_path, paths_overlap


class CheckerPage(PageBase):
    tag = "chk"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.groups = []
        self.n_base = self.n_extra = 0
        self.started = 0.0
        self._reset_sel()
        self._build_ui(parent)
        self._load_cfg()

    def _build_ui(self, parent):
        ttk, tk = self.ttk, self.tk
        self.frame = ttk.Frame(parent)
        wrap = ttk.Frame(self.frame, padding=10)
        wrap.pack(fill="both", expand=True)

        row1 = ttk.Frame(wrap)
        row1.pack(fill="x")
        ttk.Label(row1, text="基础目录", width=8).pack(side="left")
        self.var_dir = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_dir).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row1, text="选择…", width=8, command=self._pick_base).pack(side="left")
        self.btn_start = ttk.Button(row1, text="开始扫描", width=10, command=self._start)
        self.btn_start.pack(side="left", padx=(8, 0))
        self.btn_stop = ttk.Button(row1, text="停止", width=6, command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(wrap)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="额外目录", width=8).pack(side="left")
        self.var_extra = tk.StringVar()
        ttk.Entry(row2, textvariable=self.var_extra).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row2, text="选择…", width=8, command=self._pick_extra).pack(side="left")
        self.var_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="仅比对额外目录（基础库内部重复不处理）",
                        variable=self.var_only).pack(side="left", padx=(10, 0))

        opts = ttk.Frame(wrap)
        opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="最小(KB)").pack(side="left")
        self.var_min = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=0, to=1024 * 1024, width=6, textvariable=self.var_min).pack(side="left", padx=6)
        self.var_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="包含隐藏/系统", variable=self.var_hidden).pack(side="left", padx=(8, 0))

        table = ttk.Frame(wrap)
        table.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("sel", "src", "count", "size", "hash", "path")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse", height=14)
        for key, text, width, anchor in (
            ("sel", "移除", 48, "center"), ("src", "来源", 52, "center"),
            ("count", "份数", 52, "center"), ("size", "大小", 88, "e"),
            ("hash", "哈希", 132, "w"), ("path", "路径", 600, "w"),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "path"))
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("even", background="#FFFFFF")
        self.tree.tag_configure("odd", background="#F5F7FA")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._open_location)

        bottom = ttk.Frame(wrap)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, text="待移除目录").pack(side="left")
        self.var_out = tk.StringVar()
        ttk.Entry(bottom, textvariable=self.var_out).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(bottom, text="选择…", width=8, command=self._pick_out).pack(side="left")
        self.btn_all = ttk.Button(bottom, text="全选", width=6,
                                  command=lambda: self._set_all(True))
        self.btn_all.pack(side="left", padx=(8, 0))
        self.btn_none = ttk.Button(bottom, text="全不选", width=7,
                                   command=lambda: self._set_all(False))
        self.btn_none.pack(side="left", padx=6)
        self.btn_remove = ttk.Button(bottom, text="移除", width=8, style="Write.TButton",
                                     command=self._do_remove, state="disabled")
        self.btn_remove.pack(side="left")

        self.stats = ttk.Label(wrap, text="")
        self.stats.pack(fill="x", pady=(6, 0))

        self.log = tk.Text(wrap, height=6, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(6, 0))

        # 结果与参数绑定：扫描时记下当时那组参数，之后一旦改动就让「移除」失效，
        # 避免"扫的是 A 目录、校验却用新填的 B 目录"这种错配。
        self.scanned = None
        self._stats_text = ""
        self._loading = False       # 分批插表中：禁用全选/全不选/移除
        self._done_status = ""
        for var in (self.var_dir, self.var_extra, self.var_min, self.var_hidden, self.var_only):
            var.trace_add("write", self._on_param_change)

    def _pick_base(self):
        d = self.app.fd.askdirectory()
        if d:
            self.var_dir.set(os.path.normpath(d))

    def _pick_extra(self):
        d = self.app.fd.askdirectory()
        if d:
            self.var_extra.set(os.path.normpath(d))

    def _pick_out(self):
        d = self.app.fd.askdirectory()
        if d:
            self.var_out.set(os.path.normpath(d))

    def set_enabled(self, flag):
        self.btn_start.config(state="normal" if flag else "disabled")
        self.btn_stop.config(state="disabled")
        state = "normal" if (flag and not self._loading) else "disabled"
        for w in (self.btn_all, self.btn_none):
            try:
                w.config(state=state)
            except Exception:
                pass
        if not flag:
            self.btn_remove.config(state="disabled")

    def _load_cfg(self):
        cfg = self.app.cfg
        self.var_dir.set(str(config.get_value(cfg, "checker.base", "")))
        self.var_extra.set(str(config.get_value(cfg, "checker.extra", "")))
        self.var_out.set(str(config.get_value(cfg, "checker.out", "")))
        try:
            self.var_min.set(int(config.get_value(cfg, "checker.min_kb", 1)))
        except (TypeError, ValueError):
            pass
        self.var_hidden.set(bool(config.get_value(cfg, "checker.hidden", False)))
        self.var_only.set(bool(config.get_value(cfg, "checker.only_extra", True)))

    def save_cfg(self):
        cfg = self.app.cfg
        config.set_value(cfg, "checker.base", self.var_dir.get().strip())
        config.set_value(cfg, "checker.extra", self.var_extra.get().strip())
        config.set_value(cfg, "checker.out", self.var_out.get().strip())
        try:
            config.set_value(cfg, "checker.min_kb", int(self.var_min.get()))
        except (TypeError, ValueError):
            pass
        config.set_value(cfg, "checker.hidden", bool(self.var_hidden.get()))
        config.set_value(cfg, "checker.only_extra", bool(self.var_only.get()))

    def _cur_params(self):
        """当前界面上这组会影响扫描结果的参数。"""
        try:
            min_kb = int(self.var_min.get())
        except Exception:
            min_kb = None
        return {"base": self.var_dir.get().strip(),
                "extra": self.var_extra.get().strip(),
                "min": min_kb,
                "hidden": bool(self.var_hidden.get()),
                "only": bool(self.var_only.get())}

    @staticmethod
    def _param_key(p):
        """比较用：目录统一成绝对小写路径，"最小(KB)"没填好时记成 None。"""
        def norm(s):
            s = (s or "").strip()
            return os.path.normcase(os.path.abspath(s)) if s else ""
        return (norm(p["base"]), norm(p["extra"]), p["min"], p["hidden"], p["only"])

    def _refresh_remove_btn(self):
        same = (self.scanned is not None
                and self._param_key(self._cur_params()) == self._param_key(self.scanned))
        enabled = self.groups and same and not self._loading and not self.app.busy
        self.btn_remove.config(state="normal" if enabled else "disabled")

    def _on_param_change(self, *_):
        """参数被改动：结果保留（还能看），但「移除」失效并说明原因。"""
        if self.scanned is None or self.app.busy:
            return
        if self._param_key(self._cur_params()) == self._param_key(self.scanned):
            self._refresh_remove_btn()
            self.stats.config(text=self._stats_text)
        else:
            self.btn_remove.config(state="disabled")
            self.stats.config(text="参数已改变，当前结果基于扫描时的设置"
                                   "（改回原参数或重新扫描后才能移除）")

    def _start(self):
        base = self.var_dir.get().strip()
        extra = self.var_extra.get().strip()
        if not base or not os.path.isdir(base):
            self.app.mb.showerror(APP, "请先选择有效的基础目录")
            return
        if is_drive_root(base):
            self.app.mb.showerror(APP, "暂不支持以盘根作为基础目录，请选择盘内的具体目录（整盘扫描请分段进行）")
            return
        if self.var_only.get() and not extra:
            if not self.app.mb.askyesno(
                    APP, "勾选了「仅比对额外目录」，但没有填写额外目录。\n\n"
                         "是否改为「仅清理基础目录内部重复」（等同取消该勾选）？"):
                return
            self.var_only.set(False)
        if extra:
            if not os.path.isdir(extra):
                self.app.mb.showerror(APP, "额外目录无效")
                return
            if paths_overlap(base, extra):
                self.app.mb.showerror(APP, "额外目录不能与基础目录相同，也不能互相包含（会导致同一文件被扫两次）")
                return
        try:
            min_bytes = max(0, int(self.var_min.get())) * 1024
            workers = max(1, int(self.app.var_workers.get()))
        except Exception:
            self.app.mb.showerror(APP, "「最小(KB)」与「线程」必须填写数字")
            return
        if not self.app.acquire(self):
            return
        self.app.stop.clear()
        self.tree.delete(*self.tree.get_children())
        self.groups = []
        self._reset_sel()
        self.scanned = None
        self._stats_text = ""
        self._render_token = getattr(self, "_render_token", 0) + 1   # 作废旧的分批插入
        self._loading = False
        self.stats.config(text="")
        self.btn_remove.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.app.progress_start("扫描中…")
        self.started = time.time()
        algo = self.app.var_algo.get()
        hidden = bool(self.var_hidden.get())
        only_extra = bool(self.var_only.get()) and bool(extra)
        stop = self.app.stop
        post = self.app.post
        self._log("[i] 开始扫描：%s%s（最小 %d KB，算法 %s，%d 线程）"
                  % (base, (" + " + extra) if extra else "", min_bytes // 1024,
                     algo, workers))

        def work():
            try:
                base_abs = os.path.abspath(base)
                rows, skipped = scan_tree(base_abs, min_bytes, hidden, stop, SRC_BASE,
                                          lambda n: post(self.tag, "scan", ("基础", n)))
                n_base = len(rows)
                n_extra = 0
                if extra and not stop.is_set():
                    more, sk2 = scan_tree(os.path.abspath(extra), min_bytes, hidden, stop,
                                          SRC_EXTRA,
                                          lambda n: post(self.tag, "scan", ("额外", n_base + n)))
                    n_extra = len(more)
                    rows += more
                    for k, v in sk2.items():
                        skipped[k] = skipped.get(k, 0) + v
                if stop.is_set():
                    groups = []
                else:
                    stat = {}
                    groups = checker_groups(rows, workers, stop, algo, only_extra,
                                            lambda d, t: post(self.tag, "hash", (d, t)),
                                            stat=stat)
                    skipped.update(stat)
                post(self.tag, "done",
                     (groups, skipped, only_extra, bool(extra), n_base, n_extra))
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as e:
            self.app.release()
            self.app.progress_done()
            self._log("[x] 无法启动扫描线程：%s" % e)
            self.app.mb.showerror(APP, "无法启动扫描线程：\n%s" % e)

    def _stop(self):
        if self.app.busy:
            self.app.stop.set()
            self.app.set_status("正在停止…")
            self.btn_stop.config(state="disabled")

    def _log(self, msg):
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")

    def handle(self, kind, payload):
        if kind == "scan":
            stage, n = payload
            self.app.set_status(f"扫描中（{stage}）… 已发现 {n} 个文件")
        elif kind == "hash":
            d, t = payload
            self.app.progress_det(d, t, f"哈希校验中 {d}/{t}（{self.app.var_algo.get()}）")
        elif kind == "done":
            self._render(*payload)
        elif kind == "rprog":
            d, t = payload
            self.app.progress_det(d, t, f"移除中 {d}/{t}")
        elif kind == "removed":
            self._removed(payload)
        elif kind == "error":
            self.app.release()
            self._log("[x] " + payload.splitlines()[-1])
            self.app.mb.showerror(APP, payload)
            self.app.set_status("出错：" + payload.splitlines()[-1])

    def _render(self, groups, skipped, only_extra, has_extra, n_base, n_extra):
        self.app.release()
        self.n_base = n_base
        self.n_extra = n_extra
        self.groups = groups
        self._reset_sel()
        items = []
        for gi, g in enumerate(groups):
            tag = "even" if gi % 2 == 0 else "odd"
            for i, m in enumerate(g["members"]):
                if m["kept"]:
                    mark = "★"
                elif not m["deletable"]:
                    mark = "–"
                else:
                    mark = "☑" if m["checked"] else "☐"
                items.append((m["path"], (
                    mark, "基础" if m["src"] == SRC_BASE else "额外",
                    len(g["members"]) if i == 0 else "",
                    human_size(g["size"]) if i == 0 else "",
                    g["hash"] if i == 0 else "", m["path"],
                ), tag))
        waste = self._sel_size
        used = time.time() - self.started
        skip_txt = "，".join(f"{k} {v}" for k, v in sorted(skipped.items()))
        stopped = "（已停止，结果不完整）" if self.app.stop.is_set() else ""
        mode = ("仅比对额外目录" if only_extra else
                ("基础目录去重" if not has_extra else "全量去重"))
        self.app.progress_done()
        status = (f"完成{stopped}：基础 {self.n_base} 个 / 额外 {self.n_extra} 个文件，"
                  f"耗时 {used:.1f}s，{mode}，算法 {self.app.var_algo.get()}")
        if not groups:
            hint = "未发现基础目录与额外目录重复的文件" if has_extra else "未发现重复文件"
            status = f"{hint}{stopped}"
        self._done_status = status
        self.app.set_status(status)
        self._log("[OK] 扫描完成%s：基础 %d / 额外 %d，重复 %d 组，待移除可回收 %s，耗时 %.1fs"
                  % (stopped, self.n_base, self.n_extra, len(groups), human_size(waste), used))
        if skip_txt:
            self._log("[i] 跳过明细：" + skip_txt)
        if not self.var_out.get().strip():
            self.var_out.set(self._default_out(self.var_dir.get().strip() or "."))
        self._stats_text = (f"重复 {len(groups)} 组 / 待移除可回收 {human_size(waste)}"
                            f"　|　跳过：{skip_txt or '无'}"
                            f"　|　★ 保留　– 不处理　☑ 待移除（单击切换）　双击定位")
        self.stats.config(text=self._stats_text)
        self.scanned = self._cur_params()      # 记住这组参数，之后改动会让「移除」失效
        self._refresh_remove_btn()
        self._set_loading(True)
        self._render_token = getattr(self, "_render_token", 0) + 1
        token = self._render_token
        self.tree.delete(*self.tree.get_children())
        self._insert_items(items, 0, token)

    def _set_loading(self, flag):
        """分批插表期间禁用全选/全不选/移除，插完再恢复。"""
        self._loading = flag
        if flag:
            for w in (self.btn_all, self.btn_none, self.btn_remove):
                w.config(state="disabled")
            self.app.set_status("正在加载查重结果…")
            return
        state = "normal" if not self.app.busy else "disabled"
        for w in (self.btn_all, self.btn_none):
            w.config(state=state)
        self._refresh_remove_btn()
        self.app.set_status(self._done_status)

    def _insert_items(self, items, start, token):
        """分批插表，几万行也不会一次性卡死界面。"""
        if token != self._render_token:
            return
        end = min(start + 400, len(items))
        try:
            for iid, values, tag in items[start:end]:
                self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        except Exception:
            try:
                self._set_loading(False)   # 别把全选/移除永久锁死
            except Exception:
                pass
            raise
        if end < len(items):
            self.frame.after(1, lambda: self._insert_items(items, end, token))
        else:
            self._set_loading(False)

    def _default_out(self, base):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return "%s_待移除_%s" % (os.path.abspath(base).rstrip("\\/"), stamp)

    def _reset_sel(self):
        """结果变化后重建勾选索引与计数；勾选过程中只做增量加减。"""
        self._by_path = {}
        self._group_n = {}
        self._sel = self._sel_size = self._copy_size = 0
        for gi, g in enumerate(self.groups):
            for m in g["members"]:
                self._by_path[m["path"]] = (gi, m)
                if m["checked"] and m["deletable"]:
                    self._group_n[gi] = self._group_n.get(gi, 0) + 1
                    self._sel += 1
                    self._sel_size += m["size"]
            if self._group_n.get(gi):
                self._copy_size += g["keeper"]["size"]

    def _set_checked(self, gi, m, flag):
        if not m["deletable"] or m["checked"] == flag:
            return
        m["checked"] = flag
        delta = 1 if flag else -1
        n = self._group_n.get(gi, 0)
        self._group_n[gi] = n + delta
        self._sel += delta
        self._sel_size += delta * m["size"]
        if n == 0 and flag:
            self._copy_size += self.groups[gi]["keeper"]["size"]
        elif n == 1 and not flag:
            self._copy_size -= self.groups[gi]["keeper"]["size"]
        self.tree.set(m["path"], "sel", "☑" if flag else "☐")

    def _set_all(self, flag):
        if self._loading or not self.groups:
            return
        for gi, m in self._by_path.values():
            self._set_checked(gi, m, flag)
        self._update_stats()

    def _on_click(self, ev):
        if self.app.busy or self._loading:
            return
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        if self.tree.identify_column(ev.x) != "#1":
            return
        hit = self._by_path.get(self.tree.identify_row(ev.y))
        if not hit:
            return
        gi, m = hit
        self._set_checked(gi, m, not m["checked"])
        self._update_stats()

    def _update_stats(self):
        groups = sum(1 for n in self._group_n.values() if n)
        self._stats_text = (
            f"待移除 {self._sel} 个文件 / {groups} 组 / 释放 {human_size(self._sel_size)}"
            f"　|　保留件副本占额外 {human_size(self._copy_size)}"
            f"　|　★ 保留　– 不处理　☑ 待移除（单击切换）　双击定位")
        self.stats.config(text=self._stats_text)

    def _do_remove(self):
        if self._loading:
            return
        plan = checker_plan(self.groups)
        if not plan:
            self.app.mb.showinfo(APP, "没有勾选任何待移除文件")
            return
        snap = self.scanned
        if snap is None or self._param_key(self._cur_params()) != self._param_key(snap):
            self.app.mb.showerror(APP, "参数已改变（目录 / 最小大小 / 隐藏文件 / 比对模式），"
                                       "请重新扫描后再移除")
            return
        base = snap["base"]
        extra = snap["extra"]
        dst = self.var_out.get().strip()
        if not dst:
            self.app.mb.showwarning(APP, "请先指定待移除目录")
            return
        dst = os.path.abspath(dst)
        created = not os.path.isdir(dst)

        def drop_created():
            if created:
                try:
                    os.rmdir(dst)
                except OSError:
                    pass

        try:
            os.makedirs(long_path(dst), exist_ok=True)
        except OSError as e:
            self.app.mb.showerror(APP, f"待移除目录不可用：{e}")
            return
        need, copy_size = checker_space(plan)
        problems = preflight(dst, [("基础目录", base), ("额外目录", extra)],
                             dst_label="待移除目录", must_be_empty=True, need_bytes=need)
        if problems:
            drop_created()
            self.app.mb.showerror(APP, "以下问题会阻止执行：\n\n· " + "\n· ".join(problems))
            return
        n = sum(len(p["moves"]) for p in plan)
        msg = (f"即将执行移动（不是删除）：\n\n"
               f"· 移动 {n} 个文件 → {dst}\n"
               f"· 涉及 {len(plan)} 个哈希组\n"
               f"· 每组复制一份保留件到组目录，供人工对比（占用 {human_size(copy_size)}）\n"
               f"· 需要空间约 {human_size(need)}\n\n⚠️ 确认继续？")
        if not self.app.mb.askyesno("确认移除", msg, icon="warning"):
            drop_created()
            return
        if not self.app.acquire(self):
            drop_created()
            return
        only_extra = bool(snap["only"]) and bool(extra)
        self._log("[i] 开始移除：%d 个文件 → %s" % (n, dst))
        self.app.stop.clear()
        self.btn_remove.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.app.progress_det(0, 1, "移除中…")
        algo = self.app.var_algo.get()
        stop = self.app.stop
        post = self.app.post

        def work():
            try:
                r = checker_execute(plan, dst, stop,
                                    lambda d, t: post(self.tag, "rprog", (d, t)), algo, only_extra)
                post(self.tag, "removed", r)
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as e:
            self.app.release()
            self.app.progress_done()
            self._log("[x] 无法启动移除线程：%s" % e)
            self.app.mb.showerror(APP, "无法启动移除线程：\n%s" % e)

    def _removed(self, r):
        self.app.release()
        self.tree.delete(*self.tree.get_children())
        self.groups = []
        self.scanned = None
        self._reset_sel()
        skip_txt = f"，未尝试 {r['skipped']}" if r.get("skipped") else ""
        self.app.set_status(f"移除完成：成功 {r['ok']}，失败 {r['fail']}{skip_txt}，"
                            f"保留件副本 {r['copied']}"
                            + ("（已中断）" if r["stopped"] else ""))
        self._log("[OK] 移除完成：成功 %d，失败 %d，未尝试 %d，保留件副本 %d%s"
                  % (r["ok"], r["fail"], r.get("skipped", 0), r["copied"],
                     "（已中断）" if r["stopped"] else ""))
        if r.get("manifest_error"):
            self.stats.config(text="清单写入失败：%s　|　移动结果见上方日志"
                                   % r["manifest_error"])
            self.app.mb.showwarning(APP, "文件移动已完成，但清单写入失败：\n%s\n\n"
                                          "请到待移除目录查看；本次明细见日志。"
                                          % r["manifest_error"])
        else:
            self.stats.config(text=f"清单已写入 {r.get('manifest')}"
                                   f"　|　对比完成后可手动删除整个待移除目录")
        if r["fail"]:
            self.app.mb.showwarning(APP, f"有 {r['fail']} 个文件移动失败，详见 "
                                          f"{r.get('manifest') or '待移除目录'}")

    def _open_location(self, _ev=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(sel[0])])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(sel[0])])
        except Exception:
            pass
