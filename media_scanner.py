import os
from collections import deque

PHOTO_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'heic', 'heif', 'tif',
              'tiff', 'raw', 'cr2', 'nef', 'arw', 'dng', 'orf', 'rw2', 'srw'}

VIDEO_EXTS = {'mp4', 'mov', 'avi', 'mkv', 'flv', 'wmv', 'm4v', 'ts', 'webm',
              'mpg', 'mpeg', 'rm', 'rmvb', '3gp', 'ogv', 'm2ts', 'mts'}

AUDIO_EXTS = {'mp3', 'wav', 'flac', 'aac', 'm4a', 'ogg', 'wma', 'ape', 'opus', 'aiff'}

TYPE_MAP = {'photo': PHOTO_EXTS, 'video': VIDEO_EXTS, 'audio': AUDIO_EXTS}

SKIP_DIRS = {'$recycle.bin', '$recycler', 'recycled',
             'system volume information',
             'windows', 'program files', 'program files (x86)',
             'programdata', 'recovery', 'boot', 'efi', 'perflogs',
             'config.msi', '$winreagent', '$sysreset', 'windows.old',
             'msocache', 'documents and settings'}

_ATTR_REPARSE = 0x400

DEFAULT_KINDS = {'video'}


def classify(ext):
    for kind, exts in TYPE_MAP.items():
        if ext in exts:
            return kind
    return None


def scan(root, kinds=None, progress_cb=None, cancel_cb=None):
    kinds = set(kinds or DEFAULT_KINDS)
    results = []
    queue = deque([os.path.normpath(root)])
    count = 0
    while queue:
        if cancel_cb and cancel_cb():
            return results, True
        cur = queue.popleft()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if getattr(st, 'st_file_attributes', 0) & _ATTR_REPARSE:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in SKIP_DIRS:
                            queue.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(entry.name)[1].lower().lstrip('.')
                        kind = classify(ext)
                        if kind is None or kind not in kinds:
                            continue
                        base = os.path.splitext(entry.name)[0]
                        results.append((entry.path, base, ext, kind, st.st_size, st.st_mtime))
                        count += 1
                        if progress_cb and count % 200 == 0:
                            progress_cb(entry.path, count)
        except (PermissionError, OSError):
            continue
    if progress_cb:
        progress_cb(root, count)
    return results, False
