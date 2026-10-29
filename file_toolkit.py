#!/usr/bin/env python3
"""File Toolkit：两个工具合一。
Tab1 查重清理 —— 递归/双目录查重，重复件移动到待移除目录。
Tab2 关键字整理 —— 单层目录，按关键字与重复情况整理到目标目录（两阶段计划表）。
"""

import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import config
import photo_media
from fsops import long_path as _long, transfer
from media_page import MediaPage
from photo_page import PhotoPage

APP = "File Toolkit"
VER = "20261029"  # 版本号＝交付日期（同日多次交付加 a/b），每次交付改这一行
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

_bench_buf = None


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


def _is_drive_root(path):
    p = os.path.abspath(path).rstrip("\\/")
    return len(p) <= 2 or p.endswith(":")


def preflight(dst, sources, dst_label="目标目录", must_be_empty=False, need_bytes=None):
    """收集"会阻止执行"的问题；返回列表，空列表表示可以执行。

    sources 是 [(标签, 路径), ...]，用来检查目标目录不能与来源目录相同或互相包含。
    各页签只声明自己的要求，不再各写一套判断。
    """
    if not dst:
        return ["没有指定%s" % dst_label]
    dst_abs = os.path.abspath(dst)
    problems = []
    if must_be_empty and _is_drive_root(dst_abs):
        problems.append("%s不能是盘根：%s" % (dst_label, dst_abs))
    for label, path in sources:
        if not path:
            continue
        if _norm(path) == _norm(dst_abs):
            problems.append("「%s」与「%s」不能是同一个目录" % (dst_label, label))
        elif _overlap(dst_abs, path):
            problems.append("「%s」与「%s」不能互相包含（一个是另一个的子目录）"
                            % (dst_label, label))
    if must_be_empty and os.path.isdir(dst_abs) and os.listdir(dst_abs):
        problems.append("%s必须为空：%s" % (dst_label, dst_abs))
    if need_bytes is not None:
        try:
            free = shutil.disk_usage(dst_abs).free
        except OSError as e:
            problems.append("%s不可用：%s" % (dst_label, e))
        else:
            if free < need_bytes:
                problems.append("%s所在盘空间不足：需要 %s，可用 %s"
                                % (dst_label, human_size(need_bytes), human_size(free)))
    return problems


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


def _skip_reason(entry, st, min_bytes, include_hidden):
    if _is_attr(st, _ATTR_REPARSE):
        return "链接/联接点"
    if not include_hidden and (_is_attr(st, _ATTR_HIDDEN) or _is_attr(st, _ATTR_SYSTEM)):
        return "隐藏/系统"
    if entry.name.lower().endswith(_SKIP_EXT):
        return "快捷方式"
    if st.st_size < min_bytes:
        return "小于阈值"
    return None


def scan_flat(root, min_bytes, include_hidden, stop, on_progress=None):
    rows, skipped = [], defaultdict(int)
    try:
        with os.scandir(_long(root)) as it:
            entries = list(it)
    except OSError:
        return rows, {"目录无法访问": 1}
    for entry in entries:
        if stop.is_set():
            break
        try:
            st = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                continue
            if not entry.is_file(follow_symlinks=False):
                skipped["非普通文件"] += 1
                continue
            reason = _skip_reason(entry, st, min_bytes, include_hidden)
            if reason:
                skipped[reason] += 1
                continue
            rows.append({"path": entry.path, "size": st.st_size, "mtime": st.st_mtime,
                         "hash": None, "src": SRC_BASE, "depth": 0})
        except OSError:
            skipped["读取失败"] += 1
    if on_progress:
        on_progress(len(rows))
    return rows, dict(skipped)


def scan_tree(root, min_bytes, include_hidden, stop, src, on_progress):
    rows, skipped = [], defaultdict(int)
    stack = [(root, 0)]
    while stack:
        if stop.is_set():
            break
        cur, depth = stack.pop()
        try:
            with os.scandir(_long(cur)) as it:
                entries = list(it)
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
                    stack.append((entry.path, depth + 1))
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
                rows.append({"path": entry.path, "size": st.st_size, "mtime": st.st_mtime,
                             "hash": None, "src": src, "depth": depth})
            except OSError:
                skipped["读取失败"] += 1
        on_progress(len(rows))
    return rows, dict(skipped)


