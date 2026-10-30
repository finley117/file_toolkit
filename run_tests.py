"""回归测试：不启动界面，直接调用核心函数跑一遍。

用法：
    python run_tests.py

全部通过时返回 0，有失败返回 1（可用于打包前的一道闸门）。
所有测试都在系统临时目录里造文件，不碰真实素材。
"""

import datetime
import os
import shutil
import struct
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import media_db                      # noqa: E402
import media_query                   # noqa: E402
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
    meta = {"Camera": "Canon: EOS/R5",
            "DateTimeOriginal": datetime.datetime(2024, 5, 1, 10, 30)}
    stem = photo_rename.build_stem("{camera}_{date}_{time}", meta, ".jpg", original="x")
    check("相机值非法字符被清理",
          ":" not in stem and "/" not in stem and "CANON EOS R5" in stem, stem)

    sd = os.path.join(tmp, "skip")
    os.makedirs(sd, exist_ok=True)
    for n in ("a.jpg", "b.tar.gz", "c.png"):
        make(os.path.join(sd, n), 10)
    kept, info = photo_rename.collect_files(
        sd, False, skip_extensions=photo_rename.parse_skip_extensions("*.jpg tar.gz"))
    check("跳过扩展名支持 *.jpg 与多段后缀",
          [os.path.basename(p) for p in kept] == ["c.png"] and info["已跳过扩展名"] == 2,
          (kept, info))

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

    taken = {"IMG_1.JPG"}
    check("候选名等于自身时不递增（幂等）",
          photo_rename.unique_name("IMG_1", ".JPG", taken, own_name="IMG_1.JPG") == "IMG_1.JPG",
          taken)

    d = os.path.join(tmp, "uniq")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "x.jpg"), exist_ok=True)
    got = photo_rename.unique_name("x", ".jpg", set(), directory=d)
    check("同名文件夹不被覆盖", got == "x_001.jpg", got)

    u = os.path.join(src, "u.jpg")
    make(u, 50)
    rows_u = [{"序号": 1, "所在目录": src, "原文件名": "u.jpg",
               "新文件名": "u_renamed.jpg", "拍摄日期": "", "相机": "",
               "状态": "ok", "原因": "", "大小": 0, "大小未知": True}]
    res_u = photo_rename.apply_plan(rows_u, rdir)
    check("大小未知时仍执行（跳过大小校验）",
          res_u["moved"] == 1 and res_u["failed"] == 0, res_u)


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
    plus8 = photo_media._parse_iso_time("2024-05-01T18:30:00+08:00")
    expect8 = datetime.datetime.fromtimestamp(
        datetime.datetime(2024, 5, 1, 10, 30, tzinfo=datetime.timezone.utc).timestamp())
    check("带 +08:00 偏移正确换算", plus8 == expect8, "%s != %s" % (plus8, expect8))


# ---------------------------------------------------------------- 媒体库 SQLite


