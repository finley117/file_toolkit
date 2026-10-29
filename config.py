"""配置层：config.json 读写。

服务照片重命名页签（photo 段）与关键字整理页签（dedup 段）。其余页签的参数
暂不接入——它们本来就没有持久化，见 TODO 的 D-08。

设计要点：
- 缺失或损坏一律返回默认值，不抛异常，保证主程序能起来
- 写入用「临时文件 + replace」原子替换，避免写一半断电留下坏文件
- 读取时与默认值递归合并，旧版本缺字段也能用
"""

import json
import os
import sys
import tempfile

CONFIG_NAME = "config.json"

DEFAULTS = {
    "version": 1,
    "photo": {
        "source_dir": "",
        "template": "{type}_{date}_{time}",
        "include_subdir": True,
        "date_basis": "拍摄日期",
        "alternate_date": "保留原文件名",
        "skip_extensions": "",
    },
    "dedup": {
        "keyword_rule": "",
    },
}


def config_path():
    """配置文件与 exe（或源码目录）同级，保持绿色版特性。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_NAME)


def _deep_merge(base, override):
    """把 override 合并进 base 的副本；只合并 dict，列表与标量整体替换。"""
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load(path=None):
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULTS))
    return _deep_merge(DEFAULTS, data)


def save(cfg, path=None):
    path = path or config_path()
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def get_value(cfg, dotted, default=None):
    """按 "photo.template" 这种点号路径取值。"""
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_value(cfg, dotted, value):
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
