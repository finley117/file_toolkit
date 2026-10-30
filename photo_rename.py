"""命名逻辑：模板校验、渲染、冲突消解、计划生成、执行。

这一层不引用 tkinter，可被 UI 复用，也便于用 run_tests.py 验证。
"""

import csv
import os
import re

import photo_media
from util import long_path, transfer

TEMPLATE_VARS = [
    ("{type}", "文件类型（IMG 照片 / VID 视频）"),
    ("{date}", "日期 YYYYMMDD"),
    ("{time}", "时间 HHMMSS"),
    ("{original}", "原文件名（不含扩展名）"),
    ("{camera}", "相机型号"),
    ("{lens}", "镜头型号"),
    ("{iso}", "ISO"),
    ("{focal}", "焦距"),
    ("{aperture}", "光圈"),
    ("{shutter}", "快门"),
]

ILLEGAL_CHARS = set('<>:"/\\|?*')
CONTROL_CHARS = {chr(i) for i in range(32)}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    "%s%d" % (p, i) for p in ("COM", "LPT") for i in range(1, 10)
}
MAX_STEM_LEN = 120

# 这些 Warning 属于"正常缺失"，不进异常汇总（仍写入每行的原因列）
_NORMAL_WARNINGS = {"无 EXIF", "格式无 EXIF", "EXIF 无拍摄日期", "无拍摄日期"}

PLAN_FIELDS = ["序号", "所在目录", "原文件名", "新文件名", "拍摄日期",
               "相机", "状态", "原因", "大小"]
MANIFEST_FIELDS = ["时间", "动作", "所在目录", "原文件名", "新文件名", "结果", "说明"]


def validate_template(tpl):
    """校验模板本身。返回 (ok, 错误说明)。

    只检查模板的静态部分：非法字符、未知变量、空模板、缺少日期/时间变量。
    变量运行时取到的值是否合法，由渲染阶段单独检查。
    """
    if not tpl or not tpl.strip():
        return False, "模板不能为空"
    tpl = tpl.strip()

    bad = sorted({ch for ch in tpl if ch in ILLEGAL_CHARS or ch in CONTROL_CHARS})
    if bad:
        shown = " ".join(repr(c) for c in bad)
        return False, "模板含非法字符：%s" % shown

    tokens, rest = _split_braces(tpl)
    if "{" in rest or "}" in rest:
        return False, "花括号未配对"
    known = {name for name, _ in TEMPLATE_VARS}
    for token in tokens:
        if token not in known:
            return False, "未知变量：%s" % token

    if "{date}" not in tpl and "{time}" not in tpl:
        return False, "模板至少要包含 {date} 或 {time}，否则新名不会变化"

    preview = tpl.replace("{date}", "0000-00-00").replace("{time}", "00-00-00")
    for name, _ in TEMPLATE_VARS:
        preview = preview.replace(name, "X" * 8)
    if len(preview) > MAX_STEM_LEN:
        return False, "模板渲染后过长（上限 %d 字符）" % MAX_STEM_LEN
    return True, ""


def _split_braces(tpl):
    """拆出 {xxx} 形式的 token，并返回未配对的残留文本。

    残留里只要还有花括号，就说明模板存在未闭合或多余的括号。
    """
    tokens, rest, buf, depth = [], [], [], 0
    for ch in tpl:
        if depth:
            buf.append(ch)
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    tokens.append("".join(buf))
                    buf = []
        elif ch == "{":
            depth = 1
            buf = ["{"]
        else:
            rest.append(ch)
    rest.extend(buf)
    return tokens, "".join(rest)


def _clean_value(value):
    """把元数据值里 Windows 不允许的字符替换为空格，避免整行被判非法。"""
    s = '' if value is None else str(value)
    s = ''.join(' ' if (ch in ILLEGAL_CHARS or ch in CONTROL_CHARS) else ch for ch in s)
    return ' '.join(s.split()).strip(' .')


