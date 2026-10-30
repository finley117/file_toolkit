"""查重(关键字)页（第 2 个页签）的界面。

内核（关键字提取 / 分组 / 移动）仍在 file_toolkit.py。
本模块由 App._build() 在运行时导入，避免与 file_toolkit 形成循环依赖。
"""

import os
import re
import threading
import traceback
from datetime import datetime

import config
from common import APP, _KEYWORD_HINT, dedup_analyze, dedup_execute, preflight
from page_base import PageBase


class DedupPage(PageBase):
    tag = "ded"

    def __init__(self, parent, app):
        self.cfg = app.cfg                 # 与 App 共用同一份配置字典（要在 super 之前，_build_ui 会读）
        super().__init__(parent, app)
        self.cancel = threading.Event()
        self._build_ui(parent)
        self._load_cfg()

    def _build_ui(self, parent):
        ttk, tk = self.ttk, self.tk
        self.frame = ttk.Frame(parent)
        wrap = ttk.Frame(self.frame, padding=10)
        wrap.pack(fill="both", expand=True)

        grid = ttk.Frame(wrap)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.var_src = tk.StringVar()
        self.var_dst = tk.StringVar()
        self.var_quar = tk.StringVar()
        for i, (label, var) in enumerate((("待整理目录", self.var_src),
                                          ("移动到的目录", self.var_dst),
                                          ("hash 重复文件", self.var_quar))):
            ttk.Label(grid, text=label, width=13).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(grid, textvariable=var).grid(row=i, column=1, sticky="ew", padx=8, pady=3)
            ttk.Button(grid, text="选择…", width=8,
                       command=lambda v=var: self._pick(v)).grid(row=i, column=2, pady=3)

        opts = ttk.Frame(wrap)
        opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="关键字正则", width=9).pack(side="left")
        self.var_rule = tk.StringVar(
            value=str(config.get_value(self.cfg, "dedup.keyword_rule", "")))
        entry = ttk.Entry(opts, textvariable=self.var_rule, width=22)
        entry.pack(side="left", padx=8)
        entry.bind("<FocusOut>", self._on_rule_change)
        entry.bind("<Return>", self._on_rule_change)
        ttk.Button(opts, text="恢复默认", width=10,
                   command=self._reset_rule).pack(side="left", padx=(0, 18))
        ttk.Label(opts, text="最小(KB)").pack(side="left")
        self.var_minsize = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=0, to=1048576, width=8,
                    textvariable=self.var_minsize).pack(side="left", padx=(6, 18))
        ttk.Label(opts, text="重复文件保留").pack(side="left")
        self.var_keep = tk.StringVar(value="最早优先")
        ttk.Combobox(opts, width=10, state="readonly", textvariable=self.var_keep,
                     values=["最早优先", "最新优先"]).pack(side="left", padx=6)

        self.lbl_rule = ttk.Label(wrap, text="", foreground="#5F5E5A")
        self.lbl_rule.pack(fill="x", pady=(2, 0))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=10)
        self.btn_start = ttk.Button(btns, text="开始整理", style="Write.TButton",
                                    command=self._on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="停止", width=6,
                                   command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Label(btns, text="只扫一层目录；关键字命中 → 移动到的目录；内容完全相同 → hash 重复文件",
                  foreground="#5F5E5A").pack(side="left", padx=(12, 0))

        self.log = tk.Text(wrap, height=16, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True, pady=(6, 0))
        sb.pack(side="right", fill="y")
        self._on_rule_change()

    def set_enabled(self, flag):
        self.btn_start.config(state="normal" if flag else "disabled")
        self.btn_stop.config(state="disabled")

    def _load_cfg(self):
        cfg = self.cfg
        self.var_src.set(str(config.get_value(cfg, "dedup.src", "")))
        self.var_dst.set(str(config.get_value(cfg, "dedup.dst", "")))
        self.var_quar.set(str(config.get_value(cfg, "dedup.quar", "")))
        try:
            self.var_minsize.set(int(config.get_value(cfg, "dedup.min_kb", 0)))
        except (TypeError, ValueError):
            pass
        keep = str(config.get_value(cfg, "dedup.keep", "最早优先"))
        if keep in ("最早优先", "最新优先"):
            self.var_keep.set(keep)

    def save_cfg(self):
        cfg = self.cfg
        config.set_value(cfg, "dedup.keyword_rule", self.var_rule.get().strip())
        config.set_value(cfg, "dedup.src", self.var_src.get().strip())
        config.set_value(cfg, "dedup.dst", self.var_dst.get().strip())
        config.set_value(cfg, "dedup.quar", self.var_quar.get().strip())
        try:
            config.set_value(cfg, "dedup.min_kb", int(self.var_minsize.get()))
        except (TypeError, ValueError):
            pass
        config.set_value(cfg, "dedup.keep", self.var_keep.get())

    def _on_rule_change(self, _ev=None):
        text = self.var_rule.get().strip()
        if not text:
            self.lbl_rule.config(text=_KEYWORD_HINT, foreground="#5F5E5A")
            return True
        try:
            re.compile(text)
        except re.error as e:
            self.lbl_rule.config(text="正则无效：%s" % e, foreground="#A32D2D")
            return False
        self.lbl_rule.config(text="自定义正则：取第一个匹配，多分组按顺序拼接，结果转大写",
                             foreground="#0F6E56")
        return True

    def _reset_rule(self):
        self.var_rule.set("")
        self._on_rule_change()

    def _pick(self, var):
        d = self.app.fd.askdirectory()
        if d:
            var.set(os.path.normpath(d))
            if var is self.var_src:
                b = os.path.normpath(d)
                if not self.var_dst.get().strip():
                    self.var_dst.set(b + "_sorted")
                if not self.var_quar.get().strip():
                    self.var_quar.set(b + "_dup_hash")

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def handle(self, kind, payload):
        if kind == "log":
            self._log(payload)
        elif kind == "prog":
            d, t = payload
            self.app.progress_det(d, t, f"哈希校验中 {d}/{t}（{self.app.var_algo.get()}）")
        elif kind == "ready":
            self.app.root.after_idle(self._confirm, payload)
        elif kind == "mprog":
            d, t = payload
            self.app.progress_det(d, t, f"移动中 {d}/{t}")
        elif kind == "done":
            self._finished(payload)
        elif kind == "error":
            self.app.release()
            self.app.progress_done()
            self._log("[x] " + payload)
            self.app.set_status("出错，见日志")
            self.app.mb.showerror(APP, payload)

    def _paths(self):
        src = self.var_src.get().strip()
        dst = self.var_dst.get().strip()
        quar = self.var_quar.get().strip()
        if src and not dst:
            dst = os.path.abspath(src) + "_sorted"
        if src and not quar:
            quar = os.path.abspath(src) + "_dup_hash"
        self.var_dst.set(dst)
        self.var_quar.set(quar)
        return src, dst, quar

    def _check_paths(self, src, dst, quar):
        """三个路径两两检查：相等或互相包含都拒绝执行。"""
        if not src or not os.path.isdir(src):
            return ["待整理目录无效或不存在"]
        problems = []
        for label, p in (("移动到的目录", dst), ("hash 重复文件", quar)):
            if not p:
                problems.append(f"请填写「{label}」")
        if problems:
            return problems
        problems += preflight(dst, [("待整理目录", src), ("hash 重复文件", quar)],
                              dst_label="移动到的目录")
        problems += preflight(quar, [("待整理目录", src), ("移动到的目录", dst)],
                              dst_label="hash 重复文件")
        return problems

    def _on_start(self):
        src, dst, quar = self._paths()
        problems = self._check_paths(src, dst, quar)
        if problems:
            self.app.mb.showerror(APP, "路径设置有冲突，未执行：\n\n· "
                                  + "\n· ".join(problems))
            return
        if not self._on_rule_change():
            self.app.mb.showerror(APP, "关键字正则无效，请修正后再试")
            return
        try:
            workers = max(1, int(self.app.var_workers.get()))
            min_kb = max(0, int(self.var_minsize.get()))
        except Exception:
            self.app.mb.showerror(APP, "「最小(KB)」与「线程」必须填写数字")
            return
        if not self.app.acquire(self):
            return
        rule = self.var_rule.get().strip()
        self.save_cfg()
        if not config.save(self.cfg):
            self._log("[!] 配置保存失败（程序目录不可写），本次设置不会被记住")
        self.cancel.clear()
        self.btn_stop.config(state="normal")
        self.app.progress_start("分析中…")
        algo = self.app.var_algo.get()
        keep = self.var_keep.get()
        post = self.app.post

        def work():
            try:
                r = dedup_analyze(src, dst, quar, workers, min_kb * 1024, keep, algo, rule,
                                  on_log=lambda m: post(self.tag, "log", m),
                                  on_progress=lambda d, t: post(self.tag, "prog", (d, t)),
                                  cancel=self.cancel)
                post(self.tag, "ready", r)
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as e:
            self.app.release()
            self.app.progress_done()
            self._log("[x] 无法启动分析线程：%s" % e)
            self.app.mb.showerror(APP, "无法启动分析线程：\n%s" % e)

    def _confirm(self, r):
        if r.get("stopped"):
            self.app.release()
            self.app.progress_done()
            self._log("[i] 已停止，未移动任何文件")
            self.app.set_status("整理已停止")
            return
        c, dst, quar = r["counts"], r["dst"], r["quar"]
        lines = ["即将把文件移动到两个目录（不删除、不改名）：", "",
                 f"· 关键字命中 {c.get('移动', 0)} 个 → {dst}",
                 f"· 内容完全相同 {c.get('隔离', 0)} 个 → {quar}",
                 f"· 不动 {c.get('不动', 0)} 个"]
        for label, d in (("移动到的目录", dst), ("hash 重复文件", quar)):
            if os.path.isdir(d) and os.listdir(d):
                lines.append(f"· 提示：{label} 已有内容，将追加进去，同名文件跳过")
        sample = [x for x in r["rows"] if x["动作"] != "不动"][:10]
        if sample:
            lines += ["", f"示例（前 {len(sample)} 个）："]
            for x in sample:
                lines.append("  %s → %s" % (
                    x["文件名"],
                    "hash 重复文件" if x["动作"] == "隔离" else "移动到的目录"))
        lines += ["", "⚠️ 确认继续？"]
        if not self.app.mb.askyesno("确认整理", "\n".join(lines), icon="warning"):
            self.app.release()
            self.app.progress_done()
            self._log("[i] 已取消，未移动任何文件")
            self.app.set_status("整理已取消")
            return
        self.app.progress_det(0, 1, "移动中…")
        post, rows = self.app.post, r["rows"]

        def work():
            try:
                res = dedup_execute(rows, dst, quar,
                                    on_log=lambda m: post(self.tag, "log", m),
                                    on_progress=lambda d, t: post(self.tag, "mprog", (d, t)),
                                    cancel=self.cancel)
                post(self.tag, "done", res)
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as e:
            self.app.release()
            self.app.progress_done()
            self._log("[x] 无法启动执行线程：%s" % e)
            self.app.mb.showerror(APP, "无法启动执行线程：\n%s" % e)

    def _on_stop(self):
        if self.app.busy:
            self.cancel.set()
            self.btn_stop.config(state="disabled")
            self._log("[i] 正在停止…")

    def _finished(self, res):
        self.app.release()
        self.app.progress_done()
        msg = "整理完成：移动 %d，跳过 %d，失败 %d" % (res["moved"], res["skipped"], res["failed"])
        if res["stopped"]:
            msg += "（已中断）"
        self.app.set_status(msg)
        self._log("[OK] " + msg)
        if res["failed"]:
            self.app.mb.showwarning(APP, "有 %d 个文件移动失败，明细见下方日志" % res["failed"])
