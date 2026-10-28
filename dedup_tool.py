#!/usr/bin/env python3
"""文件查重与关键字归集工具：SHA-256 去重 + 正则关键字归集，图形界面单文件版。"""

import csv
import hashlib
import os
import queue
import re
import shutil
import sys
import threading
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

APP_TITLE = "文件查重与关键字归集"
APP_VER = "1.0"

_CHUNK = 1 << 20
_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,6})(?![A-Za-z])")
_DIGIT = re.compile(r"(?<![0-9])([0-9]{3,6})(?![0-9])")
_NUM_MARK = re.compile(r"[\(（]\s*(\d+)\s*[\)）]")
_CP_MARK = re.compile(r"(?i)(?<![A-Za-z])(copy|副本)(?![A-Za-z])")
_ATTR_HIDDEN = 0x2
_ATTR_SYSTEM = 0x4
_SKIP_EXT = (".lnk", ".tmp", ".part", ".crdownload")

PLAN_FIELDS = ["组号", "名次", "动作", "文件名", "关键字", "副本标记", "大小(B)",
               "修改时间", "SHA256", "源路径", "目标路径", "备注"]
MANIFEST_FIELDS = ["时间", "动作", "原路径", "新路径", "关键字", "副本标记",
                   "大小(B)", "SHA256", "组号", "备注"]

ACT_QUARANTINE = "隔离"
ACT_MOVE = "移动"
ACT_NONE = "不动"
ACT_SKIP = "跳过"


def _long(path):
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if len(p) < 240:
        return p
    return "\\\\?\\" + (p if not p.startswith("\\\\") else "UNC\\" + p[2:])


def _sha256(path):
    h = hashlib.sha256()
    with open(_long(path), "rb") as f:
        while True:
            buf = f.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _attrs(st):
    return getattr(st, "st_file_attributes", None)


def _default_sibling(src, suffix):
    s = os.path.abspath(src)
    stripped = s.rstrip("\\/")
    if len(stripped) <= 2 or stripped.endswith(":"):
        return os.path.join(s, suffix)
    return stripped + suffix


def _scan(src, min_size):
    items, skipped = [], defaultdict(int)
    with os.scandir(_long(src)) as it:
        for e in it:
            try:
                if not e.is_file(follow_symlinks=False):
                    skipped["非普通文件"] += 1
                    continue
                st = e.stat(follow_symlinks=False)
                if st.st_size < min_size:
                    skipped["0字节/过小"] += 1
                    continue
                attr = _attrs(st)
                if attr is not None and attr & (_ATTR_HIDDEN | _ATTR_SYSTEM):
                    skipped["隐藏/系统"] += 1
                    continue
                if e.name.lower().endswith(_SKIP_EXT):
                    skipped["快捷方式/临时"] += 1
                    continue
                items.append({
                    "path": e.path,
                    "name": e.name,
                    "stem": Path(e.name).stem,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
            except OSError:
                skipped["读取失败"] += 1
    return items, dict(skipped)


def _copy_mark(stem):
    nums = [int(m.group(1)) for m in _NUM_MARK.finditer(stem) if m.group(1)]
    if nums:
        return True, max(nums)
    if _CP_MARK.search(stem):
        return True, 1
    return False, 0


def extract_keyword(stem):
    for d in _DIGIT.finditer(stem):
        letters = None
        for m in _LETTER.finditer(stem[:d.start()]):
            letters = m
        if letters:
            return (letters.group(1) + d.group(1)).lower()
    return ""


def _rank_key(item, keep):
    has, num = _copy_mark(item["stem"])
    mtime = item["mtime"] if keep == "最早优先" else -item["mtime"]
    return (1 if has else 0, num, mtime, -len(item["name"]))


def _hash_group(items, workers, on_progress, on_log):
    total = len(items)
    state = {"done": 0}
    lock = threading.Lock()

    def one(it):
        try:
            it["sha"] = _sha256(it["path"])
        except OSError as e:
            it["sha"] = ""
            on_log(f"[warn] 读取失败 {it['path']}: {e}")
        with lock:
            state["done"] += 1
            if state["done"] % 40 == 0 or state["done"] == total:
                on_progress(state["done"], total)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, items))


