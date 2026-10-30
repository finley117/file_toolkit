"""File Toolkit 入口：共用工具条、页签容器、任务互斥、配置读写。

算法/扫描/分组等内核在 common.py，通用工具在 util.py，四个页签各在自己的模块里。
"""

import os
import queue
import sys
import threading
import traceback

import config
import photo_media
from checker_page import CheckerPage
from common import ALGOS, APP, BENCH_MB, DEFAULT_ALGO, VER, benchmark_all
from dedup_page import DedupPage
from media_page import MediaPage
from photo_page import PhotoPage
from settings_page import SettingsPage


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk, self.ttk = tk, ttk
        self.fd, self.mb = filedialog, messagebox
        self.root = tk.Tk()
        self.root.title(f"{APP} v{VER}")
        self.root.geometry("1140x800")
        self.root.minsize(940, 600)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.owner = None
        self.cfg = config.load()
        self._build()
        self._load_cfg()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    # ---------------------------------------------------------------- 配置

    def _load_cfg(self):
        """启动时把上次的选择搬回界面；没有配置文件时全走默认值。"""
        algo = str(config.get_value(self.cfg, "app.algo", DEFAULT_ALGO))
        if algo in ALGOS:
            self.var_algo.set(algo)
        try:
            workers = int(config.get_value(self.cfg, "app.workers", 4))
        except (TypeError, ValueError):
            workers = 4
        self.var_workers.set(min(32, max(1, workers)))
        self.var_ffprobe.set(str(config.get_value(self.cfg, "app.ffprobe_path", "")))
        self._apply_ffprobe()
        geo = str(config.get_value(self.cfg, "app.geometry", "") or "")
        if geo and "x" in geo:
            try:
                self.root.geometry(geo)
            except self.tk.TclError:
                pass
        try:
            tab = int(config.get_value(self.cfg, "app.last_tab", 0))
        except (TypeError, ValueError):
            tab = 0
        if 0 <= tab < len(self.nb.tabs()):
            self.nb.select(tab)

    def _save_cfg(self):
        config.set_value(self.cfg, "app.algo", self.var_algo.get())
        try:
            config.set_value(self.cfg, "app.workers", int(self.var_workers.get()))
        except (TypeError, ValueError, self.tk.TclError):
            pass
        config.set_value(self.cfg, "app.geometry", self.root.geometry())
        config.set_value(self.cfg, "app.last_tab", self.nb.index(self.nb.select()))
        config.set_value(self.cfg, "app.ffprobe_path", self.var_ffprobe.get().strip())
        for page in self.pages.values():
            page.save_cfg()
        ok = config.save(self.cfg)
        if not ok:
            self.set_status("配置保存失败：程序目录可能没有写权限")
            try:
                self.mb.showwarning(APP, "配置保存失败：程序目录可能没有写权限，\n"
                                          "本次设置不会被记住。")
            except Exception:
                pass
        return ok

    def _on_close(self):
        try:
            self._save_cfg()
        finally:
            self.root.destroy()

    def _pick_ffprobe(self):
        path = self.fd.askopenfilename(title="选择 ffprobe.exe",
                                       filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.var_ffprobe.set(os.path.normpath(path))
            self._apply_ffprobe()
            self.set_status("ffprobe：已指定 %s" % path)

    def _clear_ffprobe(self):
        self.var_ffprobe.set("")
        self._apply_ffprobe()
        self.set_status("ffprobe：恢复自动查找（exe 同级或 tools 子目录）")

    def _apply_ffprobe(self, _ev=None):
        """把界面上的路径交给 photo_media。

        非空但文件不存在时：使用点（`_find_tool`）本就会忽略这个覆盖、退回自动查找，不崩；
        这里再在用户操作时给出提示并把框清空，避免「填了个无效路径却以为生效」的错觉。
        启动/选路径/清空时（`_ev` 为 None）不弹窗，仅静默退回自动查找。
        """
        path = self.var_ffprobe.get().strip()
        if path and not os.path.isfile(path):
            if _ev is not None:
                self.mb.showwarning(
                    APP, "ffprobe 路径不存在：\n%s\n\n将恢复自动查找"
                    "（exe 同级 / tools 子目录）。" % path)
            self.var_ffprobe.set("")
            path = ""
        photo_media.set_tool_path("ffprobe", path)

    def _build(self):
        ttk, tk = self.ttk, self.tk
        outer = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        outer.pack(fill="both", expand=True)

        # 会真动文件的按钮统一样式（加粗 + 深红），与"只读"按钮区分开
        style = ttk.Style()
        style.configure("Write.TButton", font=("Microsoft YaHei UI", 9, "bold"),
                        foreground="#A32D2D")

        # 底部状态区先从底部占位，窗口再小也不会被页签内容挤出可视区
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x")
        self.pb = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(10, 4))
        self.lbl = ttk.Label(bottom, text=f"File Toolkit v{VER}　|　"
                                          f"查重清理 · 查重(关键字) · 媒体索引 · 照片重命名 · 设置　|　任一处任务运行时其他页签暂停")
        self.lbl.pack(fill="x")

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(10, 0))
        self.chk = CheckerPage(self.nb, self)
        self.ded = DedupPage(self.nb, self)
        self.med = None                 # 首次切到该页签才建库，见 _ensure_media
        self.med_holder = ttk.Frame(self.nb)
        self.med_failed = False
        self.photo = PhotoPage(self.nb, self)
        self.set = SettingsPage(self.nb, self)
        self.nb.add(self.chk.frame, text="  查重清理  ")
        self.nb.add(self.ded.frame, text="  查重(关键字)  ")
        self.nb.add(self.med_holder, text="  媒体索引  ")
        self.nb.add(self.photo.frame, text="  照片重命名  ")
        self.nb.add(self.set.frame, text="  设置  ")
        self.pages = {"chk": self.chk, "ded": self.ded,
                      "set": self.set, "photo": self.photo}
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _ev=None):
        try:
            if self.nb.index(self.nb.select()) == 2:
                self._ensure_media()
        except self.tk.TclError:
            pass

    def _ensure_media(self):
        """媒体索引页首次进入时才建库；建库失败只影响这一页，其余页签照常可用。"""
        if self.med is not None or self.med_failed:
            return
        try:
            self.med = MediaPage(self.med_holder, self)
        except Exception as e:
            self.med_failed = True
            self.med = None
            self.ttk.Label(self.med_holder, justify="left", foreground="#A32D2D",
                           text="媒体索引不可用：索引库无法创建（目录可能不可写）\n%s" % e
                           ).pack(padx=20, pady=40)
            self.mb.showerror(APP, "媒体索引不可用：无法创建/打开索引库。\n%s\n\n其余页签不受影响。" % e)
            return
        self.med.frame.pack(fill="both", expand=True)
        self.pages["med"] = self.med

    def post(self, tag, kind, payload=None):
        self.q.put((tag, kind, payload))

    def _poll(self):
        try:
            while True:
                tag, kind, payload = self.q.get_nowait()
                page = self.pages.get(tag)
                if page is None:
                    continue
                try:
                    page.handle(kind, payload)
                except Exception:
                    tb = traceback.format_exc()
                    self.pb.stop()
                    self.set_status("内部错误：" + tb.strip().splitlines()[-1])
                    if self.busy:
                        self.release()
                    print(tb)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll)

    def acquire(self, page):
        if self.busy:
            self.mb.showinfo(APP, "另一个页签正在运行，等它结束再操作")
            return False
        self.busy = True
        self.owner = page
        for p in self.pages.values():
            p.set_enabled(False)
        return True

    def release(self):
        owner = self.owner
        self.busy = False
        self.owner = None
        for p in self.pages.values():
            p.set_enabled(True)
        if getattr(owner, "tag", "") != "med":
            self.pb.stop()
            self.pb.config(mode="determinate")
            self.pb["value"] = 100

    def set_status(self, text):
        self.lbl.config(text=text)

    def progress_start(self, text):
        self.pb.stop()
        self.pb.config(mode="indeterminate")
        self.pb.start(12)
        self.lbl.config(text=text)

    def progress_det(self, d, t, text):
        if str(self.pb["mode"]) != "determinate":
            self.pb.stop()
            self.pb.config(mode="determinate")
        self.pb["value"] = (d / t * 100) if t else 0
        self.lbl.config(text=text)

    def progress_done(self):
        self.pb.stop()
        self.pb.config(mode="determinate")
        self.pb["value"] = 100

    def _bench(self):
        ttk, tk = self.ttk, self.tk
        win = tk.Toplevel(self.root)
        win.title("算法性能测试")
        win.geometry("460x320")
        win.transient(self.root)
        ttk.Label(win, text=f"用 {BENCH_MB} MB 内存数据测纯 CPU 吞吐（不含磁盘 I/O）",
                  padding=10).pack(fill="x")
        tv = ttk.Treeview(win, columns=("algo", "speed"), show="headings", height=8)
        tv.heading("algo", text="算法")
        tv.heading("speed", text="吞吐")
        tv.column("algo", width=180, anchor="w")
        tv.column("speed", width=220, anchor="e")
        tv.pack(fill="both", expand=True, padx=10)
        tip = ttk.Label(win, text="正在分配测试数据…", padding=10)
        tip.pack(fill="x")
        alive = {"ok": True}
        win.bind("<Destroy>", lambda _e: alive.__setitem__("ok", False))

        def run():
            results = benchmark_all()
            for name, mb in results:
                self.root.after(0, lambda n=name, m=mb: alive["ok"] and tv.insert(
                    "", "end", iid=n, values=(n, f"{m:,.0f} MB/s")))
            best = max(results, key=lambda x: x[1])[0]
            self.root.after(0, lambda: alive["ok"] and tip.config(
                text=f"最快：{best}　（双击一行即选定该算法）"))

        def choose(_ev=None):
            sel = tv.selection()
            if sel:
                self.var_algo.set(sel[0])
                win.destroy()

        tv.bind("<Double-1>", choose)
        threading.Thread(target=run, daemon=True).start()

    def run(self):
        self.root.mainloop()


def main():
    try:
        App().run()
    except Exception:
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP, traceback.format_exc())
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
