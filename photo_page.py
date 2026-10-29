"""照片重命名页签：第 4 个页签的界面与交互。

沿用另两个批量页签的两阶段模式：
  ① 分析（只读）→ plan.csv → 人工核对 → ② 执行 → manifest.csv
"""

import os
import threading
import tkinter as tk
import traceback
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import config
import photo_media
import photo_rename


class PhotoPage:
    tag = "photo"

    def __init__(self, parent, app):
        self.app = app
        self.result = None
        self.cfg = config.load()
        self.cancel = threading.Event()
        self.excluded = set()        # 被用户手动移除的原文件名（全路径）
        self.show_only_ok = tk.BooleanVar(value=False)
        self._build(parent)

    # ---------------------------------------------------------------- 界面

    def _build(self, parent):
        self.frame = ttk.Frame(parent)
        wrap = ttk.Frame(self.frame, padding=10)
        wrap.pack(fill="both", expand=True)

        row1 = ttk.Frame(wrap)
        row1.pack(fill="x")
        ttk.Label(row1, text="源目录", width=8).pack(side="left")
        self.var_src = tk.StringVar(value=config.get_value(self.cfg, "photo.source_dir", ""))
        ttk.Entry(row1, textvariable=self.var_src).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row1, text="选择…", width=8, command=self._pick_src).pack(side="left")
        self.var_sub = tk.BooleanVar(value=bool(config.get_value(self.cfg, "photo.include_subdir", True)))
        ttk.Checkbutton(row1, text="包含子目录（只改文件，目录名不动）",
                        variable=self.var_sub).pack(side="left", padx=(10, 0))

        row2 = ttk.Frame(wrap)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="模板", width=8).pack(side="left")
        self.var_tpl = tk.StringVar(value=config.get_value(self.cfg, "photo.template", "{type}_{date}_{time}"))
        self.entry_tpl = ttk.Entry(row2, textvariable=self.var_tpl)
        self.entry_tpl.pack(side="left", fill="x", expand=True, padx=8)
        self.entry_tpl.bind("<FocusOut>", self._on_template_leave)
        ttk.Button(row2, text="变量说明", width=10, command=self._show_vars).pack(side="left")
        self.lbl_tpl = ttk.Label(wrap, text="", foreground="#5F5E5A")
        self.lbl_tpl.pack(fill="x", pady=(2, 0))
        self._on_template_leave()

        row3 = ttk.Frame(wrap)
        row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="日期依据", width=8).pack(side="left")
        self.var_basis = tk.StringVar(
            value=config.get_value(self.cfg, "photo.date_basis", photo_media.DATE_BASIS_SHOOT))
        cb_basis = ttk.Combobox(row3, width=10, state="readonly", textvariable=self.var_basis,
                                values=photo_media.DATE_BASES)
        cb_basis.pack(side="left", padx=(0, 6))
        cb_basis.bind("<<ComboboxSelected>>", self._on_basis_change)

        ttk.Label(row3, text="无拍摄日期时").pack(side="left", padx=(10, 4))
        self.var_alt = tk.StringVar(
            value=config.get_value(self.cfg, "photo.alternate_date", photo_media.ALTERNATE_KEEP))
        self.cb_alt = ttk.Combobox(row3, width=13, state="readonly", textvariable=self.var_alt,
                                   values=photo_media.ALTERNATE_DATES)
        self.cb_alt.pack(side="left")
        self._on_basis_change()

        row4 = ttk.Frame(wrap)
        row4.pack(fill="x", pady=(6, 0))
        ttk.Label(row4, text="跳过扩展名", width=8).pack(side="left")
        self.var_skip = tk.StringVar(value=str(config.get_value(self.cfg, "photo.skip_extensions", "")))
        ttk.Entry(row4, textvariable=self.var_skip, width=40).pack(side="left", padx=8)
        ttk.Label(row4, text="（空格或逗号分隔，如 mp4 .mov；留空则不过滤）",
                  foreground="#5F5E5A").pack(side="left")

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=10)
        self.btn_scan = ttk.Button(btns, text="① 分析（只读）", command=self._on_scan)
        self.btn_scan.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="停止", width=6, command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 8))
        self.btn_apply = ttk.Button(btns, text="② 执行计划", command=self._on_apply, state="disabled")
        self.btn_apply.pack(side="left", padx=8)
        self.btn_open = ttk.Button(btns, text="打开报告目录", command=self.open_plan_dir,
                                   state="disabled")
        self.btn_open.pack(side="left")
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=6)
        self.btn_remove = ttk.Button(btns, text="移除选中", width=9,
                                     command=self._remove_selected, state="disabled")
        self.btn_remove.pack(side="left")
        ttk.Checkbutton(btns, text="仅显示待重命名", variable=self.show_only_ok,
                        command=self._refresh_table).pack(side="left", padx=(12, 0))

        table = ttk.Frame(wrap)
        table.pack(fill="both", expand=True)
        cols = ("idx", "dir", "old", "new", "date", "camera", "status", "reason")
        widths = (52, 150, 160, 240, 130, 100, 60, 200)
        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=14)
        for key, text, width in zip(cols, ("序", "所在目录", "原文件名", "新文件名",
                                           "拍摄日期", "相机", "状态", "原因"), widths):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w", stretch=(key in ("new", "reason")))
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("skip", foreground="#A32D2D")
        self.tree.tag_configure("even", background="#FFFFFF")
        self.tree.tag_configure("odd", background="#F5F7FA")

        self.log = tk.Text(wrap, height=8, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(8, 0))

    # ---------------------------------------------------------------- 小工具

    def _log(self, msg):
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")

    def _pick_src(self):
        d = filedialog.askdirectory()
        if d:
            self.var_src.set(os.path.normpath(d))

    def _on_template_leave(self, _ev=None):
        tpl = self.var_tpl.get()
        ok, err = photo_rename.validate_template(tpl)
        if ok:
            self.lbl_tpl.config(text="模板有效", foreground="#0F6E56")
        else:
            self.lbl_tpl.config(text="模板无效：" + err, foreground="#A32D2D")
        return ok

    def _on_basis_change(self, _ev=None):
        """只有主基准是「拍摄日期」时，备选才有意义。"""
        is_shoot = self.var_basis.get() == photo_media.DATE_BASIS_SHOOT
        self.cb_alt.configure(state="readonly" if is_shoot else "disabled")
        if not is_shoot:
            self.var_alt.set(photo_media.ALTERNATE_KEEP)

    def _show_vars(self):
        lines = ["可用变量："] + ["  %-12s %s" % (k, d) for k, d in photo_rename.TEMPLATE_VARS]
        messagebox.showinfo("模板变量", "\n".join(lines))

    def _save_cfg(self):
        config.set_value(self.cfg, "photo.source_dir", self.var_src.get())
        config.set_value(self.cfg, "photo.template", self.var_tpl.get())
        config.set_value(self.cfg, "photo.include_subdir", bool(self.var_sub.get()))
        config.set_value(self.cfg, "photo.date_basis", self.var_basis.get())
        config.set_value(self.cfg, "photo.alternate_date", self.var_alt.get())
        config.set_value(self.cfg, "photo.skip_extensions", self.var_skip.get())
        return config.save(self.cfg)

    def open_plan_dir(self):
        if self.result and self.result.get("report_dir"):
            try:
                os.startfile(self.result["report_dir"])
            except OSError as e:
                messagebox.showerror("打开失败", str(e))

    # ---------------------------------------------------------------- 互斥钩子

    def set_enabled(self, flag):
        state = "normal" if flag else "disabled"
        try:
            self.btn_scan.config(state=state)
        except tk.TclError:
            pass
        if not flag:
            for w in (self.btn_apply, self.btn_open, self.btn_remove, self.btn_stop):
                try:
                    w.config(state="disabled")
                except tk.TclError:
                    pass
        else:
            has_rows = bool(self.result and self._visible_rows())
            try:
                self.btn_apply.config(state="normal" if self.result else "disabled")
                self.btn_open.config(state="normal" if self.result else "disabled")
                self.btn_remove.config(state="normal" if has_rows else "disabled")
                self.btn_stop.config(state="disabled")
            except tk.TclError:
                pass

    def handle(self, kind, payload=None):
        if kind == "log":
            self._log(payload)
        elif kind == "prog":
            done, total = payload
            self.app.progress_det(done, total, "照片重命名：%d/%d" % (done, total))
        elif kind == "done":
            self._on_done(payload)
        elif kind == "applied":
            self._on_applied(payload)
        elif kind == "error":
            self.app.release()
            self.app.progress_done()
            self._log("[x] " + str(payload).strip().splitlines()[-1])
            messagebox.showerror("出错了", str(payload))

    # ---------------------------------------------------------------- 分析

    def _on_scan(self):
        src = self.var_src.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("提示", "请先选择有效的源目录")
            return
        if not self._on_template_leave():
            messagebox.showerror("模板无效", self.lbl_tpl.cget("text"))
            return
        if not self.app.acquire(self):
            return

        if not self._save_cfg():
            self._log("[!] 配置保存失败（程序目录不可写），本次设置不会被记住")
        self.tree.delete(*self.tree.get_children())
        self.result = None
        self.excluded.clear()
        self.cancel.clear()
        self.btn_stop.config(state="normal")
        self.btn_apply.config(state="disabled")
        self.btn_open.config(state="disabled")
        self.btn_remove.config(state="disabled")
        self.app.progress_start("分析中…（只读）")

        cfg_tpl = self.var_tpl.get()
        cfg_sub = bool(self.var_sub.get())
        cfg_basis = self.var_basis.get()
        cfg_alt = self.var_alt.get()
        cfg_skip = photo_rename.parse_skip_extensions(self.var_skip.get())
        try:
            cfg_workers = max(1, int(self.app.var_workers.get()))
        except (tk.TclError, ValueError):
            cfg_workers = photo_media.DEFAULT_WORKERS
        post = self.app.post

        def work():
            try:
                paths, skipped = photo_rename.collect_files(
                    src, cfg_sub, skip_extensions=cfg_skip,
                    on_log=lambda m: post(self.tag, "log", m))
                rows = photo_rename.plan_renames(
                    paths, cfg_tpl,
                    date_basis=cfg_basis, alternate=cfg_alt, workers=cfg_workers,
                    on_progress=lambda d, t: post(self.tag, "prog", (d, t)),
                    on_log=lambda m: post(self.tag, "log", m),
                    cancel=self.cancel)
                rd = photo_rename.report_dir_for(src)
                try:
                    os.makedirs(rd, exist_ok=True)
                except OSError as e:
                    raise RuntimeError("无法创建报告目录：%s" % e)
                stopped = self.cancel.is_set()
                name = ("plan_partial_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
                        if stopped else "plan.csv")
                plan = os.path.join(rd, name)
                photo_rename.write_plan_csv(rows, plan)
                post(self.tag, "log", "[i] %s：%s"
                     % ("分析已停止，只写出部分计划" if stopped else "计划报表已写出", plan))
                post(self.tag, "done", {"rows": rows, "plan": plan, "report_dir": rd,
                                        "stopped": stopped})
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, result):
        self.app.release()
        self.app.progress_done()
        self.btn_stop.config(state="disabled")
        self.result = result
        self.excluded.clear()
        self._refresh_table()
        ok_n = sum(1 for r in result["rows"] if r["状态"] == "ok")
        skip_n = len(result["rows"]) - ok_n
        if result.get("stopped"):
            self.btn_apply.config(state="disabled")
            self._log("[!] 分析已停止，结果不完整，未生成可执行计划；请重新分析")
            self.app.set_status("照片重命名：分析已停止（结果不完整），不能执行")
            return
        self.app.set_status("照片重命名：可执行 %d，跳过 %d" % (ok_n, skip_n))

    def _visible_rows(self):
        """按当前过滤与排除集合，返回要显示的 (原始行, 是否被排除)。"""
        if not self.result:
            return []
        rows = self.result["rows"]
        out = []
        for r in rows:
            full = os.path.join(r["所在目录"], r["原文件名"])
            if full in self.excluded:
                continue
            if self.show_only_ok.get() and r["状态"] != "ok":
                continue
            out.append(r)
        return out

    def _refresh_table(self):
        """重建表格。仅显示待重命名 开关与移除操作都走这里。"""
        self.tree.delete(*self.tree.get_children())
        if not self.result:
            self.btn_remove.config(state="disabled")
            return
        ok_n = 0
        for i, r in enumerate(self._visible_rows()):
            if r["状态"] == "ok":
                ok_n += 1
            tag = "skip" if r["状态"] != "ok" else ("even" if i % 2 else "odd")
            self.tree.insert("", "end", tags=(tag,), values=(
                r["序号"], r["所在目录"], r["原文件名"], r["新文件名"] or "-",
                r["拍摄日期"], r["相机"], r["状态"], r["原因"]))
        self.btn_apply.config(state="normal" if ok_n else "disabled")
        self.btn_open.config(state="normal")
        self.btn_remove.config(state="normal" if self._visible_rows() else "disabled")

    def _remove_selected(self):
        """把选中的行从本次计划中排除（不影响磁盘文件）。"""
        if not self.result:
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在表格中选择要移除的行")
            return
        names = []
        for iid in sel:
            vals = self.tree.item(iid)["values"]
            if vals:
                full = os.path.join(str(vals[1]), str(vals[2]))
                self.excluded.add(full)
                names.append(str(vals[2]))
        self._log("[i] 已从本次计划中移除 %d 条：%s" % (len(names), "、".join(names[:5])))
        self._refresh_table()
        self.app.set_status("照片重命名：剩余 %d 条在计划中" % len(self._visible_rows()))

    def _on_stop(self):
        self.cancel.set()
        self.btn_stop.config(state="disabled")
        self._log("[i] 正在停止…")

    # ---------------------------------------------------------------- 执行

    def _on_apply(self):
        if not self.result:
            return
        visible = self._visible_rows()
        ok_rows = [r for r in visible if r["状态"] == "ok"]
        skip_n = len(visible) - len(ok_rows)
        excluded_n = len(self.excluded)
        if not ok_rows:
            messagebox.showinfo("提示", "没有可执行的条目")
            return
        msg = ("即将执行不可逆操作：重命名 %d 个文件\n"
               "跳过 %d 个（无日期 / 名称非法 / 未变化）\n"
               % (len(ok_rows), skip_n))
        if excluded_n:
            msg += "已手动移除 %d 条，不会执行\n" % excluded_n
        msg += ("\n⚠️ 执行依据是本次分析在内存里的结果，修改 plan.csv 不会影响执行。\n"
                "计划报表（仅供核对）：%s\n\n确定继续？" % self.result["plan"])
        if not self.app.mb.askyesno("确认执行", msg, icon="warning"):
            self._log("[i] 已取消执行")
            return
        if not self.app.acquire(self):
            return
        self.cancel.clear()
        self.btn_stop.config(state="normal")
        self.app.progress_start("执行中…")
        post = self.app.post
        report_dir = self.result["report_dir"]

        def work():
            try:
                res = photo_rename.apply_plan(
                    ok_rows, report_dir,
                    on_progress=lambda d, t: post(self.tag, "prog", (d, t)),
                    on_log=lambda m: post(self.tag, "log", m),
                    cancel=self.cancel)
                post(self.tag, "applied", res)
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _on_applied(self, res):
        self.app.release()
        self.app.progress_done()
        self.btn_stop.config(state="disabled")
        self.btn_apply.config(state="disabled")
        self.btn_open.config(state="normal")
        parts = ["成功 %d" % res["moved"], "失败 %d" % res["failed"]]
        if res.get("stopped"):
            parts.append("未处理 %d" % res.get("skipped", 0))
        self.app.set_status("照片重命名完成：" + "，".join(parts))
        self._log("[i] 台账：%s" % res["manifest"])
