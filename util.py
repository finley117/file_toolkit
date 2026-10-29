"""通用工具：大小格式化、路径判断、长路径前缀、移动/复制。

只放与具体业务无关的东西，谁都能用；业务内核在 common.py。
"""

import os
import shutil


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def has_attr(st, flag):
    attr = getattr(st, "st_file_attributes", None)
    return bool(attr is not None and attr & flag)


def norm_path(p):
    return os.path.normcase(os.path.abspath(p))


def paths_overlap(a, b):
    try:
        common = os.path.commonpath([norm_path(a), norm_path(b)])
    except ValueError:
        return False
    return common in (norm_path(a), norm_path(b))


def is_drive_root(path):
    p = os.path.abspath(path).rstrip("\\/")
    return len(p) <= 2 or p.endswith(":")


def unique_path(folder, name):
    target = os.path.join(folder, name)
    if not os.path.exists(long_path(target)):
        return target
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = os.path.join(folder, f"{stem} ({i}){ext}")
        if not os.path.exists(long_path(cand)):
            return cand
        i += 1


def long_path(path):
    """Windows 长路径前缀；非 Windows 平台原样返回，短路径也原样返回。"""
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


def transfer(src, dst, mode="move"):
    """移动或复制一个文件，返回 (ok, 错误说明)。

    ok=False 时源文件一定还在原处；目标已存在时不会覆盖。
    """
    try:
        if not os.path.exists(long_path(src)):
            return False, "源文件已不存在"
        if os.path.exists(long_path(dst)):
            return False, "目标已存在同名项"
        if mode == "copy":
            shutil.copy2(long_path(src), long_path(dst))
        else:
            shutil.move(long_path(src), long_path(dst))
        return True, ""
    except OSError as e:
        return False, str(e)
