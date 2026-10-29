"""一键打包：读版本号 → 校验 CHANGELOG → 打包 → 平铺到 dist → 清理。

用法：
    python build.py             # 只打 onedir（平时够用）
    python build.py --onefile   # 同时打 onefile（必须先 onedir 后 onefile）
    python build.py --smoke     # 打完自动启动一次，确认能起来

为什么要写它：
  1. 手打 PyInstaller 命令容易漏 --hidden-import，漏了在 exe 里是运行时才报错；
  2. 直接清空 dist 会顺手删掉 media_index.db / config.json——那是用户的运行数据，
     不是构建产物（踩过一次），所以这里先挪走、打完再放回。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
STAGE = os.path.join(ROOT, "_stage")
APP = "FileToolkit"
HIDDEN = ["piexif", "exifread", "PIL.Image"]
RUNTIME_FILES = ["media_index.db", "config.json"]


def read_version():
    """版本号只在 file_toolkit.py 的 VER 常量里定义，这里读同一处。"""
    with open(os.path.join(ROOT, "file_toolkit.py"), encoding="utf-8") as f:
        m = re.search(r'^VER\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else None


def changelog_has(ver):
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        return ("## v%s" % ver) in f.read()


def run(cmd):
    print("  $ " + " ".join(cmd))
    if subprocess.call(cmd, cwd=ROOT) != 0:
        raise SystemExit("命令失败，已中止")


def pyinstaller(mode):
    name = "%s_%s" % (APP, mode)
    distpath = STAGE if mode == "onedir" else DIST
    cmd = [sys.executable, "-m", "PyInstaller", "--%s" % mode, "--windowed", "--noconfirm",
           "--name", name, "file_toolkit.py",
           "--distpath", distpath,
           "--workpath", os.path.join(ROOT, "build_%s" % mode),
           "--specpath", os.path.join(ROOT, "build_%s" % mode)]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    run(cmd)
    return os.path.join(distpath, name)


def backup_runtime():
    """把 dist 里的运行数据先挪到一边（改名加 .keep），打完再放回原位。"""
    saved = {}
    for name in RUNTIME_FILES:
        src = os.path.join(DIST, name)
        if os.path.isfile(src):
            tmp = src + ".keep"
            shutil.move(src, tmp)
            saved[name] = tmp
    return saved


def restore_runtime(saved):
    for name, tmp in saved.items():
        shutil.move(tmp, os.path.join(DIST, name))


def show_products():
    total = 0
    for name in sorted(os.listdir(DIST)):
        full = os.path.join(DIST, name)
        if os.path.isfile(full):
            size = os.path.getsize(full)
            total += size
            print("  %-30s %8.2f MB" % (name, size / 1048576))
        else:
            files = [os.path.join(r, f) for r, _, fs in os.walk(full) for f in fs]
            size = sum(os.path.getsize(f) for f in files)
            total += size
            print("  %-30s %d 个文件 / %.2f MB" % (name + os.sep, len(files), size / 1048576))
    print("  合计 %.2f MB" % (total / 1048576))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true", help="同时打 onefile")
    ap.add_argument("--smoke", action="store_true", help="打完启动一次确认能起来")
    args = ap.parse_args()

    ver = read_version()
    if not ver:
        raise SystemExit("读不到 file_toolkit.py 里的 VER")
    if not changelog_has(ver):
        raise SystemExit("CHANGELOG.md 里没有 v%s 小节，先补记录再打包" % ver)
    print("版本：v%s" % ver)

    shutil.rmtree(STAGE, ignore_errors=True)
    for mode in ("onedir", "onefile"):
        shutil.rmtree(os.path.join(ROOT, "build_%s" % mode), ignore_errors=True)
    os.makedirs(DIST, exist_ok=True)
    saved = backup_runtime()
    try:
        print("[1/3] 打包 onedir")
        out = pyinstaller("onedir")
        shutil.rmtree(os.path.join(DIST, "_internal"), ignore_errors=True)
        shutil.copytree(out, DIST, dirs_exist_ok=True)
        shutil.rmtree(STAGE, ignore_errors=True)

        if args.onefile:
            print("[2/3] 打包 onefile")
            pyinstaller("onefile")
        else:
            print("[2/3] onefile 跳过（需要时加 --onefile）")
    finally:
        restore_runtime(saved)
        for d in ("build_onedir", "build_onefile", "__pycache__"):
            shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)

    print("[3/3] 产物：")
    show_products()

    if args.smoke:
        exe = os.path.join(DIST, "%s_onedir.exe" % APP)
        print("启动冒烟：%s" % exe)
        proc = subprocess.Popen([exe])
        time.sleep(5)
        alive = proc.poll() is None
        if alive:
            proc.terminate()
        print("  5 秒后仍在运行：%s" % alive)
        if not alive:
            raise SystemExit("exe 启动后立刻退出，检查是不是漏了依赖")
    return 0


if __name__ == "__main__":
    sys.exit(main())