def build_stem(tpl, meta, ext, original=""):
    """按模板渲染主体名（不含扩展名），并转大写、清理首尾空格与点。

    变量取不到值时，替换为空串；随后折叠因此产生的连续分隔符与首尾分隔符，
    避免出现 "VID_20240501_103000_" 这种悬空下划线。
    """
    is_video = ext.lower() in photo_media.VIDEO_EXTS
    dt = meta.get("DateTimeOriginal")
    values = {
        "{type}": "VID" if is_video else "IMG",
        "{date}": dt.strftime("%Y%m%d") if dt else "",
        "{time}": dt.strftime("%H%M%S") if dt else "",
        "{original}": _clean_value(original),
        "{camera}": _clean_value(meta.get("Camera", "")),
        "{lens}": _clean_value(meta.get("Lens", "")),
        "{iso}": _clean_value(meta.get("ISO", "")),
        "{focal}": _clean_value(meta.get("Focal", "")),
        "{aperture}": _clean_value(meta.get("Aperture", "")),
        "{shutter}": _clean_value(meta.get("Shutter", "")),
    }
    stem = tpl
    for key, val in values.items():
        stem = stem.replace(key, val)

    stem = _collapse_empty_parts(stem)
    stem = stem.strip(" .")
    return stem.upper()


def _collapse_empty_parts(text):
    """清理空变量留下的分隔符：连续分隔符折叠、首尾分隔符去掉。"""
    # 把连续的分隔符（下划线/横线/空格）折叠成一个
    text = re.sub(r"([_\- ])\1{1,}", r"\1", text)
    # 去掉紧跟分隔符后的重复分隔符组合，如 "_-" 或 "-_"
    text = re.sub(r"[_\- ]{2,}", "_", text)
    # 去掉首尾的分隔符
    text = text.strip("_- ")
    return text


def sanitize_check(stem):
    """渲染结果的合法性检查。返回 (ok, 原因)。"""
    if not stem:
        return False, "渲染结果为空"
    bad = sorted({ch for ch in stem if ch in ILLEGAL_CHARS or ch in CONTROL_CHARS})
    if bad:
        return False, "名称含非法字符：%s" % " ".join(repr(c) for c in bad)
    if len(stem) > MAX_STEM_LEN:
        return False, "名称过长（%d > %d）" % (len(stem), MAX_STEM_LEN)
    if stem.split(".")[0].upper() in RESERVED_NAMES:
        return False, "名称是系统保留字"
    return True, ""


def unique_name(stem, ext, taken, own_name=None, directory=None):
    """在 taken 集合内消解重名：stem、stem_001、stem_002……

    taken 里应包含「本批次所有文件的当前名」与「已分配的新名」，
    因此永远不会覆盖任何已存在的文件。
    directory 给出时，还会再查一次磁盘——同名的文件夹、分析后新放进来的文件
    不在 taken 里，只靠集合会漏判。

    own_name 是该文件自己的当前文件名：当候选名恰好等于自己时直接采用，
    否则「已重命名过的文件再跑一次」会被自己挡在 taken 里，导致名字越改越长。
    """
    own = (own_name or "").lower()

    def available(name):
        low = name.lower()
        if low == own:
            return True          # 就是它自己，允许
        if low in taken:
            return False
        if directory:
            try:
                if os.path.exists(long_path(os.path.join(directory, name))):
                    return False
            except (OSError, ValueError):
                pass
        return True

    candidate = stem + ext
    if available(candidate):
        taken.add(candidate.lower())
        return candidate
    idx = 1
    while idx < 10000:
        candidate = "%s_%03d%s" % (stem, idx, ext)
        if available(candidate):
            taken.add(candidate.lower())
            return candidate
        idx += 1
    raise RuntimeError("重名过多：%s" % stem)


def parse_skip_extensions(text):
    """把 "mp4 .mov,*.avi" 解析成 {'.mp4', '.mov', '.avi'}；支持 tar.gz 这类多段后缀。"""
    out = set()
    for part in re.split(r"[\s,;]+", (text or "").strip().lower()):
        part = part.lstrip("*")
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        out.add(part)
    return out


def _is_link(path):
    """符号链接 / Windows junction：不跟随，避免重复扫描或绕圈。"""
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except OSError:
        return False


