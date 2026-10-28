# File Toolkit

Windows 本地文件整理工具箱：按内容查重、按关键字归类、跨磁盘媒体索引。
纯 Python 标准库 + tkinter，PyInstaller 打包，目标机无需安装 Python。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.13-3776AB.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)

## 功能

**查重清理** —— 按内容哈希找出完全相同的文件。大小预筛后只对候选文件计算哈希，支持
xxh3-128 / MD5 / SHA-1 / BLAKE2b / SHA-256 等算法。副本不删除，移动到待移除目录并留一份对照副本。

**关键字整理** —— 从文件名提取「字母 + 数字」关键字，按关键字与重复情况生成归类计划。
两阶段执行：先只读分析产出 `plan.csv`，人工核对后再照表执行。

**媒体索引** —— 扫描多个磁盘/目录的照片、视频、音频，只把文件名与元信息写入本机 SQLite。
拔盘后仍可按关键字跨盘查重、批量重命名、导出 CSV。

**照片重命名** —— 按 EXIF 拍摄日期 + 模板批量重命名照片/视频。日期基准可选
（拍摄日期 / 修改日期 / 创建日期），无 EXIF 时可回退到文件时间。同样两阶段：
先出 `plan.csv` 核对，再执行并写 `manifest.csv` 台账。同名冲突自动加 `_001`，永不覆盖。
支持中途停止、隐藏无需改名的行、把个别文件移出本次计划。

## 安全边界

这个工具会移动和重命名你的文件，因此所有写操作都遵守以下原则：

- **没有静默操作**：执行前一律弹窗确认，列明数量与目标路径
- **移除 = 移动**：副本移入待移除目录，并在同级留一份 `[保留] 原名` 供人工比对，确认无误后再自行清空
- **两阶段执行**：整理页先只读分析，`plan.csv` 可人工修改动作列，再执行
- **本地运行**：不联网、不上报、不需要管理员权限

> ⚠️ 请先在小范围目录试跑，确认结果符合预期后再用于重要数据。软件按 MIT 协议"现状"提供，作者不对数据丢失承担责任。

## 快速开始

从 [Releases](../../releases) 下载后解压，双击即可：

```
dist\
├── FileToolkit_onedir.exe      # 双击运行（需同目录的 _internal\）
├── FileToolkit_onefile.exe     # 单文件形态
└── _internal\                  # 依赖，勿删
```

无需安装 Python。首次运行会在 exe 同级生成 `media_index.db`（媒体索引库）。

## 命令行

```bat
:: 关键字整理（只分析，不移动）
FileToolkit.exe --cli --src <目录> [--min-size <KB>]

:: 查重页回归验证
FileToolkit.exe --selftest <目录> [--extra <目录>] [--all]
```

## 技术栈

- 语言：Python 3（标准库为主）
- 可选依赖：[xxhash](https://pypi.org/project/xxhash/)（未安装时自动回退 MD5，功能不受影响）
- 照片重命名依赖：[pillow-heif](https://pypi.org/project/pillow-heif/)（含 Pillow）、
  [piexif](https://pypi.org/project/piexif/)、[exifread](https://pypi.org/project/exifread/)；
  缺失时对应格式降级（HEIC 不可用等）
- 存储：SQLite（仅媒体索引页使用，`sqlite3` 内置）
- 界面：tkinter / ttk
- 打包：PyInstaller

## 构建

见 [BUILD.md](BUILD.md)。

## 项目结构

```
file_toolkit.py        # 入口：共用条、页签容器、查重清理页、关键字整理页、CLI
media_page.py          # 媒体索引页
media_db.py            # 媒体库 SQLite 层
media_scanner.py       # 媒体遍历与类型分类
media_query.py         # CSV 导出
photo_page.py          # 照片重命名页
photo_rename.py        # 模板校验、命名生成、计划与执行
photo_media.py         # EXIF / HEIC / 视频元数据提取
config.py              # config.json 读写（照片页配置）
dedup_tool.py          # 合并前的整理工具（行为对照用）
same_file_checker.py   # 合并前的查重工具（行为对照用）
```

## 文档

| 文件 | 内容 |
|---|---|
| [BUILD.md](BUILD.md) | 构建命令、产物结构、冒烟验证 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [TODO.md](TODO.md) | 待处理事项 |

## 版本约定

版本号＝交付日期（`YYYYMMDD`），同日多次交付加 `a`/`b` 后缀。版本号只存在于 `file_toolkit.py` 的 `VER` 常量和窗口标题中，产物文件名不带版本号。

## 贡献

欢迎提 Issue 反馈问题。提交代码前请先开 Issue 讨论，避免方向不一致。

## License

[MIT](LICENSE) © 2026 File Toolkit contributors