def hash_items(items, workers, stop, algo, on_progress):
    if not items:
        return 0
    total = len(items)
    state = {"done": 0, "fail": 0}
    lock = threading.Lock()

    def one(it):
        if stop.is_set():
            return
        bad = False
        try:
            it["hash"] = digest_file(it["path"], algo, stop)
        except OSError:
            it["hash"] = None
            bad = True
        with lock:
            state["done"] += 1
            if bad:
                state["fail"] += 1
            n = state["done"]
        if n % 20 == 0 or n == total:
            on_progress(n, total)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, items))
    return state["fail"]


def group_by_hash(items):
    by_size = defaultdict(list)
    for r in items:
        by_size[r["size"]].append(r)
    return [g for g in by_size.values() if len(g) > 1]


# ---------------------------------------------------------------- 查重清理页内核

def checker_rank(item):
    return (0 if item["src"] == SRC_BASE else 1,
            1 if _TODO in item["path"].lower() else 0,
            item["depth"], item["mtime"], item["path"].lower())


def checker_groups(rows, workers, stop, algo, only_extra, on_hash, stat=None):
    cands = group_by_hash(rows)
    flat = [r for g in cands for r in g]
    on_hash(0, len(flat))
    fail = hash_items(flat, workers, stop, algo, on_hash)
    if stat is not None and fail:
        stat["哈希失败"] = fail

    by_hash = defaultdict(list)
    for r in flat:
        if r["hash"]:
            by_hash[(r["size"], r["hash"])].append(r)

    groups = []
    for (size, h), members in by_hash.items():
        if len(members) < 2:
            continue
        for m in members:
            m.update({"kept": False, "deletable": False, "checked": False})
        members.sort(key=checker_rank)
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
    return groups


def checker_plan(groups):
    plan = []
    for g in groups:
        moves = [m for m in g["members"] if m["checked"] and m["deletable"]]
        if moves:
            plan.append({"group": g, "moves": moves, "kept": g["keeper"]})
    return plan


def checker_space(plan):
    need = sum(m["size"] for p in plan for m in p["moves"])
    need += sum(p["kept"]["size"] for p in plan)
    return need, sum(p["kept"]["size"] for p in plan)


def checker_execute(plan, dst, stop, on_progress, algo, only_extra):
    os.makedirs(_long(dst), exist_ok=True)
    mode_txt = "仅比对额外目录" if only_extra else "全量去重"
    manifest = [f"{APP} v{VER}  移除清单  {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"算法 {algo}   模式 {mode_txt}", f"目标目录 {dst}", ""]
    ok = fail = copied = skipped = done = 0
    total = sum(len(p["moves"]) for p in plan)
    for gi, item in enumerate(plan, 1):
        if stop.is_set():
            break
        g, kept, moves = item["group"], item["kept"], item["moves"]
        folder = os.path.join(dst, g["hash"][:16])
        try:
            os.makedirs(_long(folder), exist_ok=True)
        except OSError as e:
            manifest.append(f"[组{gi}] 创建目录失败，本组 {len(moves)} 个文件未尝试：{folder}: {e}")
            skipped += len(moves)
            continue
        manifest.append(f"[组{gi}] 哈希 {g['hash']}  大小 {human_size(g['size'])}  份数 {len(g['members'])}")
        manifest.append(f"  保留: [{'基础' if kept['src'] == SRC_BASE else '额外'}] {kept['path']}")
        ktarget = _unique_path(folder, "[保留] " + os.path.basename(kept["path"]))
        good, why = transfer(kept["path"], ktarget, "copy")
        if good:
            copied += 1
            manifest.append(f"  副本: {ktarget}")
        else:
            manifest.append(f"  副本失败: {why}")
        for m in moves:
            if stop.is_set():
                break
            tag = "基础" if m["src"] == SRC_BASE else "额外"
            target = _unique_path(folder, os.path.basename(m["path"]))
            good, why = transfer(m["path"], target)
            if good:
                ok += 1
                manifest.append(f"  待删[{tag}]: {m['path']}\n     -> {target}")
            else:
                fail += 1
                manifest.append(f"  待删[{tag}]: {m['path']}\n     -> 失败: {why}")
            done += 1
            on_progress(done, total)
        manifest.append("")
    if not stop.is_set():
        on_progress(total, total)
    summary = {"ok": ok, "fail": fail, "skipped": skipped, "copied": copied,
               "groups": len(plan), "total": total, "stopped": stop.is_set()}
    manifest.append(f"结果：移动成功 {ok}，失败 {fail}，未尝试 {skipped}，副本 {copied}"
                    + ("（已中断）" if stop.is_set() else ""))
    try:
        with open(_long(os.path.join(dst, "清单.txt")), "w", encoding="utf-8") as f:
            f.write("\n".join(manifest) + "\n")
    except OSError:
        pass
    return summary


# ---------------------------------------------------------------- 关键字整理页内核

_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,6})(?![A-Za-z])")
_DIGIT = re.compile(r"(?<![0-9])([0-9]{3,6})(?![0-9])")
_NUM_MARK = re.compile(r"[\(（]\s*(\d{1,2})\s*[\)）]")
_CP_MARK = re.compile(r"(?i)(?<![A-Za-z])(copy|副本)(?![A-Za-z])")

