#!/usr/bin/env python3
"""Same File Checker v1.2：双目录比对（基础库 vs 额外目录），按大小预筛 + 哈希找重复，重复件移动到待移除目录（附保留件副本）。"""

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

APP = "Same File Checker"
VER = "1.2"
_CHUNK = 4 << 20
_BENCH_MB = 32
_ATTR_HIDDEN = 0x2
_ATTR_SYSTEM = 0x4
_ATTR_REPARSE = 0x400
_SKIP_EXT = (".lnk",)
_TODO = "todo"
SRC_BASE, SRC_EXTRA = "base", "extra"

try:
    import xxhash
except Exception:
    xxhash = None

ALGOS = {}
if xxhash is not None:
    ALGOS["xxh3-128"] = xxhash.xxh3_128
    ALGOS["xxh3-64"] = xxhash.xxh3_64
ALGOS["MD5"] = hashlib.md5
ALGOS["SHA-1"] = hashlib.sha1
ALGOS["BLAKE2b"] = hashlib.blake2b
ALGOS["SHA-256"] = hashlib.sha256
DEFAULT_ALGO = "xxh3-128" if xxhash is not None else "MD5"


def _long(path):
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if len(p) < 240:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def _is_attr(st, flag):
    attr = getattr(st, "st_file_attributes", None)
    return bool(attr is not None and attr & flag)


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _overlap(a, b):
    try:
        common = os.path.commonpath([_norm(a), _norm(b)])
    except ValueError:
        return False
    return common in (_norm(a), _norm(b))


def validate_dirs(base, extra, out):
    if not base or not os.path.isdir(base):
        raise ValueError("基础目录无效")
    base = os.path.abspath(base)
    extra = os.path.abspath(extra) if extra else None
    if extra:
        if not os.path.isdir(extra):
            raise ValueError("额外目录无效")
        if _overlap(base, extra):
            raise ValueError("额外目录不能与基础目录相同，也不能互相包含（会导致同一文件被扫两次）")
    if out:
        out = os.path.abspath(out)
        for p in [base] + ([extra] if extra else []):
            if _overlap(out, p):
                raise ValueError("待移除目录不能位于基础目录或额外目录内部")
    return base, extra, (os.path.abspath(out) if out else None)


def _unique_path(folder, name):
    target = os.path.join(folder, name)
    if not os.path.exists(_long(target)):
        return target
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = os.path.join(folder, f"{stem} ({i}){ext}")
        if not os.path.exists(_long(cand)):
            return cand
        i += 1


def digest_file(path, algo, stop=None):
    h = ALGOS[algo]()
    with open(_long(path), "rb") as f:
        while True:
            buf = f.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
            if stop is not None and stop.is_set():
                return None
    return h.hexdigest()


def scan_tree(root, min_bytes, include_hidden, stop, src, on_progress):
    rows = []
    skipped = defaultdict(int)
    stack = [root]
    while stack:
        if stop.is_set():
            break
        cur = stack.pop()
        try:
            entries = list(os.scandir(_long(cur)))
        except OSError:
            skipped["目录无法访问"] += 1
            continue
        for entry in entries:
            if stop.is_set():
                break
            try:
                st = entry.stat(follow_symlinks=False)
                if _is_attr(st, _ATTR_REPARSE):
                    skipped["链接/联接点"] += 1
                    continue
                if not include_hidden and (_is_attr(st, _ATTR_HIDDEN) or _is_attr(st, _ATTR_SYSTEM)):
                    skipped["隐藏/系统"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped["非普通文件"] += 1
                    continue
                if entry.name.lower().endswith(_SKIP_EXT):
                    skipped["快捷方式"] += 1
                    continue
                if st.st_size < min_bytes:
                    skipped["小于阈值"] += 1
                    continue
                rel = os.path.relpath(entry.path, root)
                rows.append({"path": entry.path, "size": st.st_size, "mtime": st.st_mtime,
                             "hash": None, "src": src, "depth": rel.count(os.sep),
                             "kept": False, "deletable": False, "checked": False})
            except OSError:
                skipped["读取失败"] += 1
        on_progress(len(rows))
    return rows, dict(skipped)


def hash_group(items, workers, stop, algo, on_progress):
    if not items:
        return
    total = len(items)
    state = {"done": 0}
    lock = threading.Lock()

    def one(it):
        if stop.is_set():
            return
        try:
            it["hash"] = digest_file(it["path"], algo, stop)
        except OSError:
            it["hash"] = None
        with lock:
            state["done"] += 1
            n = state["done"]
        if n % 20 == 0 or n == total:
            on_progress(n, total)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, items))


