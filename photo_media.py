"""媒体元数据提取：给出「文件 → 拍摄日期 + 相机信息」。

分派顺序按扩展名：
  JPEG/JPEG·exifread  → TIFF·exifread  → PNG·手写 eXIf 解析  → HEIC·pillow_heif+piexif
  视频·ffprobe → ffmpeg → 纯 Python 解析 MP4/MOV 容器

约定：
- 所有函数都不抛异常，失败返回空字段 + warning，由上层决定是否排除该文件
- 第三方库懒加载：缺失时对应格式降级，工具整体仍可用
- 视频后端在首次调用时探测一次并缓存
"""

import os
import re
import struct
import subprocess
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v", ".ts", ".mts", ".m2ts"}
# 能从容器/EXIF 取日期的格式（gif/bmp 没有 EXIF）
EXIF_CAPABLE = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"} | VIDEO_EXTS

DATE_BASIS_SHOOT = "拍摄日期"
DATE_BASIS_MTIME = "修改日期"
DATE_BASIS_CTIME = "创建日期"
DATE_BASES = [DATE_BASIS_SHOOT, DATE_BASIS_MTIME, DATE_BASIS_CTIME]
# 备选基准（仅当主基准是“拍摄日期”且取不到 EXIF 时生效）
ALTERNATE_KEEP = "保留原文件名"
ALTERNATE_DATES = [DATE_BASIS_MTIME, DATE_BASIS_CTIME, ALTERNATE_KEEP]

_VIDEO_BACKEND = None  # None=未探测；"ffprobe"/"ffmpeg"/"py"
_VIDEO_LOCK = Lock()

# MP4 纪元：1904-01-01 00:00:00 UTC 与 Unix 纪元之差
_MP4_EPOCH_OFFSET = 2082844800