def collect_files(root, include_subdir, skip_extensions=None, on_log=None):
    """收集待处理文件。返回 (paths, skipped_info)。"""
    skip = set(skip_extensions or ())
    skip_suffixes = tuple(skip)
    paths = []
    skipped = {"不支持格式": 0, "已跳过扩展名": 0, "读取失败": 0}

    def accept(name):
        lower = name.lower()
        if skip_suffixes and lower.endswith(skip_suffixes):
            skipped["已跳过扩展名"] += 1
            return False
        ext = os.path.splitext(name)[1].lower()
        if ext not in photo_media.PHOTO_EXTS and ext not in photo_media.VIDEO_EXTS:
            skipped["不支持格式"] += 1
            return False
        return True

    def onerror(err):
        skipped["读取失败"] += 1
        if on_log:
            on_log("[!] 无法读取目录: %s (%s)"
                   % (getattr(err, "filename", ""), err))

    if include_subdir:
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            dirnames[:] = [d for d in dirnames
                           if not _is_link(os.path.join(dirpath, d))]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if _is_link(full):
                    continue
                if accept(name):
                    paths.append(full)
    else:
        try:
            names = os.listdir(root)
        except OSError as e:
            skipped["读取失败"] += 1
            if on_log:
                on_log("[!] 无法读取目录: %s (%s)" % (root, e))
            names = []
        for name in names:
            full = os.path.join(root, name)
            if _is_link(full):
                continue
            if not os.path.isfile(full):
                continue
            if accept(name):
                paths.append(full)
    paths.sort()
    if on_log:
        on_log("[i] 收集 %d 个文件，跳过 %s" % (len(paths), skipped))
    return paths, skipped


def plan_renames(paths, tpl, date_basis=None, alternate=None, workers=None,
                 on_progress=None, on_log=None, cancel=None):
    """阶段一：只读分析，返回计划行列表。

    每行 status:
      "ok"   —— 可执行
      "skip" —— 跳过，原因在 reason

    日期由 photo_media.resolve_date 决定：主基准取不到时按备选回退。
    """
    ok, err = validate_template(tpl)
    if not ok:
        raise ValueError(err)

    date_basis = date_basis or photo_media.DATE_BASIS_SHOOT
    alternate = alternate or photo_media.ALTERNATE_KEEP

    # 先把本批次所有文件的当前名登记，避免新名撞上某个文件的现名
    taken = set()
    for p in paths:
        taken.add(os.path.basename(p).lower())

    total = len(paths)
    if on_log:
        on_log("[i] 读取元数据（%d 个文件，%d 线程）"
               % (total, workers or photo_media.DEFAULT_WORKERS))
    metas = photo_media.read_many(
        paths,
        workers=workers,
        on_progress=(lambda d, t: on_progress(d, t)) if on_progress else None,
        cancel=cancel,
    )

    rows = []
    warn_stat = {}
    stopped = False
    for idx, path in enumerate(paths, 1):
        if cancel is not None and idx % 50 == 0 and cancel.is_set():
            stopped = True
            break
        name = os.path.basename(path)
        directory = os.path.dirname(path)
        ext = os.path.splitext(name)[1].lower()
        size = 0
        size_unknown = False
        try:
            size = os.path.getsize(path)
        except OSError:
            size_unknown = True

        meta = metas.get(path) or photo_media.empty_meta("读取失败")
        warn = (meta.get("Warning") or "").strip()
        if warn and warn not in _NORMAL_WARNINGS:
            warn_stat[warn] = warn_stat.get(warn, 0) + 1
        dt, why = photo_media.resolve_date(path, meta, date_basis, alternate)

        row = {
            "序号": idx,
            "所在目录": directory,
            "原文件名": name,
            "新文件名": "",
            "拍摄日期": "",
            "相机": meta.get("Camera", ""),
            "状态": "skip",
            "原因": "",
            "大小": size,
            "大小未知": size_unknown,
        }

        if dt is None:
            note = why or "无可用日期"
            if warn and warn not in note:
                note += "（%s）" % warn
            row["原因"] = note
            rows.append(row)
            continue

        # 把解析出的日期回填进 meta，供模板渲染使用
        meta = dict(meta)
        meta["DateTimeOriginal"] = dt
        row["拍摄日期"] = dt.strftime("%Y-%m-%d %H:%M:%S")

        original_stem = os.path.splitext(name)[0]
        stem = build_stem(tpl, meta, ext, original=original_stem)
        fine, why = sanitize_check(stem)
        if not fine:
            row["原因"] = why
            rows.append(row)
            continue

        new_name = unique_name(stem, ext, taken, own_name=name, directory=directory)
        if new_name.lower() == name.lower():
            row["新文件名"] = new_name
            row["原因"] = "名称未变化"
            rows.append(row)
            continue

        row["新文件名"] = new_name
        row["状态"] = "ok"
        rows.append(row)

    ok_n = sum(1 for r in rows if r["状态"] == "ok")
    skip_n = len(rows) - ok_n
    if on_log:
        if stopped:
            on_log("[!] 分析已停止：已处理 %d/%d，可执行 %d" % (len(rows), total, ok_n))
        else:
            on_log("[i] 计划完成：可执行 %d，跳过 %d" % (ok_n, skip_n))
        if warn_stat:
            detail = "，".join("%s %d" % (k, v) for k, v in
                              sorted(warn_stat.items(), key=lambda kv: (-kv[1], kv[0])))
            on_log("[!] 元数据异常 %d 个文件：%s" % (sum(warn_stat.values()), detail))
    return rows


