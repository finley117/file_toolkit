"""回归测试：不启动界面，直接调用核心函数跑一遍。

用法：
    python run_tests.py

全部通过时返回 0，有失败返回 1（可用于打包前的一道闸门）。
所有测试都在系统临时目录里造文件，不碰真实素材。
"""

import datetime
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import photo_media                   # noqa: E402
import photo_rename                  # noqa: E402
from checker_page import CheckerPage  # noqa: E402
from common import (DEFAULT_ALGO, SRC_BASE, checker_execute,  # noqa: E402
                    checker_groups, checker_plan, compile_keyword_rule, copy_mark,
                    dedup_analyze, dedup_execute, extract_keyword, scan_tree)
from util import is_drive_root, long_path, paths_overlap  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s   %s" % (name, detail))


def make(path, size=100, fill=b"x"):
    """造一个指定大小的文件：fill 决定内容，同 fill 同 size 就是完全相同的两份。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as f:
        f.write(fill * size)


def mark_hidden(path):
    """给文件打上 Windows 的“隐藏+系统”属性。

    注意：查重页判断隐藏/系统看的是文件属性，不是名字里带不带点——
    所以 ".hidden.txt" 这种名字在 Windows 上依然会被扫描到。
    """
    if os.name != "nt":
        return
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02 | 0x04)


# ---------------------------------------------------------------- 关键字


def test_keyword():
    print("关键字提取")
    check("默认规则 report_2024 -> REPORT2024（字母大写）",
          extract_keyword("report_2024") == "REPORT2024", extract_keyword("report_2024"))
    check("字母不足 3 位不命中", extract_keyword("ab_2024") == "",
          extract_keyword("ab_2024"))
    prog = compile_keyword_rule(r"(\d{4})")
    check("自定义正则取第一个匹配", extract_keyword("abc_2024_def", prog) == "2024")
    check("空规则等于用默认", compile_keyword_rule("  ") is None)


def test_copy_mark():
    print("副本标记")
    check("(2) 认作副本", copy_mark("a (2)") == (True, 2), copy_mark("a (2)"))
    check("copy 认作副本", copy_mark("a copy") == (True, 1), copy_mark("a copy"))
    check("普通文件名不算副本", copy_mark("a") == (False, 0))


# ---------------------------------------------------------------- 路径


def test_paths():
    print("路径校验")
    check("父子目录算重叠", paths_overlap(r"D:\a", r"D:\a\b"))
    check("跨盘不算重叠", not paths_overlap(r"D:\a", r"E:\b"))
    check("相同目录算重叠", paths_overlap(r"D:\a", r"D:\a"))
    check("盘根能识别", is_drive_root("D:\\") and not is_drive_root(r"D:\a"))
    check("短路径不加前缀", long_path(r"D:\a") == r"D:\a", long_path(r"D:\a"))
    long_p = "D:\\" + "x" * 250
    check("长路径加前缀", long_path(long_p).startswith("\\\\?\\"), long_path(long_p)[:6])
    check("加前缀可重复调用", long_path(long_path(long_p)) == long_path(long_p))


# ---------------------------------------------------------------- 扫描


def test_scan_skip(tmp):
    print("扫描的跳过规则")
    root = os.path.join(tmp, "scan")
    make(os.path.join(root, "keep.txt"), 2000)
    make(os.path.join(root, "sub", "inside.txt"), 2000)
    make(os.path.join(root, "too_small.txt"), 10)
    make(os.path.join(root, "a.lnk"), 2000)
    hidden = os.path.join(root, "hidden.txt")
    make(hidden, 2000)
    mark_hidden(hidden)
    rows, skipped = scan_tree(root, 1024, False, threading.Event(),
                                 SRC_BASE, lambda n: None)
    names = set(os.path.basename(r["path"]) for r in rows)
    check("正常文件被纳入", "keep.txt" in names, names)
    check("子目录里的文件也纳入", "inside.txt" in names, names)
    check("小于阈值的跳过", "too_small.txt" not in names)
    check("快捷方式跳过", "a.lnk" not in names)
    check("隐藏属性文件跳过",
          "hidden.txt" not in names and skipped.get("隐藏/系统", 0) >= 1, skipped)


# ---------------------------------------------------------------- 关键字页


def test_dedup(tmp):
    print("关键字页：分析 + 移动")
    src = os.path.join(tmp, "dsrc")
    make(os.path.join(src, "report_2024_a.txt"), 500)
    make(os.path.join(src, "report_2024_a (2).txt"), 500)
    make(os.path.join(src, "random.txt"), 500, fill=b"y")   # 同大小但内容不同
    dst, quar = os.path.join(tmp, "out"), os.path.join(tmp, "dup")
    r = dedup_analyze(src, dst, quar, 2, 0, "最早优先", DEFAULT_ALGO, "",
                         on_log=lambda m: None)
    check("三类计数正确", r["counts"] == {"移动": 1, "隔离": 1, "不动": 1}, r["counts"])
    res = dedup_execute(r["rows"], dst, quar, on_log=lambda m: None)
    check("移动 2 个且无失败", res["moved"] == 2 and res["failed"] == 0, res)
    check("命中文件进了目标目录", os.listdir(dst) == ["report_2024_a.txt"], os.listdir(dst))
    check("副本进了 hash 目录",
          os.listdir(quar) == ["report_2024_a (2).txt"], os.listdir(quar))
    check("源目录只剩无关文件", os.listdir(src) == ["random.txt"], os.listdir(src))


# ---------------------------------------------------------------- 查重页


def test_checker(tmp):
    print("查重页：分组 + 移除")
    src = os.path.join(tmp, "csrc")
    make(os.path.join(src, "a.txt"), 800)
    make(os.path.join(src, "b.txt"), 800)
    make(os.path.join(src, "c.txt"), 900)
    stop = threading.Event()
    rows, _ = scan_tree(src, 0, False, stop, SRC_BASE, lambda n: None)
    groups = checker_groups(rows, 2, stop, DEFAULT_ALGO, False, lambda d, t: None)
    check("找到 1 组重复", len(groups) == 1, len(groups))
    if groups:
        kept = sum(1 for m in groups[0]["members"] if m["kept"])
        drop = sum(1 for m in groups[0]["members"] if m["deletable"])
        check("1 个保留、1 个待移除", kept == 1 and drop == 1, (kept, drop))
        out = os.path.join(tmp, "moved")
        res = checker_execute(checker_plan(groups), out, stop,
                                 lambda d, t: None, DEFAULT_ALGO, False)
        check("移除 1 个且无失败", res["ok"] == 1 and res["fail"] == 0, res)
        check("清单已写出", os.path.isfile(os.path.join(out, "清单.txt")))


# ---------------------------------------------------------------- 照片页


def test_param_key():
    print("结果与参数的绑定")
    p1 = {"base": "D:\\A\\", "extra": "", "min": 1, "hidden": False, "only": True}
    p2 = {"base": "d:/a", "extra": "", "min": 1, "hidden": False, "only": True}
    p3 = dict(p1, min=2)
    p4 = dict(p1, only=False)
    check("大小写与尾部斜杠算同一组参数",
          CheckerPage._param_key(p1) == CheckerPage._param_key(p2))
    check("最小大小不同则不是同一组",
          CheckerPage._param_key(p1) != CheckerPage._param_key(p3))
    check("比对模式不同则不是同一组",
          CheckerPage._param_key(p1) != CheckerPage._param_key(p4))


def test_photo(tmp):
    print("照片页：模板 + 执行")
    check("模板缺日期变量被拒", not photo_rename.validate_template("abc")[0])
    check("花括号未配对被拒", not photo_rename.validate_template("{date")[0])
    check("合法模板通过", photo_rename.validate_template("{type}_{date}_{time}")[0])

    src = os.path.join(tmp, "psrc")
    os.makedirs(src, exist_ok=True)
    p = os.path.join(src, "a.jpg")
    make(p, 123)
    os.utime(p, (1700000000, 1700000000))
    paths, _ = photo_rename.collect_files(src, False)
    rows = photo_rename.plan_renames(paths, "{type}_{date}_{time}", date_basis="修改日期",
                                     alternate="保留原文件名", workers=1)
    check("计划里状态为 ok", bool(rows) and rows[0]["状态"] == "ok", rows[:1])

    rdir = os.path.join(tmp, "report")
    os.makedirs(rdir, exist_ok=True)
    bad = [dict(rows[0], **{"大小": 999})]
    res_bad = photo_rename.apply_plan(bad, rdir)
    check("文件大小不符时拒绝", res_bad["failed"] == 1 and res_bad["moved"] == 0, res_bad)
    res = photo_rename.apply_plan(rows, rdir)
    check("正常执行成功", res["moved"] == 1 and res["failed"] == 0, res)
    check("台账文件名带时间戳",
          os.path.basename(res["manifest"]).startswith("manifest_"), res["manifest"])


# ---------------------------------------------------------------- 时区


def test_timezone():
    print("视频日期时区")
    dt = photo_media._parse_iso_time("2024-05-01T02:30:00.000000Z")
    expect = datetime.datetime.fromtimestamp(
        datetime.datetime(2024, 5, 1, 2, 30).replace(
            tzinfo=datetime.timezone.utc).timestamp())
    check("带 Z 的时间转成本地", dt == expect, "%s != %s" % (dt, expect))
    naive = photo_media._parse_iso_time("2024-05-01T10:30:00")
    check("不带时区按本地处理", naive == datetime.datetime(2024, 5, 1, 10, 30), naive)


def main():
    tmp = tempfile.mkdtemp(prefix="ft_test_")
    print("临时目录：%s\n" % tmp)
    try:
        test_keyword()
        test_copy_mark()
        test_paths()
        test_scan_skip(tmp)
        test_dedup(tmp)
        test_checker(tmp)
        test_param_key()
        test_photo(tmp)
        test_timezone()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
