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

**推荐直接用脚本**（自动读 `VER`、校验 CHANGELOG、保护 `dist` 里的运行数据）：

```bat
python run_tests.py     :: 先跑回归，40 项全绿再打包
python build.py         :: 只打 onedir；加 --onefile 同时打 onefile；加 --smoke 打完全自动启一次
```

下面是等价的手工命令，便于排查问题。

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
- `--windowed` 的产物没有控制台，stdout 不可见；启动异常会弹错误框，运行期问题见各页签日志区
- `dist\media_index.db` 若来自冒烟测试可删，首次运行会自动在 exe 同级重建
- `dist\config.json` 是四个页签的配置（算法/线程/窗口/上次页签/各页路径与选项），可删，
  删除后全部回到默认值，首次运行时自动创建
- 重新打包不会继承签名，**每次打包后需重新签名**（若已启用代码签名）

## 冒烟验证

v20261029 起**没有命令行参数**（`--cli` / `--selftest` 已移除），冒烟全靠 GUI：

```bat
:: GUI 能起来（进程存活即通过）
start "" dist\FileToolkit_onedir.exe
```

起来后按这个顺序点一遍（用测试目录，别用真实数据）：

1. exe 同级生成 `media_index.db` → 启动链路完整
2. 查重清理页：选一个含 2 份相同文件的测试目录 → 扫描 → 结果里有 1 组、★ 与 ☑ 正确 → 点「移除」走一遍确认框（可选）
3. 查重(关键字)页：选测试目录 → 故意把「移动到的目录」设成它的子目录 → 应直接弹"路径设置有冲突"
4. 照片重命名页：3~5 张测试图 → 分析 → 计划表有行 → 执行 2~3 条
5. 媒体索引页：选一个小目录扫描 → 列表里出现该路径
