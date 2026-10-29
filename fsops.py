"""文件系统原语：长路径包装 + 统一的移动/复制。

四个页签都在这里动文件，避免"重名怎么处理、长路径怎么补、失败怎么说"各写一套。
规则只有三条，全工具一致：
  1. 目标已存在 → 不覆盖，直接失败返回原因
  2. 源文件不存在 → 失败，不创建空目标
  3. 任何失败都返回 (False, 原因)，绝不抛异常给调用方
"""

import os
import shutil


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