_KEYWORD_HINT = "留空＝默认规则（3~6 位字母 + 其后 3~6 位数字，拼接后转小写）"


def compile_keyword_rule(text):
    """把用户输入编译成正则；空串返回 None（表示用内置默认规则）。"""
    text = (text or "").strip()
    if not text:
        return None
    return re.compile(text, re.IGNORECASE)


def extract_keyword(stem, prog=None):
    """取关键字：prog 为空走内置规则，否则取用户正则的第一个匹配。"""
    if prog is not None:
        m = prog.search(stem)
        if not m:
            return ""
        parts = m.groups() or (m.group(0),)
        return "".join(p for p in parts if p).lower()
    for d in _DIGIT.finditer(stem):
        letters = None
        for m in _LETTER.finditer(stem[:d.start()]):
            letters = m
        if letters:
            return (letters.group(1) + d.group(1)).lower()
    return ""


def copy_mark(stem):
    nums = [int(m.group(1)) for m in _NUM_MARK.finditer(stem) if m.group(1)]
    if nums:
        return True, max(nums)
    if _CP_MARK.search(stem):
        return True, 1
    return False, 0


def dedup_rank(item, keep):
    has, num = item["mark"], item["mark_num"]
    mtime = item["mtime"] if keep == "最早优先" else -item["mtime"]
    return (1 if has else 0, num, mtime, -len(item["name"]))


def dedup_analyze(src, dst, quarantine, workers, min_bytes, keep, algo,
                  rule="", on_log=print, on_progress=None, cancel=None):
    src = os.path.abspath(src)
    stop = cancel or threading.Event()
    rows, skipped = scan_flat(src, min_bytes, False, stop)
    on_log(f"[i] 扫描 {src}")
    on_log(f"[i] 纳入 {len(rows)} 个文件，跳过 {sum(skipped.values())} 个 {skipped or ''}")
    if not rows:
        raise RuntimeError("源目录没有可处理的文件")
    prog = compile_keyword_rule(rule)
    for r in rows:
        r["name"] = os.path.basename(r["path"])
        r["stem"] = Path(r["name"]).stem
        r["kw"] = extract_keyword(r["stem"], prog)
        r["mark"], r["mark_num"] = copy_mark(r["stem"])

    cands = group_by_hash(rows)
    flat = [r for g in cands for r in g]
    on_log(f"[i] 大小分组 {len(cands)} 组，需计算哈希 {len(flat)} 个文件")
    if flat:
        on_log("[i] 计算哈希 ...")
        hash_items(flat, max(1, workers), stop, algo,
                   lambda d, t: on_progress(d, t) if on_progress else None)

    groups = []
    for g in cands:
        sub = defaultdict(list)
        for it in g:
            if it.get("hash"):
                sub[it["hash"]].append(it)
        for h, members in sub.items():
            if len(members) > 1:
                members.sort(key=lambda x: dedup_rank(x, keep))
                groups.append({"sha": h, "members": members})
    groups.sort(key=lambda x: (-x["members"][0]["size"], x["members"][0]["name"]))
    on_log(f"[i] 完全相同的文件组 {len(groups)} 组，多余副本 "
           f"{sum(len(g['members']) - 1 for g in groups)} 个")

    in_group = {}
    for gi, g in enumerate(groups, 1):
        for rank, it in enumerate(g["members"], 1):
            in_group[it["path"]] = (gi, rank, len(g["members"]))

    plan_rows = []
    for it in rows:
        gid, rank = in_group.get(it["path"], ("", ""))[:2]
        if gid and rank > 1:
            act = "隔离"
        elif it["kw"] or gid:
            act = "移动"
        else:
            act = "不动"
        plan_rows.append({"动作": act, "文件名": it["name"], "源路径": it["path"],
                          "关键字": it["kw"]})

    cnt = defaultdict(int)
    for r in plan_rows:
        cnt[r["动作"]] += 1
    kw_hit = sum(1 for i in rows if i["kw"])
    on_log(f"[i] 重复组 {len(groups)} 组；关键字命中 {kw_hit} 个，将移动 {cnt['移动']} 个；"
           f"内容重复 {cnt['隔离']} 个；不动 {cnt['不动']} 个")
    if stop.is_set():
        on_log("[i] 已停止，结果不完整")
    return {"rows": plan_rows, "counts": dict(cnt), "groups": len(groups),
            "items": len(rows), "dst": dst, "quar": quarantine,
            "skipped": dict(skipped), "stopped": stop.is_set()}


