# 构建与打包

两个形态的产物都**直接平铺在 `dist\` 下**，共用一个 `_internal\`；文件名不带版本号，用后缀区分形态
（版本号只存在于 `VER` 常量和窗口标题中）。

## 依赖准备

```bat
python -m pip install xxhash pillow-heif piexif exifread
```

说明：
- `xxhash` 缺失时查重自动回退 MD5（功能不受影响，速度降一个数量级）
- `pillow-heif` / `piexif` / `exifread` 供照片重命名页使用；缺失时对应格式降级（HEIC 不可用等）
- `pillow-heif` 会连带安装 `Pillow`（硬依赖），不支持省略

环境要求：Python 3.13（带 tkinter）、PyInstaller 6.x。实测 Python 3.13.14 + PyInstaller 6.21.0 可直接打包。

## 构建命令

**必须带三个 `--hidden-import`**：`piexif` / `exifread` / `PIL.Image` 在代码里是函数内懒加载的，
PyInstaller 的静态分析发现不了，不加会在运行时 `ImportError`。

```bat
cd /d D:\AI\file_toolkit
del /q dist\*.* 2>nul

set HIDDEN=--hidden-import piexif --hidden-import exifread --hidden-import PIL.Image

:: 1) onedir —— 先输出到暂存目录，再把内容平铺进 dist
python -m PyInstaller --onedir --windowed --noconfirm ^
    --name FileToolkit_onedir file_toolkit.py ^
    --distpath _stage --workpath build_onedir --specpath build_onedir %HIDDEN%
xcopy /e /i /y "_stage\FileToolkit_onedir\*" "dist\" >nul
rmdir /s /q _stage

:: 2) onefile —— 直接输出到 dist
python -m PyInstaller --onefile --windowed --noconfirm ^
    --name FileToolkit_onefile file_toolkit.py ^
    --distpath dist --workpath build_onefile --specpath build_onefile %HIDDEN%

:: 3) 清理中间产物（spec 就在 build 目录里，删目录即可）
rmdir /s /q build_onedir build_onefile
rmdir /s /q __pycache__ 2>nul
```

**执行顺序有要求**：必须先 onedir 平铺，再打 onefile。反过来会让 onefile 构建覆盖 `dist\_internal`。

## 产出结构

```
dist/
├── _internal/                  # 依赖，约 1050 个文件（含 Pillow、libheif、xxhash）
├── FileToolkit_onedir.exe      # 目录形态主程序，约 5.4 MB
└── FileToolkit_onefile.exe     # 单文件形态，约 36 MB
```

合计约 132 MB。

## 要点

- **`FileToolkit_onefile.exe` 与 `_internal\` 同目录时可正常运行**：PyInstaller 引导器检测到同级
  `_internal\` 就直接使用，跳过自解压到 `%TEMP%`。单独把它拷走则退回自解压模式，
  在拦截该行为的机器上会报 `Could not create temporary directory`
- 实测 `D:` 盘会拦截 onefile 的自解压（`Could not create temporary directory`），`C:` 盘正常；
  `D:` 盘请用 onedir 版
- `--windowed` 的产物没有控制台，stdout 不可见；异常看 `%TEMP%\filetoolkit_cli_error.txt`
- `dist\media_index.db` 若来自冒烟测试可删，首次运行会自动在 exe 同级重建
- `dist\config.json` 是照片重命名页的配置，可删，首次运行自动创建
- 重新打包不会继承签名，**每次打包后需重新签名**（若已启用代码签名）

## 冒烟验证

打包后建议各跑一次：

```bat
:: GUI 能起来（进程存活即通过）
start "" dist\FileToolkit_onedir.exe

:: CLI 参数保护（缺参应打印用法并返回 2）
dist\FileToolkit_onefile.exe --selftest
```

GUI 起来后会在 exe 同级生成 `media_index.db`，说明启动链路完整。