def test_media_db(tmp):
    print("媒体库 SQLite 层")
    dbfile = os.path.join(tmp, "media_test.db")
    orig = media_db.db_path
    media_db.db_path = lambda: dbfile      # 路径注入：测试库放临时目录
    try:
        c = media_db.conn()
        media_db.init_db(c)
        root_a = os.path.join(tmp, "diskA")
        root_b = os.path.join(tmp, "diskB")
        pa = media_db.upsert_scan_path(c, "盘A", root_a)
        pb = media_db.upsert_scan_path(c, "盘B", root_b)
        media_db.insert_files(c, pa, [
            (os.path.join(root_a, "abc123.mp4"), "abc123", "mp4", "video", 10, 1.0),
            (os.path.join(root_a, "zzz.mp4"), "zzz", "mp4", "video", 20, 2.0),
        ])
        media_db.insert_files(c, pb, [
            (os.path.join(root_b, "abc123.mp4"), "abc123", "mp4", "video", 10, 1.0),
            (os.path.join(root_b, "IMG_2024.jpg"), "IMG_2024", "jpg", "photo", 30, 3.0),
        ])
        c.commit()
        check("入库后各路径计数正确",
              [r[3] for r in media_db.list_paths(c)] == [2, 2],
              media_db.list_paths(c))
        dups = media_db.find_duplicates(c)
        check("跨盘同关键字命中 1 组",
              len(dups) == 1 and dups[0][0] == "ABC123", [d[0] for d in dups])
        check("该组含 2 个文件", bool(dups) and dups[0][1] == 2)

        # 改关键字（会写 manual），再整体重建规则，验证手工结果不被覆盖
        ok = media_db.rename_keyword(c, "ABC123", "XYZ789")
        media_db.apply_rule_to_keywords(c, media_db.DEFAULT_RULE)
        c.commit()
        dups2 = media_db.find_duplicates(c)
        check("改关键字在规则重建后仍保留",
              ok and len(dups2) == 1 and dups2[0][0] == "XYZ789",
              [d[0] for d in dups2])
        check("应用规则后 needs_rebuild 为 False", not media_db.needs_rebuild(c))

        # 单文件改成手工关键字：规则会算出 IMG_2024，重建后应被 manual 的 MYWORD 覆盖
        fp = os.path.join(root_b, "IMG_2024.jpg")
        media_db.rename_file_to_keyword(c, fp, fp, "MYWORD")
        media_db.apply_rule_to_keywords(c, media_db.DEFAULT_RULE)
        c.commit()
        kws = [w for w, _n, _d in media_db.list_path_keywords(c, pb)]
        check("手工关键字 MYWORD 覆盖规则结果",
              "MYWORD" in kws and "IMG_2024" not in kws, kws)

        pb_again = media_db.upsert_scan_path(c, "盘B改名", root_b)
        cnt = c.execute('SELECT COUNT(*) FROM files WHERE path_id=?', (pb,)).fetchone()[0]
        check("重复 upsert 保留 path id 与文件",
              pb_again == pb and cnt == 2, (pb_again, pb, cnt))

        before_kw = c.execute('SELECT COUNT(*) FROM file_keywords').fetchone()[0]
        ev = threading.Event()
        ev.set()
        ok_cancel = media_db.apply_rule_to_keywords(c, media_db.DEFAULT_RULE, cancel=ev)
        after_kw = c.execute('SELECT COUNT(*) FROM file_keywords').fetchone()[0]
        check("取消重建：不写库、关键字不变",
              ok_cancel is False and after_kw == before_kw,
              (ok_cancel, before_kw, after_kw))

        calls = []
        media_db.apply_rule_to_keywords(
            c, media_db.DEFAULT_RULE, on_progress=lambda d, t: calls.append((d, t)))
        check("重建进度回调收尾于 total/total",
              bool(calls) and calls[-1][0] == calls[-1][1], calls[-5:])

        # 目录改名同步前缀与索引根
        new_b = os.path.join(tmp, "diskB2")
        media_db.rename_folder(c, root_b, new_b)
        c.commit()
        old_n = c.execute("SELECT COUNT(*) FROM files WHERE full_path LIKE ?",
                          (root_b + os.sep + "%",)).fetchone()[0]
        new_n = c.execute("SELECT COUNT(*) FROM files WHERE full_path LIKE ?",
                          (new_b + os.sep + "%",)).fetchone()[0]
        check("目录改名后旧前缀 0 条、新前缀 2 条", old_n == 0 and new_n == 2, (old_n, new_n))
        roots = [r[2] for r in media_db.list_paths(c)]
        check("索引根已更新", new_b in roots and root_b not in roots, roots)

        # 删除路径 → 级联清文件
        media_db.delete_scan_path(c, pa)
        c.commit()
        left = c.execute("SELECT COUNT(*) FROM files WHERE path_id=?", (pa,)).fetchone()[0]
        check("删除路径后其文件级联清空", left == 0, left)
        c.close()
    finally:
        media_db.db_path = orig


# ---------------------------------------------------------------- 照片/视频元数据