def dedup_execute(rows, dst, quarantine, on_log=print, on_progress=None, cancel=None):
    """按内存里的计划移动文件：只移动，不删除、不改名；目标有同名则跳过。"""
    dst, quarantine = os.path.abspath(dst), os.path.abspath(quarantine)
    todo = [r for r in rows if r["动作"] in ("隔离", "移动")]
    total = len(todo)
    on_log(f"[i] 待移动 {total} 个文件（命中关键字 + 内容重复）")
    if not total:
        return {"moved": 0, "skipped": 0, "failed": 0, "stopped": False, "errors": []}
    for d in (dst, quarantine):
        os.makedirs(_long(d), exist_ok=True)
    moved = skipped = failed = 0
    errors = []
    for i, r in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            break
        base = quarantine if r["动作"] == "隔离" else dst
        src_path = r["源路径"]
        target = os.path.join(base, r["文件名"])
        if os.path.exists(_long(target)):
            skipped += 1
            on_log(f"[i] 跳过（目标已有同名）：{r['文件名']}")
            continue
        good, why = transfer(src_path, target)
        if good:
            moved += 1
        else:
            failed += 1
            errors.append({"源路径": src_path, "错误": why})
            on_log(f"[!] 失败 {r['文件名']}：{why}")
        if on_progress and (i % 50 == 0 or i == total):
            on_progress(i, total)
    stopped = bool(cancel is not None and cancel.is_set())
    on_log(f"[OK] 移动完成：成功 {moved}，跳过 {skipped}，失败 {failed}"
           + ("（已中断）" if stopped else ""))
    return {"moved": moved, "skipped": skipped, "failed": failed,
            "stopped": stopped, "errors": errors}


def benchmark_algo(algo):
    buf = _bench_buf
    h = ALGOS[algo]()
    t = time.perf_counter()
    h.update(buf)
    dt = time.perf_counter() - t
    return len(buf) / dt / 1048576.0 if dt else 0.0


# ---------------------------------------------------------------- 界面