def keep_rank(item):
    return (0 if item["src"] == SRC_BASE else 1,
            1 if _TODO in item["path"].lower() else 0,
            item["depth"], item["mtime"], item["path"].lower())


def build_groups(rows, workers, stop, algo, only_extra, on_hash):
    by_size = defaultdict(list)
    for r in rows:
        by_size[r["size"]].append(r)
    candidates = [g for g in by_size.values() if len(g) > 1]
    flat = [r for g in candidates for r in g]
    on_hash(0, len(flat))
    hash_group(flat, workers, stop, algo, on_hash)

    by_hash = defaultdict(list)
    for r in flat:
        if r["hash"]:
            by_hash[(r["size"], r["hash"])].append(r)

    groups = []
    for (size, h), members in by_hash.items():
        if len(members) < 2:
            continue
        members.sort(key=keep_rank)
        bases = [m for m in members if m["src"] == SRC_BASE]
        extras = [m for m in members if m["src"] == SRC_EXTRA]
        if only_extra and (not bases or not extras):
            continue
        keeper = bases[0] if (only_extra and bases) else members[0]
        keeper["kept"] = True
        for m in members:
            if m is keeper:
                continue
            if only_extra and m["src"] == SRC_BASE:
                continue
            m["deletable"] = True
            m["checked"] = True
        groups.append({"size": size, "hash": h, "members": members, "keeper": keeper})
    groups.sort(key=lambda g: (-g["size"], -len(g["members"]), g["keeper"]["path"].lower()))
    return groups, {"candidates": len(flat), "size_groups": len(by_size)}


def plan_removal(groups):
    plan = []
    for g in groups:
        moves = [m for m in g["members"] if m["checked"] and m["deletable"]]
        if moves:
            plan.append({"group": g, "moves": moves, "kept": g["keeper"]})
    return plan


def check_space(plan):
    need = sum(m["size"] for p in plan for m in p["moves"])
    need += sum(p["kept"]["size"] for p in plan)
    extra_only = sum(p["kept"]["size"] for p in plan)
    return need, extra_only


