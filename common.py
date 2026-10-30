"""业务内核：扫描、哈希、分组、关键字提取、执行。

四个页签界面与入口都从这里调用；本模块不引用任何界面代码，也不引用 file_toolkit。
"""

import hashlib
import os
import re
import shutil
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import xxhash
except ImportError:
    xxhash = None

from util import (has_attr, human_size, is_drive_root, long_path, norm_path,
                  paths_overlap, transfer, unique_path)


APP = "File Toolkit"
VER = "20261030"  # 版本号＝交付日期（同日多次交付加 a/b），每次交付改这一行
_CHUNK = 4 << 20
BENCH_MB = 32
_ATTR_HIDDEN = 0x2
_ATTR_SYSTEM = 0x4
_ATTR_REPARSE = 0x400
_SKIP_EXT = (".lnk",)
_TODO = "todo"
SRC_BASE, SRC_EXTRA = "base", "extra"

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


def preflight(dst, sources, dst_label="目标目录", must_be_empty=False, need_bytes=None):
    """收集"会阻止执行"的问题；返回列表，空列表表示可以执行。

    sources 是 [(标签, 路径), ...]，用来检查目标目录不能与来源目录相同或互相包含。
    各页签只声明自己的要求，不再各写一套判断。
    """
    if not dst:
        return ["没有指定%s" % dst_label]
    dst_abs = os.path.abspath(dst)
    problems = []
    if must_be_empty and is_drive_root(dst_abs):
        problems.append("%s不能是盘根：%s" % (dst_label, dst_abs))
    for label, path in sources:
        if not path:
            continue
        if norm_path(path) == norm_path(dst_abs):
            problems.append("「%s」与「%s」不能是同一个目录" % (dst_label, label))
        elif paths_overlap(dst_abs, path):
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


def digest_file(path, algo, stop=None):
    h = ALGOS[algo]()
    with open(long_path(path), "rb") as f:
        while True:
            buf = f.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
            if stop is not None and stop.is_set():
                return None
    return h.hexdigest()


def _skip_reason(entry, st, min_bytes, include_hidden):
    if has_attr(st, _ATTR_REPARSE):
        return "链接/联接点"
    if not include_hidden and (has_attr(st, _ATTR_HIDDEN) or has_attr(st, _ATTR_SYSTEM)):
        return "隐藏/系统"
    if entry.name.lower().endswith(_SKIP_EXT):
        return "快捷方式"
    if st.st_size < min_bytes:
        return "小于阈值"
    return None


def scan_flat(root, min_bytes, include_hidden, stop, on_progress=None):
    rows, skipped = [], defaultdict(int)
    try:
        with os.scandir(long_path(root)) as it:
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
            with os.scandir(long_path(cur)) as it:
                entries = list(it)
        except OSError:
            skipped["目录无法访问"] += 1
            continue
        for entry in entries:
            if stop.is_set():
                break
            try:
                st = entry.stat(follow_symlinks=False)
                if has_attr(st, _ATTR_REPARSE):
                    skipped["链接/联接点"] += 1
                    continue
                if not include_hidden and (has_attr(st, _ATTR_HIDDEN) or has_attr(st, _ATTR_SYSTEM)):
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
    os.makedirs(long_path(dst), exist_ok=True)
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
            os.makedirs(long_path(folder), exist_ok=True)
        except OSError as e:
            manifest.append(f"[组{gi}] 创建目录失败，本组 {len(moves)} 个文件未尝试：{folder}: {e}")
            skipped += len(moves)
            continue
        manifest.append(f"[组{gi}] 哈希 {g['hash']}  大小 {human_size(g['size'])}  份数 {len(g['members'])}")
        manifest.append(f"  保留: [{'基础' if kept['src'] == SRC_BASE else '额外'}] {kept['path']}")
        ktarget = unique_path(folder, "[保留] " + os.path.basename(kept["path"]))
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
            target = unique_path(folder, os.path.basename(m["path"]))
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
    manifest_path = os.path.join(dst, "清单.txt")
    try:
        with open(long_path(manifest_path), "w", encoding="utf-8") as f:
            f.write("\n".join(manifest) + "\n")
        summary["manifest"] = manifest_path
    except OSError as e:
        summary["manifest"] = None
        summary["manifest_error"] = str(e)
    return summary


_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,6})(?![A-Za-z])")
_DIGIT = re.compile(r"(?<![0-9])([0-9]{3,6})(?![0-9])")
_NUM_MARK = re.compile(r"[\(（]\s*(\d{1,2})\s*[\)）]")
_CP_MARK = re.compile(r"(?i)(?<![A-Za-z])(copy|副本)(?![A-Za-z])")
_KEYWORD_HINT = "留空＝默认规则（3~6 位字母 + 其后 3~6 位数字，字母转大写，扩展名等尾缀保持小写）"


def compile_keyword_rule(text):
    """把用户输入编译成正则；空串返回 None（表示用内置默认规则）。"""
    text = (text or "").strip()
    if not text:
        return None
    return re.compile(text, re.IGNORECASE)


def extract_keyword(stem, prog=None):
    """取关键字：prog 为空走内置规则，否则取用户正则的第一个匹配。

    大小写约定（D-16）：字母统一大写、数字不变；若关键字里含「.后缀」式尾缀，尾缀保持小写。
    这样与媒体页的关键字（`_norm` 转大写、重命名时扩展名小写）保持一致。
    """
    if prog is not None:
        m = prog.search(stem)
        if not m:
            return ""
        parts = m.groups() or (m.group(0),)
        raw = "".join(p for p in parts if p)
    else:
        raw = ""
        for d in _DIGIT.finditer(stem):
            letters = None
            for m in _LETTER.finditer(stem[:d.start()]):
                letters = m
            if letters:
                raw = letters.group(1) + d.group(1)
                break
    if not raw:
        return ""
    if "." in raw:
        head, _, tail = raw.rpartition(".")
        return head.upper() + "." + tail.lower()
    return raw.upper()


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
        os.makedirs(long_path(d), exist_ok=True)
    moved = skipped = failed = 0
    errors = []
    for i, r in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            break
        base = quarantine if r["动作"] == "隔离" else dst
        src_path = r["源路径"]
        target = os.path.join(base, r["文件名"])
        if os.path.exists(long_path(target)):
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


def benchmark_all():
    """用同一份内存数据依次测所有算法，返回 [(名字, MB/s), ...]（按 ALGOS 顺序）。

    缓冲区由本模块持有，界面只负责画表——避免入口再复制一份 `_bench_buf`
    导致 `benchmark_algo` 读到 None。
    """
    global _bench_buf
    if _bench_buf is None:
        _bench_buf = os.urandom(BENCH_MB * 1024 * 1024)
    return [(name, benchmark_algo(name)) for name in ALGOS]