class CheckerPage:
    tag = "chk"

    def __init__(self, parent, app):
        self.app = app
        self.ttk, self.tk = app.ttk, app.tk
        self.groups = []
        self.n_base = self.n_extra = 0
        self.started = 0.0
        self._reset_sel()
        self._build(parent)
        self._load_cfg()

    def _build(self, parent):
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
        ttk.Button(bottom, text="全选", width=6,
                   command=lambda: self._set_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bottom, text="全不选", width=7,
                   command=lambda: self._set_all(False)).pack(side="left", padx=6)
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
        self.btn_remove.config(state="normal" if (self.groups and same) else "disabled")

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
        if _is_drive_root(base):
            self.app.mb.showerror(APP, "暂不支持以盘根作为基础目录，请选择盘内的具体目录（整盘扫描请分段进行）")
            return
        if extra:
            if not os.path.isdir(extra):
                self.app.mb.showerror(APP, "额外目录无效")
                return
            if _overlap(base, extra):
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
                self.n_base = len(rows)
                if extra and not stop.is_set():
                    more, sk2 = scan_tree(os.path.abspath(extra), min_bytes, hidden, stop,
                                          SRC_EXTRA,
                                          lambda n: post(self.tag, "scan", ("额外", self.n_base + n)))
                    self.n_extra = len(more)
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
                post(self.tag, "done", (groups, skipped, only_extra, bool(extra)))
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

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

    def _render(self, groups, skipped, only_extra, has_extra):
        self.app.release()
        self.groups = groups
        self._reset_sel()
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
                    mark, "基础" if m["src"] == SRC_BASE else "额外",
                    len(g["members"]) if i == 0 else "",
                    human_size(g["size"]) if i == 0 else "",
                    g["hash"] if i == 0 else "", m["path"],
                ), tags=(tag,))
        waste = self._sel_size
        used = time.time() - self.started
        skip_txt = "，".join(f"{k} {v}" for k, v in sorted(skipped.items()))
        stopped = "（已停止，结果不完整）" if self.app.stop.is_set() else ""
        mode = "仅比对额外目录" if only_extra else "全量去重"
        self.app.progress_done()
        self.app.set_status(f"完成{stopped}：基础 {self.n_base} 个 / 额外 {self.n_extra} 个文件，"
                            f"耗时 {used:.1f}s，{mode}，算法 {self.app.var_algo.get()}")
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
        if not groups:
            hint = "未发现基础目录与额外目录重复的文件" if has_extra else "未发现重复文件"
            self.app.set_status(f"{hint}{stopped}")

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
        if not self.groups:
            return
        for gi, m in self._by_path.values():
            self._set_checked(gi, m, flag)
        self._update_stats()

    def _on_click(self, ev):
        if self.app.busy:
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
        try:
            os.makedirs(_long(dst), exist_ok=True)
        except OSError as e:
            self.app.mb.showerror(APP, f"待移除目录不可用：{e}")
            return
        need, copy_size = checker_space(plan)
        problems = preflight(dst, [("基础目录", base), ("额外目录", extra)],
                             dst_label="待移除目录", must_be_empty=True, need_bytes=need)
        if problems:
            self.app.mb.showerror(APP, "以下问题会阻止执行：\n\n· " + "\n· ".join(problems))
            return
        n = sum(len(p["moves"]) for p in plan)
        msg = (f"即将执行移动（不是删除）：\n\n"
               f"· 移动 {n} 个文件 → {dst}\n"
               f"· 涉及 {len(plan)} 个哈希组\n"
               f"· 每组复制一份保留件到组目录，供人工对比（占用 {human_size(copy_size)}）\n"
               f"· 需要空间约 {human_size(need)}\n\n⚠️ 确认继续？")
        if not self.app.mb.askyesno("确认移除", msg, icon="warning"):
            return
        if not self.app.acquire(self):
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

        threading.Thread(target=work, daemon=True).start()

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
        self.stats.config(text=f"清单已写入 {os.path.join(self.var_out.get(), '清单.txt')}"
                               f"　|　对比完成后可手动删除整个待移除目录")
        if r["fail"]:
            self.app.mb.showwarning(APP, f"有 {r['fail']} 个文件移动失败，详见 清单.txt")

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