class LRUCache:
    """线程安全的 LRU 缓存，用于 EXIF 结果复用（容量与 to_del 对齐：2000）。"""

    def __init__(self, capacity=2000):
        self.capacity = capacity
        self._cache = OrderedDict()
        self._lock = Lock()

    def get(self, key):
        with self._lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def put(self, key, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self.capacity:
                self._cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self._cache.clear()


_META_CACHE = LRUCache(2000)


def empty_meta(warning=""):
    return {
        "DateTimeOriginal": None,
        "Camera": "",
        "Lens": "",
        "ISO": "",
        "Focal": "",
        "Aperture": "",
        "Shutter": "",
        "Source": "",
        "Warning": warning,
    }


# ------------------------------------------------------------------ 工具函数

def _base_dir():
    """程序所在目录，用于查找外挂 ffmpeg/ffprobe。"""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _find_tool(name):
    """在 exe 同级与 tools 子目录查找可执行文件。"""
    base = _base_dir()
    for cand in (
        os.path.join(base, name),
        os.path.join(base, "tools", name),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _run_hidden(cmd, timeout=10):
    """静默执行外部命令，返回 (rc, stdout_bytes, stderr_bytes)。"""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError):
        return -1, b"", b""


def _parse_exif_datetime(text):
    """解析 'YYYY:MM:DD HH:MM:SS' 形式的 EXIF 时间。"""
    import datetime
    if not text:
        return None
    text = str(text).strip().strip("\x00")
    text = text.replace("上午", "").replace("下午", "").strip()
    if not text or text.startswith("0000"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
        if dt.year < 1970 or dt.year > 2100:
            return None
        return dt
    return None


def _mp4_time_to_local(seconds):
    """MP4 mvhd 的 creation_time（1904 起的秒数，UTC）→ 本地时间。"""
    import datetime
    if not seconds:
        return None
    try:
        unix = int(seconds) - _MP4_EPOCH_OFFSET
    except (TypeError, ValueError):
        return None
    if unix <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(unix)
    except (OSError, OverflowError, ValueError):
        return None


def _ratio_text(num, den):
    """把 (num, den) 渲染成可读数值：整值直接给整数，否则给保留两位的小数。"""
    try:
        num, den = int(num), int(den)
    except (TypeError, ValueError):
        return ""
    if not num or not den:
        return ""
    if den == 1:
        return str(num)
    value = num / den
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    return text


def _ratio_value(num, den):
    """把 EXIF 分数转成 float；无法转换返回 None。"""
    try:
        num, den = float(num), float(den)
    except (TypeError, ValueError):
        return None
    if not den:
        return None
    return num / den


def _format_aperture(num, den):
    """光圈：EXIF FNumber 是 (分子, 分母)，渲染为 f/2.8 这种形式。"""
    value = _ratio_value(num, den)
    if value is None:
        return ""
    text = ("%.1f" % value).rstrip("0").rstrip(".")
    return "f/" + text


def _format_shutter(num, den):
    """快门：小于 1 秒显示为 1/N，否则显示为 Ns 或小数秒。"""
    value = _ratio_value(num, den)
    if value is None:
        return ""
    if value >= 1:
        return ("%.1f" % value).rstrip("0").rstrip(".") + "s"
    return "1/%d" % round(1.0 / value) if value > 0 else ""


def _format_focal(num, den):
    """焦距：渲染为整数或一位小数的 mm。"""
    value = _ratio_value(num, den)
    if value is None:
        return ""
    text = ("%.1f" % value).rstrip("0").rstrip(".")
    return text + "mm"


# ------------------------------------------------------------------ JPEG / TIFF

def _meta_from_exifread(path):
    meta = empty_meta()
    try:
        import exifread
    except ImportError:
        meta["Warning"] = "未安装 exifread"
        return meta
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        meta["Warning"] = "EXIF 读取失败"
        return meta
    if not tags:
        meta["Warning"] = "无 EXIF"
        return meta

    def tag(name):
        v = tags.get(name)
        return str(v).strip() if v is not None else ""

    dt = None
    for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
        dt = _parse_exif_datetime(tag(key))
        if dt:
            break
    if dt:
        meta["DateTimeOriginal"] = dt
        meta["Source"] = "exif"
    else:
        meta["Warning"] = "EXIF 无拍摄日期"

    meta["Camera"] = tag("Image Model")
    meta["Lens"] = tag("EXIF LensModel")
    meta["ISO"] = tag("EXIF ISOSpeedRatings")
    meta["Shutter"] = tag("EXIF ExposureTime")
    aperture = tag("EXIF FNumber")
    if aperture:
        if "/" in aperture:
            try:
                n, d = aperture.split("/", 1)
                meta["Aperture"] = _format_aperture(n, d)
            except ValueError:
                meta["Aperture"] = "f/" + aperture
        else:
            meta["Aperture"] = "f/" + aperture
    focal = tag("EXIF FocalLength")
    meta["Focal"] = focal + "mm" if focal and "mm" not in focal else focal
    return meta


# ------------------------------------------------------------------ PNG

def _meta_from_png(path):
    """手写解析 PNG chunk，取 eXIf 块内的 TIFF，交给 piexif 解析。"""
    meta = empty_meta()
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                meta["Warning"] = "非 PNG"
                return meta
            blob = None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                data = f.read(length)
                f.read(4)  # CRC
                if ctype == b"eXIf":
                    blob = data[6:] if data.startswith(b"Exif\x00\x00") else data
                    break
                if ctype == b"IEND":
                    break
    except OSError:
        meta["Warning"] = "PNG 读取失败"
        return meta
    if blob is None:
        meta["Warning"] = "PNG 无 eXIf 块"
        return meta
    return _meta_from_tiff_blob(blob, meta)


# ------------------------------------------------------------------ TIFF 通用

def _meta_from_tiff_blob(blob, meta=None):
    """用 piexif 解析 TIFF/EXIF 字节流。"""
    meta = meta or empty_meta()
    try:
        import piexif
    except ImportError:
        meta["Warning"] = "未安装 piexif"
        return meta
    try:
        exif = piexif.load(blob)
    except Exception:
        meta["Warning"] = "EXIF 解析失败"
        return meta

    def get(ifd, key):
        try:
            raw = exif.get(ifd, {}).get(key)
        except Exception:
            return ""
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="ignore").strip("\x00").strip()
        return str(raw).strip()

    PID = piexif.ExifIFD.DateTimeOriginal
    dt_raw = get("Exif", PID) or get("0th", piexif.ImageIFD.DateTime)
    dt = _parse_exif_datetime(dt_raw)
    if dt:
        meta["DateTimeOriginal"] = dt
        meta["Source"] = "exif"
    else:
        meta["Warning"] = meta["Warning"] or "无拍摄日期"

    make = get("0th", piexif.ImageIFD.Make)
    model = get("0th", piexif.ImageIFD.Model)
    meta["Camera"] = (make + " " + model).strip() if make else model
    meta["Lens"] = get("Exif", piexif.ExifIFD.LensModel)

    raw_iso = exif.get("Exif", {}).get(piexif.ExifIFD.ISOSpeedRatings)
    if isinstance(raw_iso, (tuple, list)):
        raw_iso = raw_iso[0] if raw_iso else None
    meta["ISO"] = str(raw_iso) if raw_iso else ""

    try:
        shutter = exif.get("Exif", {}).get(piexif.ExifIFD.ExposureTime)
        if isinstance(shutter, tuple) and len(shutter) == 2:
            meta["Shutter"] = _format_shutter(*shutter)
    except Exception:
        pass

    try:
        fn = exif.get("Exif", {}).get(piexif.ExifIFD.FNumber)
        if isinstance(fn, tuple) and len(fn) == 2:
            meta["Aperture"] = _format_aperture(*fn)
    except Exception:
        pass

    try:
        fl = exif.get("Exif", {}).get(piexif.ExifIFD.FocalLength)
        if isinstance(fl, tuple) and len(fl) == 2:
            meta["Focal"] = _format_focal(*fl)
    except Exception:
        pass

    return meta


# ------------------------------------------------------------------ HEIC

def _meta_from_heic(path):
    meta = empty_meta()
    try:
        import pillow_heif
    except ImportError:
        meta["Warning"] = "未安装 pillow-heif"
        return meta
    try:
        heif = pillow_heif.read_heif(path, decode_image=False) if hasattr(pillow_heif, "read_heif") \
            else pillow_heif.open_heif(path, convert_hdr_to_8bit=False)
    except Exception:
        meta["Warning"] = "HEIC 读取失败"
        return meta
    try:
        info = getattr(heif, "info", {}) or {}
        blob = info.get("exif")
    except Exception:
        blob = None
    if not blob:
        meta["Warning"] = "HEIC 无 EXIF"
        return meta
    if blob.startswith(b"Exif\x00\x00"):
        blob = blob[6:]
    return _meta_from_tiff_blob(blob, meta)


# ------------------------------------------------------------------ 视频

def detect_video_backend():
    """探测视频元数据后端：ffprobe → ffmpeg → 纯 Python。结果缓存，多线程安全。"""
    global _VIDEO_BACKEND
    if _VIDEO_BACKEND is not None:
        return _VIDEO_BACKEND
    with _VIDEO_LOCK:
        if _VIDEO_BACKEND is not None:
            return _VIDEO_BACKEND
        for name, kind in (("ffprobe.exe", "ffprobe"), ("ffmpeg.exe", "ffmpeg")):
            exe = _find_tool(name)
            if not exe:
                continue
            rc, _out, _err = _run_hidden([exe, "-version"], timeout=8)
            if rc == 0:
                _VIDEO_BACKEND = kind
                return _VIDEO_BACKEND
        _VIDEO_BACKEND = "py"
        return _VIDEO_BACKEND


def _meta_from_ffprobe(path, exe):
    meta = empty_meta()
    rc, out, _err = _run_hidden([
        exe, "-v", "quiet", "-print_format", "json",
        "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
        path,
    ])
    if rc != 0 or not out:
        meta["Warning"] = "ffprobe 失败"
        return meta
    try:
        import json
        data = json.loads(out.decode("utf-8", errors="ignore"))
    except ValueError:
        meta["Warning"] = "ffprobe 输出解析失败"
        return meta
    times = []
    fmt = (data.get("format") or {}).get("tags") or {}
    if fmt.get("creation_time"):
        times.append(fmt["creation_time"])
    for st in data.get("streams") or []:
        tags = st.get("tags") or {}
        if tags.get("creation_time"):
            times.append(tags["creation_time"])
    for text in times:
        dt = _parse_iso_time(text)
        if dt:
            meta["DateTimeOriginal"] = dt
            meta["Source"] = "ffprobe"
            return meta
    meta["Warning"] = "ffprobe 无 creation_time"
    return meta


def _parse_iso_time(text):
    """解析 '2024-05-01T10:30:00.000000Z' 这类 UTC 时间串 → 本地时间。"""
    import datetime
    if not text:
        return None
    text = str(text).strip()
    for trim in (len(text), 19):
        try:
            base = datetime.datetime.strptime(text[:trim], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if text.endswith("Z") or "+00:00" in text:
            try:
                return datetime.datetime.fromtimestamp(
                    base.replace(tzinfo=datetime.timezone.utc).timestamp())
            except (OSError, ValueError):
                return base
        return base
    return None


def _meta_from_ffmpeg(path, exe):
    """ffmpeg -i 把元信息写到 stderr。"""
    meta = empty_meta()
    rc, _out, err = _run_hidden([exe, "-i", path])
    if not err:
        meta["Warning"] = "ffmpeg 无输出"
        return meta
    text = err.decode("utf-8", errors="ignore")
    m = re.search(r"creation_time\s*:\s*(\S+)", text)
    if not m:
        meta["Warning"] = "ffmpeg 无 creation_time"
        return meta
    dt = _parse_iso_time(m.group(1))
    if dt:
        meta["DateTimeOriginal"] = dt
        meta["Source"] = "ffmpeg"
    else:
        meta["Warning"] = "creation_time 解析失败"
    return meta


def _meta_from_mp4(path):
    """纯 Python 解析 MP4/MOV 的 moov/mvhd，取 creation_time。"""
    meta = empty_meta()
    try:
        with open(path, "rb") as f:
            date = _scan_boxes_for_mvhd(f, 0, os.path.getsize(path))
    except OSError:
        meta["Warning"] = "视频读取失败"
        return meta
    if date:
        meta["DateTimeOriginal"] = date
        meta["Source"] = "container"
    else:
        meta["Warning"] = "容器无 creation_time"
    return meta


def _scan_boxes_for_mvhd(f, start, end, depth=0):
    """在 [start, end) 内遍历 box，找到 mvhd 则取出时间。"""
    if depth > 6:
        return None
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return None
        size, btype = struct.unpack(">I4s", head)
        header = 8
        if size == 1:
            big = f.read(8)
            if len(big) < 8:
                return None
            size = struct.unpack(">Q", big)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return None
        body = pos + header
        if btype == b"mvhd":
            return _read_mvhd(f, body, pos + size)
        if btype in (b"moov", b"trak", b"mdia"):
            found = _scan_boxes_for_mvhd(f, body, pos + size, depth + 1)
            if found:
                return found
        pos += size
    return None


def _read_mvhd(f, body, box_end):
    """mvhd: version(1) + flags(3) + [creation(4|8)] + ..."""
    f.seek(body)
    head = f.read(4)
    if len(head) < 4:
        return None
    version = head[0]
    if version == 1:
        raw = f.read(8)
        if len(raw) < 8:
            return None
        seconds = struct.unpack(">Q", raw)[0]
    else:
        raw = f.read(4)
        if len(raw) < 4:
            return None
        seconds = struct.unpack(">I", raw)[0]
    return _mp4_time_to_local(seconds)


# ------------------------------------------------------------------ 入口

def read_meta(path):
    """按扩展名分派，返回 MediaMeta。永不抛异常。结果带 LRU 缓存。"""
    cached = _META_CACHE.get(path)
    if cached is not None:
        return cached
    meta = _read_meta_uncached(path)
    _META_CACHE.put(path, meta)
    return meta


def _read_meta_uncached(path):
    ext = os.path.splitext(path)[1].lower()

    if ext in (".jpg", ".jpeg", ".tif", ".tiff"):
        return _meta_from_exifread(path)
    if ext == ".png":
        return _meta_from_png(path)
    if ext in (".heic", ".heif"):
        return _meta_from_heic(path)
    if ext == ".gif" or ext == ".bmp":
        # 这两种格式没有 EXIF，交给上层按日期基准回退
        return empty_meta("格式无 EXIF")

    if ext in VIDEO_EXTS:
        backend = detect_video_backend()
        if backend == "ffprobe":
            meta = _meta_from_ffprobe(path, _find_tool("ffprobe.exe"))
        elif backend == "ffmpeg":
            meta = _meta_from_ffmpeg(path, _find_tool("ffmpeg.exe"))
        else:
            meta = _meta_from_mp4(path)
        if meta["DateTimeOriginal"] is None and backend != "py":
            fallback = _meta_from_mp4(path)
            if fallback["DateTimeOriginal"] is not None:
                return fallback
        return meta

    return empty_meta("不支持的格式")


# 并发读取默认线程数。实测建议固定 4：这是 I/O 密集型任务，
# 线程再多只会排队并增加调度开销（12 线程实测反而变慢）。
DEFAULT_WORKERS = 4


def read_many(paths, workers=None, on_progress=None, cancel=None):
    """并发读取多个文件的元数据。

    workers 默认 DEFAULT_WORKERS（4）；on_progress(done, total) 每完成一个回调一次。
    cancel 为 threading.Event，置位后尽快返回已收集的部分。
    """
    total = len(paths)
    if not total:
        return {}
    workers = max(1, workers or DEFAULT_WORKERS)
    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(read_meta, p): p for p in paths}
        for fut in as_completed(futures):
            if cancel is not None and cancel.is_set():
                break
            path = futures[fut]
            try:
                results[path] = fut.result()
            except Exception:
                results[path] = empty_meta("读取异常")
            done += 1
            if on_progress is not None:
                on_progress(done, total)
    return results


# ------------------------------------------------------------------ 日期基准

def file_mtime(path):
    """文件修改时间（本地时间）。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).replace(microsecond=0)
    except OSError:
        return None


def file_ctime(path):
    """文件创建时间（Windows 用 st_ctime 近似）。

    注意：Windows 上 os.stat().st_ctime 是创建时间，Linux 上是元数据变更时间。
    本项目只跑 Windows，因此按创建时间使用。
    """
    import datetime
    try:
        st = os.stat(path)
        stamp = getattr(st, "st_birthtime", None) or st.st_ctime
        return datetime.datetime.fromtimestamp(stamp).replace(microsecond=0)
    except OSError:
        return None


def resolve_date(path, meta, basis, alternate):
    """按「主基准 + 备选」决定用于命名的日期。

    返回 (datetime|None, 说明)。
    basis: 拍摄日期 / 修改日期 / 创建日期
    alternate: 保留原文件名 / 修改日期 / 创建日期（仅拍摄日期取不到时生效）
    """
    if basis == DATE_BASIS_MTIME:
        dt = file_mtime(path)
        return dt, "" if dt else "无法读取修改时间"
    if basis == DATE_BASIS_CTIME:
        dt = file_ctime(path)
        return dt, "" if dt else "无法读取创建时间"

    # 主基准＝拍摄日期
    dt = meta.get("DateTimeOriginal") if meta else None
    if dt is not None:
        return dt, ""

    if alternate == DATE_BASIS_MTIME:
        dt = file_mtime(path)
        return dt, "" if dt else "无 EXIF 且无法读取修改时间"
    if alternate == DATE_BASIS_CTIME:
        dt = file_ctime(path)
        return dt, "" if dt else "无 EXIF 且无法读取创建时间"
    return None, "无 EXIF（备选为保留原名）"


def is_supported(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in PHOTO_EXTS or ext in VIDEO_EXTS