def execute_removal(plan, dst, stop, on_progress, algo, only_extra):
    os.makedirs(dst, exist_ok=True)
    mode_txt = "仅比对额外目录" if only_extra else "全量去重"
    manifest = [f"Same File Checker v{VER}  移除清单  {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"算法 {algo}   模式 {mode_txt}", f"目标目录 {dst}", ""]
    ok = fail = copied = 0
    total = sum(len(p["moves"]) for p in plan)
    done = 0
    for gi, item in enumerate(plan, 1):
        if stop.is_set():
            break
        g, kept, moves = item["group"], item["kept"], item["moves"]
        folder = os.path.join(dst, g["hash"][:16])
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            manifest.append(f"[组{gi}] 创建目录失败 {folder}: {e}")
            fail += len(moves)
            continue
        manifest.append(f"[组{gi}] 哈希 {g['hash']}  大小 {human_size(g['size'])}  份数 {len(g['members'])}")
        manifest.append(f"  保留: [{'基础' if kept['src'] == SRC_BASE else '额外'}] {kept['path']}")
        try:
            ktarget = _unique_path(folder, "[保留] " + os.path.basename(kept["path"]))
            shutil.copy2(_long(kept["path"]), _long(ktarget))
            copied += 1
            manifest.append(f"  副本: {ktarget}")
        except OSError as e:
            manifest.append(f"  副本失败: {e}")
        for m in moves:
            if stop.is_set():
                break
            tag = "基础" if m["src"] == SRC_BASE else "额外"
            try:
                target = _unique_path(folder, os.path.basename(m["path"]))
                shutil.move(_long(m["path"]), _long(target))
                ok += 1
                manifest.append(f"  待删[{tag}]: {m['path']}\n     -> {target}")
            except OSError as e:
                fail += 1
                manifest.append(f"  待删[{tag}]: {m['path']}\n     -> 失败: {e}")
            done += 1
            on_progress(done, total)
        manifest.append("")
    summary = {"ok": ok, "fail": fail, "copied": copied,
               "groups": len(plan), "total": total, "stopped": stop.is_set()}
    manifest.append(f"结果：移动成功 {ok}，失败 {fail}，副本 {copied}"
                    + ("（已中断）" if stop.is_set() else ""))
    try:
        with open(os.path.join(dst, "清单.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(manifest) + "\n")
    except OSError:
        pass
    return summary


_bench_buf = None


def benchmark_algo(algo):
    buf = _bench_buf
    h = ALGOS[algo]()
    t = time.perf_counter()
    h.update(buf)
    dt = time.perf_counter() - t
    return len(buf) / dt / 1048576.0 if dt else 0.0


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk, self.ttk = tk, ttk
        self.fd, self.mb = filedialog, messagebox
        self.root = tk.Tk()
        self.root.title(f"{APP} v{VER}")
        self.root.geometry("1120x760")
        self.root.minsize(920, 580)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.groups = []
        self.n_base = 0
        self.n_extra = 0
        self.started = 0.0
        self.busy = False
        self._build()
        self.root.after(100, self._poll)

    def _build(self):
        ttk, tk = self.ttk, self.tk
        wrap = ttk.Frame(self.root, padding=12)
        wrap.pack(fill="both", expand=True)

        row1 = ttk.Frame(wrap)
        row1.pack(fill="x")
        ttk.Label(row1, text="基础目录", width=8).pack(side="left")
        self.var_dir = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_dir).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row1, text="选择…", width=8, command=self._pick_base).pack(side="left")
        self.btn_run = ttk.Button(row1, text="开始扫描", width=10, command=self._start)
        self.btn_run.pack(side="left", padx=(8, 0))
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
        ttk.Label(opts, text="算法").pack(side="left")
        self.var_algo = tk.StringVar(value=DEFAULT_ALGO)
        ttk.Combobox(opts, width=11, state="readonly", textvariable=self.var_algo,
                     values=list(ALGOS.keys())).pack(side="left", padx=(6, 4))
        ttk.Button(opts, text="性能测试", width=9, command=self._bench).pack(side="left")
        ttk.Label(opts, text="线程").pack(side="left", padx=(16, 0))
        self.var_workers = tk.IntVar(value=max(1, min(32, os.cpu_count() or 4)))
        ttk.Spinbox(opts, from_=1, to=32, width=5, textvariable=self.var_workers).pack(side="left", padx=6)
        ttk.Label(opts, text="最小(KB)").pack(side="left", padx=(10, 0))
        self.var_min = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=0, to=1024 * 1024, width=6, textvariable=self.var_min).pack(side="left", padx=6)
        self.var_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="包含隐藏/系统", variable=self.var_hidden).pack(side="left", padx=(8, 0))

        self.pb = ttk.Progressbar(wrap, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(8, 4))
        self.lbl = ttk.Label(wrap, text="就绪：选基础目录（可选额外目录）后点「开始扫描」")
        self.lbl.pack(fill="x")

        table = ttk.Frame(wrap)
        table.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("sel", "src", "count", "size", "hash", "path")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse")
        for key, text, width, anchor in (
            ("sel", "移除", 48, "center"),
            ("src", "来源", 52, "center"),
            ("count", "份数", 52, "center"),
            ("size", "大小", 88, "e"),
            ("hash", "哈希", 132, "w"),
            ("path", "路径", 600, "w"),
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
        ttk.Button(bottom, text="全选", width=6,
                   command=lambda: self._set_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bottom, text="全不选", width=7,
                   command=lambda: self._set_all(False)).pack(side="left", padx=6)
        self.btn_remove = ttk.Button(bottom, text="移除", width=8, command=self._do_remove, state="disabled")
        self.btn_remove.pack(side="left")

        self.stats = ttk.Label(wrap, text="")
        self.stats.pack(fill="x", pady=(8, 0))

    def _pick_base(self):
        d = self.fd.askdirectory()
        if d:
            self.var_dir.set(os.path.normpath(d))

    def _pick_extra(self):
        d = self.fd.askdirectory()
        if d:
            self.var_extra.set(os.path.normpath(d))

    def _pick_out(self):
        d = self.fd.askdirectory()
        if d:
            self.var_out.set(os.path.normpath(d))

    def _post(self, kind, payload=None):
        self.q.put((kind, payload))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "scan":
                    stage, n = payload
                    self.lbl.config(text=f"扫描中（{stage}）… 已发现 {n} 个文件")
                elif kind == "hash":
                    d, t = payload
                    if str(self.pb["mode"]) != "determinate":
                        self.pb.stop()
                        self.pb.config(mode="determinate")
                    self.pb["value"] = (d / t * 100) if t else 0
                    self.lbl.config(text=f"哈希校验中 {d}/{t}（{self.var_algo.get()}）")
                elif kind == "done":
                    self._render(*payload)
                elif kind == "removed":
                    self._removed(payload)
                elif kind == "rprog":
                    d, t = payload
                    self.pb["value"] = (d / t * 100) if t else 0
                    self.lbl.config(text=f"移除中 {d}/{t}")
                elif kind == "error":
                    self._error(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _start(self):
        if self.busy:
            return
        try:
            base, extra, _ = validate_dirs(self.var_dir.get().strip(),
                                           self.var_extra.get().strip(), None)
        except ValueError as e:
            self.mb.showerror(APP, str(e))
            return
        self.stop.clear()
        self.tree.delete(*self.tree.get_children())
        self.groups = []
        self.stats.config(text="")
        self.btn_remove.config(state="disabled")
        self.pb.stop()
        self.pb.config(mode="indeterminate")
        self.pb.start(12)
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl.config(text="扫描中…")
        self.busy = True
        self.started = time.time()
        min_bytes = max(0, int(self.var_min.get())) * 1024
        workers = max(1, int(self.var_workers.get()))
        algo = self.var_algo.get()
        hidden = bool(self.var_hidden.get())
        only_extra = bool(self.var_only.get()) and bool(extra)

        def work():
            try:
                rows, skipped = scan_tree(base, min_bytes, hidden, self.stop, SRC_BASE,
                                          lambda n: self._post("scan", ("基础", n)))
                self.n_base = len(rows)
                if extra and not self.stop.is_set():
                    more, sk2 = scan_tree(extra, min_bytes, hidden, self.stop, SRC_EXTRA,
                                          lambda n: self._post("scan", ("额外", self.n_base + n)))
                    self.n_extra = len(more)
                    rows += more
                    for k, v in sk2.items():
                        skipped[k] = skipped.get(k, 0) + v
                if not self.stop.is_set():
                    groups, meta = build_groups(rows, workers, self.stop, algo, only_extra,
                                                lambda d, t: self._post("hash", (d, t)))
                else:
                    groups, meta = [], {}
                self._post("done", (groups, skipped, meta, only_extra, bool(extra)))
            except Exception:
                self._post("error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        if self.busy:
            self.stop.set()
            self.lbl.config(text="正在停止…")
            self.btn_stop.config(state="disabled")

    def _render(self, groups, skipped, meta, only_extra, has_extra):
        self.busy = False
        self.pb.stop()
        self.pb.config(mode="determinate")
        self.pb["value"] = 100
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.groups = groups
        for gi, g in enumerate(groups):
            tag = "even" if gi % 2 == 0 else "odd"
            for i, m in enumerate(g["members"]):
                if m["kept"]:
                    mark = "★"
                elif not m["deletable"]:
                    mark = "–"
                else:
                    mark = "☑" if m["checked"] else "☐"
                self.tree.insert("", "end", iid=m["path"], values=(
                    mark,
                    "基础" if m["src"] == SRC_BASE else "额外",
                    len(g["members"]) if i == 0 else "",
                    human_size(g["size"]) if i == 0 else "",
                    g["hash"] if i == 0 else "",
                    m["path"],
                ), tags=(tag,))
        waste = sum(m["size"] for g in groups for m in g["members"] if m["checked"] and m["deletable"])
        used = time.time() - self.started
        skip_txt = "，".join(f"{k} {v}" for k, v in sorted(skipped.items()))
        stopped = "（已停止，结果不完整）" if self.stop.is_set() else ""
        mode = "仅比对额外目录" if only_extra else "全量去重"
        self.lbl.config(text=f"完成{stopped}：基础 {self.n_base} 个 / 额外 {self.n_extra} 个文件，"
                             f"耗时 {used:.1f}s，{mode}，算法 {self.var_algo.get()}")
        self.var_out.set(self._default_out(self.var_dir.get().strip() or "."))
        self.stats.config(
            text=f"重复 {len(groups)} 组 / 待移除可回收 {human_size(waste)}"
                 f"　|　跳过：{skip_txt or '无'}　|　★ 保留　– 不处理　☑ 待移除（单击切换）　双击定位")
        self.btn_remove.config(state="normal" if groups else "disabled")
        if not groups:
            hint = "未发现重复文件" if not has_extra else "未发现基础目录与额外目录重复的文件"
            self.lbl.config(text=f"{hint}{stopped}")

    def _default_out(self, base):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        b = os.path.abspath(base).rstrip("\\/")
        if len(b) <= 2 or b.endswith(":"):
            return os.path.join(b, f"待移除_{stamp}")
        return f"{b}_待移除_{stamp}"

    def _iter_members(self):
        for g in self.groups:
            for m in g["members"]:
                yield m

    def _set_all(self, flag):
        if not self.groups:
            return
        for m in self._iter_members():
            if not m["deletable"]:
                continue
            m["checked"] = flag
            self.tree.set(m["path"], "sel", "☑" if flag else "☐")
        self._update_stats()

    def _on_click(self, ev):
        if self.busy:
            return
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        if self.tree.identify_column(ev.x) != "#1":
            return
        iid = self.tree.identify_row(ev.y)
        if not iid:
            return
        for m in self._iter_members():
            if m["path"] == iid and m["deletable"]:
                m["checked"] = not m["checked"]
                self.tree.set(iid, "sel", "☑" if m["checked"] else "☐")
                self._update_stats()
                return

    def _update_stats(self):
        plan = plan_removal(self.groups)
        n = sum(len(p["moves"]) for p in plan)
        size = sum(m["size"] for p in plan for m in p["moves"])
        copy_size = sum(p["kept"]["size"] for p in plan)
        self.stats.config(
            text=f"待移除 {n} 个文件 / {len(plan)} 组 / 释放 {human_size(size)}"
                 f"　|　保留件副本占额外 {human_size(copy_size)}"
                 f"　|　★ 保留　– 不处理　☑ 待移除（单击切换）　双击定位")

    def _do_remove(self):
        if self.busy:
            return
        plan = plan_removal(self.groups)
        if not plan:
            self.mb.showinfo(APP, "没有勾选任何待移除文件")
            return
        base = self.var_dir.get().strip()
        extra = self.var_extra.get().strip()
        dst = self.var_out.get().strip()
        if not dst:
            self.mb.showwarning(APP, "请先指定待移除目录")
            return
        try:
            _, _, dst = validate_dirs(base, extra, dst)
        except ValueError as e:
            self.mb.showerror(APP, str(e))
            return
        if os.path.isdir(dst) and os.listdir(dst):
            self.mb.showerror(APP, f"待移除目录非空，为避免混入旧数据已中止：\n{dst}\n\n请换个新目录。")
            return
        try:
            os.makedirs(dst, exist_ok=True)
            need, copy_size = check_space(plan)
            free = shutil.disk_usage(dst).free
        except OSError as e:
            self.mb.showerror(APP, f"目标目录不可用：{e}")
            return
        if free < need:
            self.mb.showerror(APP, f"目标盘空间不足。\n需要 {human_size(need)}，可用 {human_size(free)}")
            return
        n = sum(len(p["moves"]) for p in plan)
        msg = (f"即将执行移动（不是删除）：\n\n"
               f"· 移动 {n} 个文件 → {dst}\n"
               f"· 涉及 {len(plan)} 个哈希组\n"
               f"· 每组复制一份保留件到组目录，供你人工对比（占用 {human_size(copy_size)}）\n"
               f"· 需要空间约 {human_size(need)}\n\n"
               f"⚠️ 移动会改变文件位置。确认继续？")
        if not self.mb.askyesno("确认移除", msg, icon="warning"):
            return
        only_extra = bool(self.var_only.get()) and bool(extra)
        self.stop.clear()
        self.busy = True
        self.btn_run.config(state="disabled")
        self.btn_remove.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.pb.config(mode="determinate")
        self.pb["value"] = 0
        algo = self.var_algo.get()

        def work():
            try:
                r = execute_removal(plan, dst, self.stop,
                                    lambda d, t: self._post("rprog", (d, t)), algo, only_extra)
                self._post("removed", r)
            except Exception:
                self._post("error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _removed(self, r):
        self.busy = False
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_remove.config(state="disabled")
        self.pb["value"] = 100
        self.tree.delete(*self.tree.get_children())
        self.groups = []
        self.lbl.config(text=f"移除完成：成功 {r['ok']}，失败 {r['fail']}，保留件副本 {r['copied']}"
                             + ("（已中断）" if r["stopped"] else ""))
        self.stats.config(text=f"清单已写入 {os.path.join(self.var_out.get(), '清单.txt')}"
                               f"　|　对比完成后可手动删除整个待移除目录")
        if r["fail"]:
            self.mb.showwarning(APP, f"有 {r['fail']} 个文件移动失败，详见 清单.txt")

    def _error(self, msg):
        self.busy = False
        self.pb.stop()
        self.pb.config(mode="determinate")
        self.pb["value"] = 0
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl.config(text="出错：" + (msg.splitlines()[-1] if msg else ""))
        self.mb.showerror(APP, msg)

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

    def _bench(self):
        global _bench_buf
        ttk, tk = self.ttk, self.tk
        win = tk.Toplevel(self.root)
        win.title("算法性能测试")
        win.geometry("460x320")
        win.transient(self.root)
        ttk.Label(win, text=f"用 {_BENCH_MB} MB 内存数据测纯 CPU 吞吐（不含磁盘 I/O）",
                  padding=10).pack(fill="x")
        cols = ("algo", "speed")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=8)
        tv.heading("algo", text="算法")
        tv.heading("speed", text="吞吐")
        tv.column("algo", width=180, anchor="w")
        tv.column("speed", width=220, anchor="e")
        tv.pack(fill="both", expand=True, padx=10)
        tip = ttk.Label(win, text="正在分配测试数据…", padding=10)
        tip.pack(fill="x")

        def run():
            global _bench_buf
            if _bench_buf is None:
                _bench_buf = os.urandom(_BENCH_MB * 1024 * 1024)
            results = []
            for name in ALGOS:
                mb = benchmark_algo(name)
                results.append((name, mb))
                self.root.after(0, lambda n=name, m=mb: tv.insert(
                    "", "end", iid=n, values=(n, f"{m:,.0f} MB/s")))
            best = max(results, key=lambda x: x[1])[0]
            self.root.after(0, lambda: tip.config(text=f"最快：{best}　（双击一行即选定该算法）"))

        def choose(_ev=None):
            sel = tv.selection()
            if sel:
                self.var_algo.set(sel[0])
                win.destroy()

        tv.bind("<Double-1>", choose)
        threading.Thread(target=run, daemon=True).start()

    def run(self):
        self.root.mainloop()


def _selftest(base, extra=None, apply_dst=None, only_extra=True):
    stop = threading.Event()
    rows, skipped = scan_tree(base, 1024, False, stop, SRC_BASE, lambda n: None)
    if extra:
        more, sk2 = scan_tree(extra, 1024, False, stop, SRC_EXTRA, lambda n: None)
        rows += more
        for k, v in sk2.items():
            skipped[k] = skipped.get(k, 0) + v
    groups, meta = build_groups(rows, 4, stop, DEFAULT_ALGO, only_extra, lambda d, t: None)
    lines = [f"mode={'only_extra' if only_extra else 'all'} files={len(rows)} groups={len(groups)}",
             f"skipped={skipped}"]
    for g in groups:
        lines.append(f"group size={g['size']} n={len(g['members'])} hash={g['hash'][:12]}")
        for m in g["members"]:
            tag = "KEPT" if m["kept"] else ("drop" if m["deletable"] else "----")
            lines.append(f"  {tag} [{m['src']}] {m['path']}")
    if apply_dst:
        plan = plan_removal(groups)
        res = execute_removal(plan, apply_dst, stop, lambda d, t: None, DEFAULT_ALGO, only_extra)
        lines.append(f"removed ok={res['ok']} fail={res['fail']} copied={res['copied']}")
    out = os.path.join(os.environ.get("TEMP", "."), "sfc_selftest.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        extra = sys.argv[sys.argv.index("--extra") + 1] if "--extra" in sys.argv else None
        dst = sys.argv[sys.argv.index("--apply") + 1] if "--apply" in sys.argv else None
        only = "--all" not in sys.argv
        return _selftest(sys.argv[i + 1], extra, dst, only)
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