class DedupPage:
    tag = "ded"

    def __init__(self, parent, app):
        self.app = app
        self.ttk, self.tk = app.ttk, app.tk
        self.cfg = app.cfg                 # 与 App 共用同一份配置字典
        self.cancel = threading.Event()
        self._build(parent)
        self._load_cfg()

    def _build(self, parent):
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

        rule_row = ttk.Frame(wrap)
        rule_row.pack(fill="x", pady=(8, 0))
        ttk.Label(rule_row, text="关键字正则", width=13).pack(side="left")
        self.var_rule = tk.StringVar(
            value=str(config.get_value(self.cfg, "dedup.keyword_rule", "")))
        entry = ttk.Entry(rule_row, textvariable=self.var_rule)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<FocusOut>", self._on_rule_change)
        entry.bind("<Return>", self._on_rule_change)
        ttk.Button(rule_row, text="恢复默认", width=10,
                   command=self._reset_rule).pack(side="left")

        self.lbl_rule = ttk.Label(wrap, text="", foreground="#5F5E5A")
        self.lbl_rule.pack(fill="x", pady=(2, 0))

        opts = ttk.Frame(wrap)
        opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="最小(KB)").pack(side="left")
        self.var_minsize = tk.IntVar(value=0)
        ttk.Spinbox(opts, from_=0, to=1048576, width=8,
                    textvariable=self.var_minsize).pack(side="left", padx=(6, 18))
        ttk.Label(opts, text="重复文件保留").pack(side="left")
        self.var_keep = tk.StringVar(value="最早优先")
        ttk.Combobox(opts, width=10, state="readonly", textvariable=self.var_keep,
                     values=["最早优先", "最新优先"]).pack(side="left", padx=6)
        ttk.Label(opts, text="（带 (2)/副本/copy 标记的优先算作重复）",
                  foreground="#5F5E5A").pack(side="left", padx=(10, 0))

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
        self.lbl_rule.config(text="自定义正则：取第一个匹配，多分组按顺序拼接，结果转小写",
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
                self.var_dst.set(b + "_sorted")
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

        threading.Thread(target=work, daemon=True).start()

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

        threading.Thread(target=work, daemon=True).start()

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
        self.chk.save_cfg()
        self.ded.save_cfg()
        config.save(self.cfg)

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
        """把界面上的路径交给 photo_media；空或不存在时它自己会退回自动查找。"""
        photo_media.set_tool_path("ffprobe", self.var_ffprobe.get())

    def _build(self):
        ttk, tk = self.ttk, self.tk
        outer = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        outer.pack(fill="both", expand=True)

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Label(bar, text="算法").pack(side="left")
        self.var_algo = tk.StringVar(value=DEFAULT_ALGO)
        ttk.Combobox(bar, width=11, state="readonly", textvariable=self.var_algo,
                     values=list(ALGOS.keys())).pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="性能测试", width=9, command=self._bench).pack(side="left")
        ttk.Label(bar, text="线程").pack(side="left", padx=(16, 0))
        self.var_workers = tk.IntVar(value=4)
        ttk.Spinbox(bar, from_=1, to=32, width=5, textvariable=self.var_workers).pack(side="left", padx=6)

        # ffprobe 可选指定（不指定就在 exe 同级 / tools 子目录里找；都没有就退回内置解析）
        ttk.Label(bar, text="ffprobe").pack(side="left", padx=(16, 0))
        self.var_ffprobe = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var_ffprobe, width=26)
        entry.pack(side="left", padx=6)
        entry.bind("<FocusOut>", self._apply_ffprobe)
        entry.bind("<Return>", self._apply_ffprobe)
        ttk.Button(bar, text="选择…", width=7,
                   command=self._pick_ffprobe).pack(side="left")
        ttk.Button(bar, text="清空", width=6,
                   command=self._clear_ffprobe).pack(side="left", padx=(4, 0))

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
                                          f"查重清理 · 查重(关键字) · 媒体索引 · 照片重命名　|　任一处任务运行时其他页签暂停")
        self.lbl.pack(fill="x")

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(10, 0))
        self.chk = CheckerPage(self.nb, self)
        self.ded = DedupPage(self.nb, self)
        self.med = None                 # 首次切到该页签才建库，见 _ensure_media
        self.med_holder = ttk.Frame(self.nb)
        self.med_failed = False
        self.photo = PhotoPage(self.nb, self)
        self.nb.add(self.chk.frame, text="  查重清理  ")
        self.nb.add(self.ded.frame, text="  查重(关键字)  ")
        self.nb.add(self.med_holder, text="  媒体索引  ")
        self.nb.add(self.photo.frame, text="  照片重命名  ")
        self.pages = {"chk": self.chk, "ded": self.ded, "photo": self.photo}
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
        for p in (self.chk, self.ded, self.med, self.photo):
            if p is not None:
                p.set_enabled(False)
        return True

    def release(self):
        owner = self.owner
        self.busy = False
        self.owner = None
        for p in (self.chk, self.ded, self.med, self.photo):
            if p is not None:
                p.set_enabled(True)
        if getattr(owner, "tag", "") != "med":
            self.pb.stop()
            self.pb.config(mode="determinate")
            self.pb["value"] = 100
        self.chk.btn_stop.config(state="disabled")

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
        global _bench_buf
        ttk, tk = self.ttk, self.tk
        win = tk.Toplevel(self.root)
        win.title("算法性能测试")
        win.geometry("460x320")
        win.transient(self.root)
        ttk.Label(win, text=f"用 {_BENCH_MB} MB 内存数据测纯 CPU 吞吐（不含磁盘 I/O）",
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
            global _bench_buf
            if _bench_buf is None:
                _bench_buf = os.urandom(_BENCH_MB * 1024 * 1024)
            results = []
            for name in ALGOS:
                mb = benchmark_algo(name)
                results.append((name, mb))
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


# ---------------------------------------------------------------- 入口

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
