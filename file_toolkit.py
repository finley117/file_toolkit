#!/usr/bin/env python3
"""File Toolkit：两个工具合一。
Tab1 查重清理 —— 递归/双目录查重，重复件移动到待移除目录。
Tab2 关键字整理 —— 单层目录，按关键字与重复情况整理到目标目录（两阶段计划表）。
"""

import csv
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

from media_page import MediaPage
from photo_page import PhotoPage

APP = "File Toolkit"
VER = "20261028b"  # 版本号＝交付日期（同日多次交付加 a/b），每次交付改这一行
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


def _under(path, root):
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


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


def checker_groups(rows, workers, stop, algo, only_extra, on_hash):
    cands = group_by_hash(rows)
    flat = [r for g in cands for r in g]
    on_hash(0, len(flat))
    hash_items(flat, workers, stop, algo, on_hash)

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
    os.makedirs(dst, exist_ok=True)
    mode_txt = "仅比对额外目录" if only_extra else "全量去重"
    manifest = [f"{APP} v{VER}  移除清单  {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"算法 {algo}   模式 {mode_txt}", f"目标目录 {dst}", ""]
    ok = fail = copied = done = 0
    total = sum(len(p["moves"]) for p in plan)
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


# ---------------------------------------------------------------- 关键字整理页内核

_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,6})(?![A-Za-z])")
_DIGIT = re.compile(r"(?<![0-9])([0-9]{3,6})(?![0-9])")
_NUM_MARK = re.compile(r"[\(（]\s*(\d{1,2})\s*[\)）]")
_CP_MARK = re.compile(r"(?i)(?<![A-Za-z])(copy|副本)(?![A-Za-z])")

PLAN_FIELDS = ["组号", "名次", "动作", "文件名", "关键字", "副本标记", "大小(B)",
               "修改时间", "哈希", "源路径", "目标路径", "备注"]
MANIFEST_FIELDS = ["时间", "动作", "原路径", "新路径", "关键字", "副本标记",
                   "大小(B)", "哈希", "组号", "备注"]