def report_dir_for(root):
    """报告目录：<源目录>_photo_report；盘根时 D:\\_photo_report。"""
    root = os.path.abspath(root)
    if root.endswith(os.sep):
        root = root.rstrip(os.sep + "/")
    if len(root) <= 2 or root.endswith(":"):
        return os.path.join(root + os.sep, "_photo_report")
    return root + "_photo_report"


def write_plan_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def apply_plan(rows, report_dir, on_progress=None, on_log=None, cancel=None):
    """阶段二：按内存里的计划重命名，写 manifest_<时间戳>.csv。

    plan.csv 只是核对报表，执行不回读它；cancel 为 threading.Event，
    置位后停止后续条目（已完成的保留在台账里）。
    """
    import datetime

    todo = [r for r in rows if (r.get("状态") or "").strip() == "ok"]
    if on_log:
        on_log("[i] 待执行 %d 条" % len(todo))

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = os.path.join(report_dir, "manifest_%s.csv" % stamp)
    done = failed = skipped = 0
    total = len(todo)
    stopped = False
    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for idx, r in enumerate(todo, 1):
            if cancel is not None and cancel.is_set():
                stopped = True
                skipped = total - idx + 1
                if on_log:
                    on_log("[i] 已停止，剩余 %d 条未处理" % skipped)
                break
            directory = r.get("所在目录", "")
            old_name = r.get("原文件名", "")
            new_name = r.get("新文件名", "")
            src = os.path.join(directory, old_name)
            dst = os.path.join(directory, new_name)
            size = r.get("大小") or 0
            size_unknown = bool(r.get("大小未知"))
            rec = {
                "时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "动作": "重命名",
                "所在目录": directory,
                "原文件名": old_name,
                "新文件名": new_name,
                "结果": "失败",
                "说明": "",
            }
            try:
                if not os.path.isfile(long_path(src)):
                    raise FileNotFoundError("源文件已不存在")
                if not size_unknown and os.path.getsize(long_path(src)) != int(size):
                    raise RuntimeError("文件已变化（大小不符）")
                if os.path.exists(long_path(dst)) and os.path.normcase(src) != os.path.normcase(dst):
                    raise FileExistsError("目标已存在")
                moved, why = transfer(src, dst)
                if not moved:
                    raise RuntimeError(why)
                rec["结果"] = "成功"
                done += 1
            except Exception as e:
                rec["说明"] = str(e)
                failed += 1
                if on_log:
                    on_log("[!] 失败 %s：%s" % (old_name, e))
            w.writerow(rec)
            if on_progress and (idx % 20 == 0 or idx == total):
                on_progress(idx, total)

    if on_log:
        on_log("[OK] 执行完成：成功 %d，失败 %d%s"
               % (done, failed, ("，未处理 %d" % skipped) if stopped else ""))
    return {"moved": done, "failed": failed, "skipped": skipped,
            "stopped": stopped, "manifest": manifest_path}