def test_photo_meta(tmp):
    print("照片/视频元数据提取")
    try:
        from PIL import Image
        import piexif
    except ImportError:
        print("  [SKIP] 未安装 Pillow/piexif")
        return

    exif = {"0th": {piexif.ImageIFD.Make: b"Canon", piexif.ImageIFD.Model: b"EOS R5"},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2024:05:01 10:30:00",
                     piexif.ExifIFD.ISOSpeedRatings: 400,
                     piexif.ExifIFD.FNumber: (28, 10),
                     piexif.ExifIFD.FocalLength: (50, 1)}}
    jpg = os.path.join(tmp, "exif.jpg")
    Image.new("RGB", (16, 12), (9, 9, 9)).save(jpg, exif=piexif.dump(exif))
    m = photo_media.read_meta(jpg)
    check("JPEG EXIF 拍摄日期",
          m["DateTimeOriginal"] == datetime.datetime(2024, 5, 1, 10, 30), m["DateTimeOriginal"])
    check("JPEG 相机＝厂商+型号", m["Camera"] == "Canon EOS R5", m["Camera"])
    check("光圈化简为 f/2.8", m["Aperture"] == "f/2.8", m["Aperture"])
    check("焦距带 mm", m["Focal"] == "50mm", m["Focal"])

    plain = os.path.join(tmp, "plain.jpg")
    Image.new("RGB", (16, 12), (1, 2, 3)).save(plain)
    m2 = photo_media.read_meta(plain)
    check("无 EXIF 的 JPEG 无日期且带告警",
          m2["DateTimeOriginal"] is None and bool(m2["Warning"]), m2["Warning"])
    st = os.stat(plain)
    os.utime(plain, (st.st_atime, st.st_mtime + 10))
    check("缓存键含 mtime：文件变化后重新读取",
          photo_media.read_meta(plain) is not m2)

    png = os.path.join(tmp, "plain.png")
    Image.new("RGB", (16, 12), (4, 5, 6)).save(png)
    m3 = photo_media.read_meta(png)
    check("无 eXIf 的 PNG 给出明确告警", "eXIf" in m3["Warning"], m3["Warning"])

    png2 = os.path.join(tmp, "with_exif.png")
    Image.new("RGB", (16, 12), (7, 8, 9)).save(png2)
    data = open(png2, "rb").read()
    ihdr_end = 8 + 8 + 13 + 4                     # 签名 + IHDR 头 + 数据 + CRC
    payload = b"Exif\x00\x00" + piexif.dump(exif)
    chunk = struct.pack(">I", len(payload)) + b"eXIf" + payload + struct.pack(">I", 0)
    open(png2, "wb").write(data[:ihdr_end] + chunk + data[ihdr_end:])
    m4 = photo_media.read_meta(png2)
    check("PNG eXIf 块读出日期",
          m4["DateTimeOriginal"] == datetime.datetime(2024, 5, 1, 10, 30), m4["DateTimeOriginal"])

    # MP4 mvhd：UTC 纪元换算 → 本地时间
    mp4 = os.path.join(tmp, "clip.mp4")
    utc = datetime.datetime(2024, 5, 1, 2, 30, 0, tzinfo=datetime.timezone.utc)
    secs = int((utc - datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())
    mvhd_body = (b"\x00\x00\x00\x00" + struct.pack(">II", secs, secs)
                 + struct.pack(">II", 1000, 5000) + b"\x00" * 80)
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\x00\x00\x02\x00"
    open(mp4, "wb").write(ftyp + moov)
    m5 = photo_media.read_meta(mp4)
    expect = datetime.datetime.fromtimestamp(secs - photo_media._MP4_EPOCH_OFFSET)
    check("MP4 mvhd 日期转本地", m5["DateTimeOriginal"] == expect,
          "%s != %s" % (m5["DateTimeOriginal"], expect))

    # 日期基准与备选回退
    dt, _ = photo_media.resolve_date(plain, m2, "拍摄日期", "修改日期")
    check("无 EXIF 时回退修改日期", dt is not None)
    dt2, _ = photo_media.resolve_date(jpg, m, "拍摄日期", "保留原文件名")
    check("有 EXIF 时优先拍摄日期",
          dt2 == datetime.datetime(2024, 5, 1, 10, 30))
    dt3, _ = photo_media.resolve_date(plain, m2, "拍摄日期", "保留原文件名")
    check("备选保留原名则无日期", dt3 is None)

    # 并发读取与 LRU 缓存
    res = photo_media.read_many([jpg, plain, png], workers=2)
    check("并发读取全部返回", len(res) == 3, list(res))
    check("LRU 缓存命中同一对象", photo_media.read_meta(jpg) is photo_media.read_meta(jpg))


def test_heic(tmp):
    """顺带完成 R-37 的外部验证：read_heif(decode_image=False) 是否可用。"""
    print("HEIC 元数据")
    try:
        import pillow_heif
        from PIL import Image
        import piexif
    except ImportError:
        print("  [SKIP] 未安装 pillow-heif / Pillow / piexif")
        return
    p = os.path.join(tmp, "t.heic")
    blob = piexif.dump({"0th": {},
                        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2023:08:09 07:06:05"}})
    saved, errs = False, []
    attempts = (
        lambda: pillow_heif.save_heif(p, Image.new("RGB", (8, 8), (1, 2, 3)), exif=blob),
        lambda: pillow_heif.from_pillow(Image.new("RGB", (8, 8), (1, 2, 3))).save(p, exif=blob),
    )
    for fn in attempts:
        try:
            fn()
            saved = True
            break
        except Exception as e:
            errs.append(str(e))
    if not saved:
        print("  [SKIP] 无法构造 HEIC 样本：%s" % "; ".join(errs))
        return
    m = photo_media.read_meta(p)
    check("read_heif(decode_image=False) 读出 EXIF 日期",
          m["DateTimeOriginal"] == datetime.datetime(2023, 8, 9, 7, 6, 5), m)


# ---------------------------------------------------------------- 媒体关键字


def test_media_keywords():
    import re
    rule = re.compile(media_db.DEFAULT_RULE, re.IGNORECASE)
    print("媒体关键字")
    check("规则匹配不到 -> 回退 stem 原名",
          media_db.extract_keywords("IMG_2024", rule) == ["IMG_2024"],
          media_db.extract_keywords("IMG_2024", rule))
    check("规则命中 -> 取字母+数字",
          media_db.extract_keywords("DSC0001", rule) == ["DSC0001"],
          media_db.extract_keywords("DSC0001", rule))
    check("规则为空 -> 用 stem 原名",
          media_db.extract_keywords("x", None) == ["X"],
          media_db.extract_keywords("x", None))
    check("@ 文件名匹配不到 -> 回退完整 stem",
          media_db.extract_keywords("a@b", rule) == ["A@B"],
          media_db.extract_keywords("a@b", rule))
    check("@ 后缀匹配成功 -> 仍取后缀",
          media_db.extract_keywords("song@live1", rule) == ["LIVE1"],
          media_db.extract_keywords("song@live1", rule))
    check("@ 后缀失败 -> 用全名跑规则",
          media_db.extract_keywords("abc123@xyz", rule) == ["ABC123"],
          media_db.extract_keywords("abc123@xyz", rule))
    check("stem 自带点 -> 不被二次截断",
          media_db.extract_keywords("my.photo", rule) == ["MY.PHOTO"],
          media_db.extract_keywords("my.photo", rule))


def test_csv_export(tmp):
    print("媒体 CSV 导出")
    path = os.path.join(tmp, "dups.csv")
    media_query.export_csv(path, [
        ("=1+1", 2, [("@dev", "=root", "=path", ".jpg", 10, None)]),
    ])
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    check("CSV 公式前缀与 None 为空",
          "'=1+1" in text and "'@dev" in text and "'=root" in text
          and "'=path" in text and "None" not in text, text)


def main():
    tmp = tempfile.mkdtemp(prefix="ft_test_")
    print("临时目录：%s\n" % tmp)
    try:
        test_keyword()
        test_media_keywords()
        test_csv_export(tmp)
        test_copy_mark()
        test_paths()
        test_scan_skip(tmp)
        test_dedup(tmp)
        test_checker(tmp)
        test_param_key()
        test_photo(tmp)
        test_timezone()
        test_media_db(tmp)
        test_photo_meta(tmp)
        test_heic(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