def build_plan(src, dst, quarantine, workers=8, min_size=1, keep="最早优先",
               on_log=print, on_progress=lambda d, t: None):
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    quarantine = os.path.abspath(quarantine)
    on_log(f"[i] 扫描 {src}")
    if os.path.normcase(os.path.normpath(dst)) == os.path.normcase(os.path.normpath(src)):
        raise RuntimeError("目标目录不能与源目录相同")
    if os.path.isdir(dst) and os.listdir(dst):
        on_log(f"[!] 注意：目标目录非空，执行阶段会中止以免覆盖 —— {dst}")
    items, skipped = _scan(src, min_size)
    on_log(f"[i] 纳入 {len(items)} 个文件，跳过 {sum(skipped.values())} 个 {skipped or ''}")
    if not items:
        raise RuntimeError("源目录没有可处理的文件")

    by_size = defaultdict(list)
    for it in items:
        it["kw"] = extract_keyword(it["stem"])
        it["mark"], it["mark_num"] = _copy_mark(it["stem"])
        by_size[it["size"]].append(it)
    dup_candidates = [g for g in by_size.values() if len(g) > 1]
    on_log(f"[i] 大小分组 {len(by_size)} 组，其中 {len(dup_candidates)} 组需计算哈希"
           f"（{sum(len(g) for g in dup_candidates)} 个文件）")

    flat = [it for g in dup_candidates for it in g]
    if flat:
        on_log("[i] 计算 SHA-256 ...")
        _hash_group(flat, max(1, workers), on_progress, on_log)

    groups = []
    for g in dup_candidates:
        sub = defaultdict(list)
        for it in g:
            if it.get("sha"):
                sub[it["sha"]].append(it)
        for sha, members in sub.items():
            if len(members) > 1:
                members.sort(key=lambda x: _rank_key(x, keep))
                groups.append({"sha": sha, "members": members})
    groups.sort(key=lambda x: (-x["members"][0]["size"], x["members"][0]["name"]))
    on_log(f"[i] 完全相同的文件组 {len(groups)} 组，"
           f"其中多余副本 {sum(len(g['members']) - 1 for g in groups)} 个")

    in_group = {}
    for gi, g in enumerate(groups, 1):
        for rank, it in enumerate(g["members"], 1):
            in_group[it["path"]] = (gi, rank, len(g["members"]))

    rows = []
    for it in items:
        gid, rank, total = in_group.get(it["path"], ("", "", ""))
        note = []
        if gid:
            note.append("重复组保留件" if rank == 1 else f"重复组第{rank}份/共{total}份")
        if it["kw"]:
            note.append(f"关键字={it['kw']}")
        if gid and rank > 1:
            act, target = ACT_QUARANTINE, os.path.join(quarantine, it["name"])
        elif gid and rank == 1:
            act, target = ACT_MOVE, os.path.join(dst, it["name"])
        elif it["kw"]:
            act, target = ACT_MOVE, os.path.join(dst, it["name"])
        else:
            act, target = ACT_NONE, ""
        rows.append({
            "组号": gid, "名次": rank, "动作": act, "文件名": it["name"],
            "关键字": it["kw"], "副本标记": "是" if it["mark"] else "",
            "大小(B)": it["size"],
            "修改时间": datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d %H:%M:%S"),
            "SHA256": it.get("sha", ""), "源路径": it["path"], "目标路径": target,
            "备注": "；".join(note),
        })

    kw_stat = defaultdict(int)
    for it in items:
        if it["kw"]:
            kw_stat[it["kw"]] += 1

    report_dir = os.path.abspath(src + "_dedup_report")
    os.makedirs(report_dir, exist_ok=True)
    plan_path = os.path.join(report_dir, "plan.csv")
    with open(plan_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS)
        w.writeheader()
        w.writerows(rows)

    cnt = defaultdict(int)
    for r in rows:
        cnt[r["动作"]] += 1
    waste = sum(g["members"][0]["size"] * (len(g["members"]) - 1) for g in groups)
    summary = [
        f"源目录      : {src}",
        f"目标目录    : {dst}",
        f"隔离区      : {quarantine}",
        f"文件总数    : {len(items)}（跳过 {sum(skipped.values())}）",
        f"总容量      : {sum(i['size'] for i in items) / 1048576:.1f} MB",
        f"重复组      : {len(groups)} 组，副本 {cnt[ACT_QUARANTINE]} 个，可回收 {waste / 1048576:.1f} MB",
        f"关键字命中  : {sum(1 for i in items if i['kw'])} 个文件，{len(kw_stat)} 个不同关键字",
        f"待移动      : {cnt[ACT_MOVE]} 个（保留件 + 命中件）",
        f"不动        : {cnt[ACT_NONE]} 个",
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
    on_log(f"[✓] 计划已生成：{plan_path}")
    on_log("[i] 检查无误后点「执行计划」；可先用 Excel 修改 plan.csv 的动作列")
    return {"report_dir": report_dir, "plan": plan_path, "rows": rows,
            "counts": dict(cnt), "groups": len(groups), "items": len(items)}


def _read_plan(plan_path):
    with open(plan_path, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def apply_plan(plan_path, dst, quarantine, on_log=print):
    rows = _read_plan(plan_path)
    if not rows:
        raise RuntimeError("计划表为空")
    dst = os.path.abspath(dst)
    quarantine = os.path.abspath(quarantine)
    for d in (dst, quarantine):
        if os.path.isdir(d) and os.listdir(d):
            raise RuntimeError(f"目标目录非空，为避免覆盖已中止：{d}")
        os.makedirs(d, exist_ok=True)

    todo = [r for r in rows if r["动作"].strip() in (ACT_QUARANTINE, ACT_MOVE)]
    on_log(f"[i] 待执行 {len(todo)} 个文件（隔离 + 移动）")
    manifest_path = os.path.join(os.path.dirname(plan_path), "manifest.csv")
    failed = []
    done = 0
    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for r in todo:
            src_path = r["源路径"]
            act = r["动作"].strip()
            plan_target = (r.get("目标路径") or "").strip()
            base_dir = quarantine if act == ACT_QUARANTINE else dst
            if plan_target:
                target = plan_target if os.path.isabs(plan_target) else os.path.join(base_dir, plan_target)
            else:
                target = os.path.join(base_dir, os.path.basename(src_path))
            rec = {
                "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "动作": act,
                "原路径": src_path, "新路径": target, "关键字": r.get("关键字", ""),
                "副本标记": r.get("副本标记", ""), "大小(B)": r.get("大小(B)", ""),
                "SHA256": r.get("SHA256", ""), "组号": r.get("组号", ""),
                "备注": r.get("备注", ""),
            }
            try:
                if not os.path.isfile(_long(src_path)):
                    raise FileNotFoundError("源文件已不存在")
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
    on_log(f"[✓] 执行完成：成功 {done} 个，失败 {len(failed)} 个")
    on_log(f"[i] 去向清单：{manifest_path}")
    return {"moved": done, "failed": failed, "manifest": manifest_path}


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, scrolledtext, filedialog, messagebox
        self.tk, self.ttk, self.scrolledtext = tk, ttk, scrolledtext
        self.fd, self.mb = filedialog, messagebox
        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} v{APP_VER}")
        self.root.geometry("880x660")
        self.root.minsize(760, 560)
        self.q = queue.Queue()
        self.worker = None
        self.result = None
        self._build()
        self.root.after(120, self._poll)

    def _build(self):
        ttk, tk = self.ttk, self.tk
        wrap = ttk.Frame(self.root, padding=14)
        wrap.pack(fill="both", expand=True)
        grid = ttk.Frame(wrap)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        self.var_src = tk.StringVar()
        self.var_dst = tk.StringVar()
        self.var_quar = tk.StringVar()
        self.var_workers = tk.IntVar(value=min(32, (os.cpu_count() or 4) * 4))
        self.var_keep = tk.StringVar(value="最早优先")
        self.var_minsize = tk.IntVar(value=1)

        rows = [("源目录", self.var_src, True), ("目标目录", self.var_dst, True),
                ("隔离区", self.var_quar, True)]
        for i, (label, var, browse) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Entry(grid, textvariable=var).grid(row=i, column=1, sticky="ew", padx=8, pady=4)
            ttk.Button(grid, text="浏览…", width=8,
                       command=lambda v=var: self._pick_dir(v)).grid(row=i, column=2, pady=4)

        opts = ttk.Frame(wrap)
        opts.pack(fill="x", pady=(10, 0))
        ttk.Label(opts, text="线程数").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=64, width=5, textvariable=self.var_workers).pack(side="left", padx=(6, 18))
        ttk.Label(opts, text="最小文件字节").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=1048576, width=8, textvariable=self.var_minsize).pack(side="left", padx=(6, 18))
        ttk.Label(opts, text="保留规则").pack(side="left")
        ttk.Combobox(opts, width=12, state="readonly", textvariable=self.var_keep,
                     values=["最早优先", "最新优先"]).pack(side="left", padx=6)

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=12)
        self.btn_scan = ttk.Button(btns, text="① 分析（只读）", command=self._on_scan)
        self.btn_scan.pack(side="left")
        self.btn_apply = ttk.Button(btns, text="② 执行计划", command=self._on_apply, state="disabled")
        self.btn_apply.pack(side="left", padx=8)
        self.btn_open = ttk.Button(btns, text="打开报告目录", command=self._on_open, state="disabled")
        self.btn_open.pack(side="left")

        self.pb = ttk.Progressbar(wrap, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(4, 4))
        self.lbl = ttk.Label(wrap, text="就绪：先选源目录，点「分析（只读）」，不会改动任何文件")
        self.lbl.pack(fill="x")

        self.log = self.scrolledtext.ScrolledText(wrap, height=18, wrap="word",
                                                  font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self._log(f"{APP_TITLE} v{APP_VER}")
        self._log("流程：① 分析（只读，生成 plan.csv）→ 检查/可手改 → ② 执行计划（隔离副本 + 移动保留件与命中件）")
        self._log("说明：源目录只扫一层，不递归；重复副本进隔离区，不直接删除。")

    def _pick_dir(self, var):
        d = self.fd.askdirectory()
        if d:
            var.set(os.path.normpath(d))
            if var is self.var_src:
                base = os.path.normpath(d)
                self.var_dst.set(_default_sibling(base, "_sorted"))
                self.var_quar.set(_default_sibling(base, "_dup_quarantine"))

    def _log(self, msg):
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
        self.log.see("end")
        self.root.update_idletasks()

    def _post(self, kind, payload):
        self.q.put((kind, payload))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    d, t = payload
                    self.pb["value"] = (d / t * 100) if t else 0
                    self.lbl.config(text=f"哈希计算中 {d}/{t}")
                elif kind == "done":
                    self._finish(payload)
                elif kind == "applied":
                    self._applied(payload)
                elif kind == "error":
                    self._fail(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _busy(self, flag):
        state = "disabled" if flag else "normal"
        self.btn_scan.config(state=state)
        if flag:
            self.btn_apply.config(state="disabled")
        self.root.config(cursor="watch" if flag else "")

    def _on_scan(self):
        src = self.var_src.get().strip()
        if not src or not os.path.isdir(src):
            self._log("[!] 请先选择有效的源目录")
            return
        dst = self.var_dst.get().strip() or _default_sibling(src, "_sorted")
        quar = self.var_quar.get().strip() or _default_sibling(src, "_dup_quarantine")
        self.var_dst.set(dst)
        self.var_quar.set(quar)
        self.pb["value"] = 0
        self._busy(True)
        self.lbl.config(text="分析中…（只读，不改动文件）")
        args = (src, dst, quar, int(self.var_workers.get()), int(self.var_minsize.get()),
                self.var_keep.get())

        def work():
            try:
                r = build_plan(*args, on_log=lambda m: self._post("log", m),
                               on_progress=lambda d, t: self._post("progress", (d, t)))
                self._post("done", r)
            except Exception as e:
                self._post("error", f"{e}\n{traceback.format_exc()}")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _finish(self, r):
        self._busy(False)
        self.result = r
        self.pb["value"] = 100
        self.lbl.config(text=f"分析完成：{r['items']} 个文件，{r['groups']} 个重复组")
        self.btn_apply.config(state="normal")
        self.btn_open.config(state="normal")

    def _fail(self, msg):
        self._busy(False)
        self.pb["value"] = 0
        self.lbl.config(text="出错了，见日志")
        self._log("[x] " + msg)

    def _applied(self, r):
        self._busy(False)
        self.pb["value"] = 100
        self.lbl.config(text=f"执行完成：成功 {r['moved']} 个，失败 {len(r['failed'])} 个")
        self.btn_open.config(state="normal")

    def _on_apply(self):
        if not self.result:
            return
        c = self.result["counts"]
        msg = (f"即将执行不可逆操作：\n\n"
               f"· 隔离 {c.get(ACT_QUARANTINE, 0)} 个重复副本 → {self.var_quar.get()}\n"
               f"· 移动 {c.get(ACT_MOVE, 0)} 个文件 → {self.var_dst.get()}\n\n"
               f"⚠️ 此操作会改动文件位置，请先确认 plan.csv 内容正确！\n"
               f"确定继续？")
        if not self.mb.askyesno("确认执行", msg, icon="warning"):
            self._log("[i] 已取消执行")
            return
        self._busy(True)
        self.btn_apply.config(state="disabled")
        self.lbl.config(text="执行中…")
        plan, dst, quar = self.result["plan"], self.var_dst.get(), self.var_quar.get()

        def work():
            try:
                r = apply_plan(plan, dst, quar, on_log=lambda m: self._post("log", m))
                self._post("applied", r)
            except Exception as e:
                self._post("error", f"{e}\n{traceback.format_exc()}")

        threading.Thread(target=work, daemon=True).start()

    def _on_open(self):
        if self.result:
            try:
                os.startfile(self.result["report_dir"])
            except Exception as e:
                self._log(f"[!] 打开失败：{e}")

    def run(self):
        self.root.mainloop()


def _run_cli(argv):
    args = {"--workers": 8, "--min-size": 1, "--keep": "最早优先"}
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
        print("用法: dedup_tool.exe --cli --src <目录> [--apply] [--workers N]")
        return 1
    src = os.path.abspath(src)
    dst = os.path.abspath(args.get("--dst") or _default_sibling(src, "_sorted"))
    quar = os.path.abspath(args.get("--quarantine") or _default_sibling(src, "_dup_quarantine"))
    r = build_plan(src, dst, quar, int(args["--workers"]), int(args["--min-size"]),
                   args["--keep"], on_log=print,
                   on_progress=lambda d, t: print(f"  hash {d}/{t}", end="\r"))
    if "--apply" in flags:
        apply_plan(r["plan"], dst, quar, on_log=print)
    return 0


def main():
    if "--cli" in sys.argv:
        return _run_cli(sys.argv[2:])
    try:
        App().run()
    except Exception:
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP_TITLE, traceback.format_exc())
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)


if __name__ == "__main__":
    main()