def extract_keyword(stem):
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
                  on_log=print, on_progress=None):
    src = os.path.abspath(src)
    stop = threading.Event()
    rows, skipped = scan_flat(src, min_bytes, False, stop)
    on_log(f"[i] 扫描 {src}")
    on_log(f"[i] 纳入 {len(rows)} 个文件，跳过 {sum(skipped.values())} 个 {skipped or ''}")
    if not rows:
        raise RuntimeError("源目录没有可处理的文件")
    for r in rows:
        r["name"] = os.path.basename(r["path"])
        r["stem"] = Path(r["name"]).stem
        r["kw"] = extract_keyword(r["stem"])
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
        gid, rank, total = in_group.get(it["path"], ("", "", ""))
        note = []
        if gid:
            note.append("重复组保留件" if rank == 1 else f"重复组第{rank}份/共{total}份")
        if it["kw"]:
            note.append(f"关键字={it['kw']}")
        if gid and rank > 1:
            act, target = "隔离", os.path.join(quarantine, it["name"])
        elif gid and rank == 1:
            act, target = "移动", os.path.join(dst, it["name"])
        elif it["kw"]:
            act, target = "移动", os.path.join(dst, it["name"])
        else:
            act, target = "不动", ""
        plan_rows.append({
            "组号": gid, "名次": rank, "动作": act, "文件名": it["name"],
            "关键字": it["kw"], "副本标记": "是" if it["mark"] else "",
            "大小(B)": it["size"],
            "修改时间": datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d %H:%M:%S"),
            "哈希": it.get("hash", ""), "源路径": it["path"],
            "目标路径": target, "备注": "；".join(note),
        })

    kw_stat = defaultdict(int)
    for it in rows:
        if it["kw"]:
            kw_stat[it["kw"]] += 1

    report_dir = os.path.abspath(src + "_dedup_report")
    os.makedirs(report_dir, exist_ok=True)
    plan_path = os.path.join(report_dir, "plan.csv")
    with open(plan_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS)
        w.writeheader()
        w.writerows(plan_rows)

    cnt = defaultdict(int)
    for r in plan_rows:
        cnt[r["动作"]] += 1
    waste = sum(g["members"][0]["size"] * (len(g["members"]) - 1) for g in groups)
    summary = [
        f"源目录      : {src}",
        f"目标目录    : {dst}",
        f"隔离区      : {quarantine}",
        f"哈希算法    : {algo}",
        f"文件总数    : {len(rows)}（跳过 {sum(skipped.values())}）",
        f"总容量      : {sum(i['size'] for i in rows) / 1048576:.1f} MB",
        f"重复组      : {len(groups)} 组，副本 {cnt['隔离']} 个，可回收 {waste / 1048576:.1f} MB",
        f"关键字命中  : {sum(1 for i in rows if i['kw'])} 个文件，{len(kw_stat)} 个不同关键字",
        f"待移动      : {cnt['移动']} 个（保留件 + 命中件）",
        f"不动        : {cnt['不动']} 个",
        "",
        "关键字 TOP 10：",
    ]
    for k, v in sorted(kw_stat.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        summary.append(f"  {k:<24} {v:>6} 个文件")
    text = "\n".join(summary)
    with open(os.path.join(report_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    for line in summary:
        on_log(line)
    on_log(f"[OK] 计划已生成：{plan_path}")
    return {"report_dir": report_dir, "plan": plan_path, "rows": plan_rows,
            "counts": dict(cnt), "groups": len(groups), "items": len(rows),
            "dst": dst, "quar": quarantine}


def dedup_apply(plan_path, dst, quarantine, on_log=print, rebase=None):
    with open(plan_path, "r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("计划表为空")
    dst, quarantine = os.path.abspath(dst), os.path.abspath(quarantine)
    old_dst, old_quar = rebase if rebase else (None, None)
    for d in (dst, quarantine):
        if os.path.isdir(d) and os.listdir(d):
            raise RuntimeError(f"目标目录非空，为避免覆盖已中止：{d}")
        os.makedirs(d, exist_ok=True)

    todo = [r for r in rows if r["动作"].strip() in ("隔离", "移动")]
    on_log(f"[i] 待执行 {len(todo)} 个文件（隔离 + 移动）")
    manifest_path = os.path.join(os.path.dirname(plan_path), "manifest.csv")
    failed, done = [], 0
    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for r in todo:
            src_path = r["源路径"]
            act = r["动作"].strip()
            plan_target = (r.get("目标路径") or "").strip()
            base_dir = quarantine if act == "隔离" else dst
            if plan_target and old_dst and _under(plan_target, old_dst):
                plan_target = os.path.join(dst, os.path.relpath(plan_target, old_dst))
            elif plan_target and old_quar and _under(plan_target, old_quar):
                plan_target = os.path.join(quarantine, os.path.relpath(plan_target, old_quar))
            if plan_target:
                target = plan_target if os.path.isabs(plan_target) else os.path.join(base_dir, plan_target)
            else:
                target = os.path.join(base_dir, os.path.basename(src_path))
            rec = {
                "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "动作": act,
                "原路径": src_path, "新路径": target, "关键字": r.get("关键字", ""),
                "副本标记": r.get("副本标记", ""), "大小(B)": r.get("大小(B)", ""),
                "哈希": r.get("哈希", ""), "组号": r.get("组号", ""), "备注": r.get("备注", ""),
            }
            try:
                if not os.path.isfile(_long(src_path)):
                    raise FileNotFoundError("源文件已不存在")
                os.makedirs(_long(os.path.dirname(target)), exist_ok=True)
                if os.path.exists(_long(target)):
                    raise FileExistsError("目标位置已存在同名文件")
                shutil.move(_long(src_path), _long(target))
                done += 1
                rec["备注"] = (rec["备注"] + "；" if rec["备注"] else "") + "成功"
            except Exception as e:
                rec["备注"] = (rec["备注"] + "；" if rec["备注"] else "") + f"失败：{e}"
                failed.append({"源路径": src_path, "错误": str(e)})
            w.writerow(rec)
            if done and done % 100 == 0:
                on_log(f"[i] 已处理 {done}/{len(todo)}")
    if failed:
        fp = os.path.join(os.path.dirname(plan_path), "failed.csv")
        with open(fp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["源路径", "错误"])
            w.writeheader()
            w.writerows(failed)
        on_log(f"[!] 失败 {len(failed)} 个，明细：{fp}")
    on_log(f"[OK] 执行完成：成功 {done} 个，失败 {len(failed)} 个")
    return {"moved": done, "failed": failed, "manifest": manifest_path}


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
        self._build(parent)

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
        self.btn_remove = ttk.Button(bottom, text="移除", width=8,
                                     command=self._do_remove, state="disabled")
        self.btn_remove.pack(side="left")

        self.stats = ttk.Label(wrap, text="")
        self.stats.pack(fill="x", pady=(6, 0))

        self.log = tk.Text(wrap, height=6, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(6, 0))

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

    def _start(self):
        base = self.var_dir.get().strip()
        extra = self.var_extra.get().strip()
        if not base or not os.path.isdir(base):
            self.app.mb.showerror(APP, "请先选择有效的基础目录")
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
                    groups = checker_groups(rows, workers, stop, algo, only_extra,
                                            lambda d, t: post(self.tag, "hash", (d, t)))
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
        waste = sum(m["size"] for g in groups for m in g["members"]
                    if m["checked"] and m["deletable"])
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
        self.stats.config(
            text=f"重复 {len(groups)} 组 / 待移除可回收 {human_size(waste)}"
                 f"　|　跳过：{skip_txt or '无'}　|　★ 保留　– 不处理　☑ 待移除（单击切换）　双击定位")
        if groups:
            self.btn_remove.config(state="normal")
        else:
            hint = "未发现基础目录与额外目录重复的文件" if has_extra else "未发现重复文件"
            self.app.set_status(f"{hint}{stopped}")

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
        if self.app.busy:
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
        plan = checker_plan(self.groups)
        n = sum(len(p["moves"]) for p in plan)
        size = sum(m["size"] for p in plan for m in p["moves"])
        copy_size = sum(p["kept"]["size"] for p in plan)
        self.stats.config(
            text=f"待移除 {n} 个文件 / {len(plan)} 组 / 释放 {human_size(size)}"
                 f"　|　保留件副本占额外 {human_size(copy_size)}"
                 f"　|　★ 保留　– 不处理　☑ 待移除（单击切换）　双击定位")

    def _do_remove(self):
        plan = checker_plan(self.groups)
        if not plan:
            self.app.mb.showinfo(APP, "没有勾选任何待移除文件")
            return
        base = self.var_dir.get().strip()
        extra = self.var_extra.get().strip()
        dst = self.var_out.get().strip()
        if not dst:
            self.app.mb.showwarning(APP, "请先指定待移除目录")
            return
        dst = os.path.abspath(dst)
        for p in [base] + ([extra] if extra else []):
            if _overlap(dst, p):
                self.app.mb.showerror(APP, "待移除目录不能位于基础目录或额外目录内部")
                return
        if os.path.isdir(dst) and os.listdir(dst):
            self.app.mb.showerror(APP, f"待移除目录非空，为避免混入旧数据已中止：\n{dst}")
            return
        try:
            os.makedirs(dst, exist_ok=True)
            need, copy_size = checker_space(plan)
            free = shutil.disk_usage(dst).free
        except OSError as e:
            self.app.mb.showerror(APP, f"目标目录不可用：{e}")
            return
        if free < need:
            self.app.mb.showerror(APP, f"目标盘空间不足。\n需要 {human_size(need)}，可用 {human_size(free)}")
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
        only_extra = bool(self.var_only.get()) and bool(extra)
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
        self.app.set_status(f"移除完成：成功 {r['ok']}，失败 {r['fail']}，保留件副本 {r['copied']}"
                            + ("（已中断）" if r["stopped"] else ""))
        self._log("[OK] 移除完成：成功 %d，失败 %d，保留件副本 %d%s"
                  % (r["ok"], r["fail"], r["copied"], "（已中断）" if r["stopped"] else ""))
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
        self.result = None
        self._build(parent)

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
        for i, (label, var) in enumerate((("源目录", self.var_src), ("目标目录", self.var_dst),
                                          ("隔离区", self.var_quar))):
            ttk.Label(grid, text=label, width=8).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(grid, textvariable=var).grid(row=i, column=1, sticky="ew", padx=8, pady=3)
            ttk.Button(grid, text="选择…", width=8,
                       command=lambda v=var: self._pick(v)).grid(row=i, column=2, pady=3)

        opts = ttk.Frame(wrap)
        opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="最小(KB)").pack(side="left")
        self.var_minsize = tk.IntVar(value=0)
        ttk.Spinbox(opts, from_=0, to=1048576, width=8,
                    textvariable=self.var_minsize).pack(side="left", padx=(6, 18))
        ttk.Label(opts, text="保留规则").pack(side="left")
        self.var_keep = tk.StringVar(value="最早优先")
        ttk.Combobox(opts, width=10, state="readonly", textvariable=self.var_keep,
                     values=["最早优先", "最新优先"]).pack(side="left", padx=6)
        ttk.Label(opts, text="（单层扫描，不递归；副本标记 (2)/副本/copy 优先隔离）").pack(side="left", padx=(10, 0))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=10)
        self.btn_scan = ttk.Button(btns, text="① 分析（只读）", command=self._on_scan)
        self.btn_scan.pack(side="left")
        self.btn_apply = ttk.Button(btns, text="② 执行计划", command=self._on_apply, state="disabled")
        self.btn_apply.pack(side="left", padx=8)
        self.btn_open = ttk.Button(btns, text="打开报告目录", command=self._on_open, state="disabled")
        self.btn_open.pack(side="left")

        self.log = tk.Text(wrap, height=16, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def set_enabled(self, flag):
        self.btn_scan.config(state="normal" if flag else "disabled")
        if not flag:
            self.btn_apply.config(state="disabled")

    def _pick(self, var):
        d = self.app.fd.askdirectory()
        if d:
            var.set(os.path.normpath(d))
            if var is self.var_src:
                b = os.path.normpath(d)
                self.var_dst.set(b + "_sorted")
                self.var_quar.set(b + "_dup_quarantine")

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
            self.app.progress_det(d, t, f"哈希计算中 {d}/{t}（{self.app.var_algo.get()}）")
        elif kind == "done":
            self._scanned(payload)
        elif kind == "applied":
            self._applied(payload)
        elif kind == "error":
            self.app.release()
            self._log("[x] " + payload)
            self.app.set_status("出错，见日志")
            self.app.mb.showerror(APP, payload)

    def _on_scan(self):
        src = self.var_src.get().strip()
        if not src or not os.path.isdir(src):
            self.app.mb.showerror(APP, "请先选择有效的源目录")
            return
        dst = self.var_dst.get().strip()
        quar = self.var_quar.get().strip()
        if not dst:
            dst = os.path.abspath(src) + "_sorted"
        if not quar:
            quar = os.path.abspath(src) + "_dup_quarantine"
        self.var_dst.set(dst)
        self.var_quar.set(quar)
        if _norm(dst) == _norm(src) or _norm(quar) == _norm(src):
            self.app.mb.showerror(APP, "目标目录/隔离区不能与源目录相同")
            return
        try:
            workers = max(1, int(self.app.var_workers.get()))
            min_kb = max(0, int(self.var_minsize.get()))
        except Exception:
            self.app.mb.showerror(APP, "「最小(KB)」与「线程」必须填写数字")
            return
        if not self.app.acquire(self):
            return
        self.app.stop.clear()
        self.btn_apply.config(state="disabled")
        self.app.progress_start("分析中…（只读）")
        algo = self.app.var_algo.get()
        keep = self.var_keep.get()
        post = self.app.post

        def work():
            try:
                r = dedup_analyze(src, dst, quar, workers, min_kb * 1024, keep, algo,
                                  on_log=lambda m: post(self.tag, "log", m),
                                  on_progress=lambda d, t: post(self.tag, "prog", (d, t)))
                post(self.tag, "done", r)
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _scanned(self, r):
        self.app.release()
        self.result = r
        self.app.progress_done()
        c = r["counts"]
        self.app.set_status(f"分析完成：{r['items']} 个文件，{r['groups']} 个重复组，"
                            f"待移动 {c.get('移动', 0)}，待隔离 {c.get('隔离', 0)}")
        self.btn_apply.config(state="normal")
        self.btn_open.config(state="normal")

    def _on_apply(self):
        if not self.result:
            return
        src = self.var_src.get().strip()
        dst = self.var_dst.get().strip()
        quar = self.var_quar.get().strip()
        if not src:
            self.app.mb.showerror(APP, "源目录为空，请重新分析")
            return
        if not dst:
            dst = os.path.abspath(src) + "_sorted"
            self.var_dst.set(dst)
        if not quar:
            quar = os.path.abspath(src) + "_dup_quarantine"
            self.var_quar.set(quar)
        if _norm(dst) == _norm(src) or _norm(quar) == _norm(src):
            self.app.mb.showerror(APP, "目标目录/隔离区不能与源目录相同")
            return
        if _norm(dst) == _norm(quar):
            self.app.mb.showerror(APP, "目标目录与隔离区不能相同")
            return
        c = self.result["counts"]
        msg = (f"即将执行不可逆操作：\n\n"
               f"· 隔离 {c.get('隔离', 0)} 个重复副本 → {quar}\n"
               f"· 移动 {c.get('移动', 0)} 个文件 → {dst}\n\n"
               f"⚠️ 请先确认 plan.csv 内容正确！\n确定继续？")
        if not self.app.mb.askyesno("确认执行", msg, icon="warning"):
            self._log("[i] 已取消执行")
            return
        if not self.app.acquire(self):
            return
        r = self.result
        plan = r["plan"]
        rebase = (r.get("dst"), r.get("quar"))
        if rebase[0] and _norm(rebase[0]) != _norm(dst):
            self._log(f"[i] 目标目录已更改，计划表路径将按新目录改写：{rebase[0]} -> {dst}")
        if rebase[1] and _norm(rebase[1]) != _norm(quar):
            self._log(f"[i] 隔离区已更改，计划表路径将按新目录改写：{rebase[1]} -> {quar}")
        post = self.app.post

        def work():
            try:
                res = dedup_apply(plan, dst, quar, on_log=lambda m: post(self.tag, "log", m),
                                  rebase=rebase)
                post(self.tag, "applied", res)
            except Exception:
                post(self.tag, "error", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _applied(self, r):
        self.app.release()
        self.app.progress_done()
        self.app.set_status(f"执行完成：成功 {r['moved']} 个，失败 {len(r['failed'])} 个")
        self.btn_apply.config(state="disabled")

    def _on_open(self):
        if self.result:
            try:
                os.startfile(self.result["report_dir"])
            except Exception as e:
                self._log(f"[!] 打开失败：{e}")


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
        self._build()
        self.root.after(100, self._poll)

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

        # 底部状态区先从底部占位，窗口再小也不会被页签内容挤出可视区
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x")
        self.pb = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(10, 4))
        self.lbl = ttk.Label(bottom, text=f"File Toolkit v{VER}　|　"
                                          f"查重清理 · 关键字整理 · 媒体索引 · 照片重命名　|　任一处任务运行时其他页签暂停")
        self.lbl.pack(fill="x")

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(10, 0))
        self.chk = CheckerPage(self.nb, self)
        self.ded = DedupPage(self.nb, self)
        self.med = MediaPage(self.nb, self)
        self.photo = PhotoPage(self.nb, self)
        self.nb.add(self.chk.frame, text="  查重清理  ")
        self.nb.add(self.ded.frame, text="  关键字整理  ")
        self.nb.add(self.med.frame, text="  媒体索引  ")
        self.nb.add(self.photo.frame, text="  照片重命名  ")
        self.pages = {"chk": self.chk, "ded": self.ded, "med": self.med, "photo": self.photo}

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
        self.chk.set_enabled(False)
        self.ded.set_enabled(False)
        self.med.set_enabled(False)
        self.photo.set_enabled(False)
        return True

    def release(self):
        owner = self.owner
        self.busy = False
        self.owner = None
        self.chk.set_enabled(True)
        self.ded.set_enabled(True)
        self.med.set_enabled(True)
        self.photo.set_enabled(True)
        if getattr(owner, "tag", "") != "med":
            self.pb.stop()
            self.pb.config(mode="determinate")
            self.pb["value"] = 100
        self.chk.btn_stop.config(state="disabled")
        self.ded.btn_apply.config(state="normal" if self.ded.result else "disabled")

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


# ---------------------------------------------------------------- 命令行（回归验证用）

def _selftest_checker(base, extra=None, dst=None, only_extra=True):
    stop = threading.Event()
    rows, skipped = scan_tree(os.path.abspath(base), 1024, False, stop, SRC_BASE, lambda n: None)
    if extra:
        more, sk2 = scan_tree(os.path.abspath(extra), 1024, False, stop, SRC_EXTRA, lambda n: None)
        rows += more
        for k, v in sk2.items():
            skipped[k] = skipped.get(k, 0) + v
    groups = checker_groups(rows, 4, stop, DEFAULT_ALGO, only_extra, lambda d, t: None)
    lines = [f"mode={'only_extra' if only_extra else 'all'} files={len(rows)} groups={len(groups)}",
             f"skipped={skipped}"]
    for g in groups:
        lines.append(f"group size={g['size']} n={len(g['members'])} hash={g['hash'][:12]}")
        for m in g["members"]:
            tag = "KEPT" if m["kept"] else ("drop" if m["deletable"] else "----")
            lines.append(f"  {tag} [{m['src']}] {m['path']}")
    if dst:
        res = checker_execute(checker_plan(groups), dst, stop, lambda d, t: None,
                              DEFAULT_ALGO, only_extra)
        lines.append(f"removed ok={res['ok']} fail={res['fail']} copied={res['copied']}")
    out = os.path.join(os.environ.get("TEMP", "."), "sfc_selftest.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def _safe_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _dump_error(tb):
    try:
        p = os.path.join(os.environ.get("TEMP", "."), "filetoolkit_cli_error.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(tb)
    except OSError:
        pass


def _cli_dedup(argv):
    _safe_streams()
    args = {"--workers": 8, "--min-size": 0, "--keep": "最早优先", "--algo": DEFAULT_ALGO}
    flags = set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            args[a] = argv[i + 1]
            i += 2
        else:
            flags.add(a)
            i += 1
    src = args.get("--src")
    if not src:
        print("用法: FileToolkit.exe --cli --src <目录> [--apply] [--algo xxh3-128] [--min-size KB]")
        return 1
    src = os.path.abspath(src)
    dst = os.path.abspath(args.get("--dst") or src + "_sorted")
    quar = os.path.abspath(args.get("--quarantine") or src + "_dup_quarantine")
    r = dedup_analyze(src, dst, quar, int(args["--workers"]),
                      max(0, int(args["--min-size"])) * 1024,
                      args["--keep"], args["--algo"], on_log=print,
                      on_progress=lambda d, t: None)
    if "--apply" in flags:
        dedup_apply(r["plan"], dst, quar, on_log=print)
    return 0


def _argv_value(flag):
    """命令行选项 flag 的后继参数；缺失或位于末尾时返回 None。"""
    if flag not in sys.argv:
        return None
    i = sys.argv.index(flag)
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else None


def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        if i + 1 >= len(sys.argv):
            print("用法: FileToolkit.exe --selftest <目录> [--extra <目录>] [--apply <目录>] [--all]")
            return 2
        extra = _argv_value("--extra")
        dst = _argv_value("--apply")
        return _selftest_checker(sys.argv[i + 1], extra, dst, "--all" not in sys.argv)
    if "--cli" in sys.argv:
        try:
            return _cli_dedup(sys.argv[sys.argv.index("--cli") + 1:])
        except Exception:
            tb = traceback.format_exc()
            _dump_error(tb)
            print(tb, file=sys.stderr)
            return 1
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
