# MTeam Post

本项目把本地媒体识别、MediaInfo、规范重命名、TMDB/豆瓣匹配、V1 私有种子、截图以及 M-Team 发布页填写整合为一个可安装的 Python 包。支持普通视频、整季剧集目录、DVD ISO 和 Blu-ray ISO。程序始终停在最终“发布”按钮之前。

## 在另一台 Windows 电脑复现

### 1. 安装系统依赖

准备以下软件，并确保命令行可以找到它们：

- Python 3.10 或更高版本
- Google Chrome
- MediaInfo CLI（命令为 `mediainfo`）
- FFmpeg（同时需要 `ffmpeg` 和 `ffprobe`）
- [bdinfo-rs](https://github.com/agentjp/bdinfo-rs)（仅自动处理 Blu-ray/UHD ISO 时需要）
- Git

Selenium 会自动寻找或下载与 Chrome 匹配的 ChromeDriver，一般不需要手工下载驱动。
蓝光 ISO 必须使用 BDInfo 格式；Windows 可安装命令行版：

```powershell
winget install agentjp.bdinfo-rs
```

在 PowerShell 中检查：

```powershell
py --version
mediainfo --version
ffmpeg -version
ffprobe -version
bdinfo-rs --version
git --version
```

### 2. 下载并安装本项目

```powershell
git clone https://github.com/haildceu1/mteam-post.git
cd mteam-post
py -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

验证安装：

```powershell
media-title-rename --help
python -m unittest discover -s tests -v
```

### 3. 配置 TMDB（推荐）

TMDB 应填写 API Read Access Token：

```powershell
$env:TMDB_READ_ACCESS_TOKEN = "你的 API Read Access Token"
```

以上只对当前 PowerShell 有效。若要永久保存到当前 Windows 用户：

```powershell
[Environment]::SetEnvironmentVariable(
  "TMDB_READ_ACCESS_TOKEN",
  "你的 API Read Access Token",
  "User"
)
```

永久设置后请重新打开 PowerShell。不要把令牌、Cookie 或请求头提交到 Git。

### 4. 首次登录 M-Team

使用独立 Chrome 配置，避免影响日常 Chrome：

```powershell
media-title-rename mteam-fill --login-only `
  --profile-dir "$env:LOCALAPPDATA\mteam-post\chrome-profile" `
  --url "https://kp.m-team.cc/"
```

在打开的 ChromeDriver 窗口中完成登录；程序检测到 `localStorage auth` 后会自动继续，无需切回 PowerShell 按回车。以后一直复用同一个 `--profile-dir`。

### 5. 一条命令准备并填写发布页

整季剧集：

```powershell
media-title-rename publish "F:\TV\20.22" `
  --apply `
  --profile-dir "$env:LOCALAPPDATA\mteam-post\chrome-profile" `
  --keep-open
```

单个视频或 ISO 只需替换输入路径。该命令会完成重命名、MediaInfo（蓝光 ISO 使用 BDInfo）、4 张截图、V1 私有种子、IMDb/豆瓣链接、分类和简介，并将截图追加在简介末尾。填写/上传前输入 `y` 确认，最后检查页面并手工点击“发布”。

升级项目时执行：

```powershell
git pull
python -m pip install -e .
```

## M-Team 标题重命名

`media-title-rename` 读取 MediaInfo，生成符合 M-Team 影片主标题规则的文件名。它默认只显示预览，只有加上 `--apply` 才会更改文件名。

```powershell
# 预览：从文件名自动识别片名、年份、来源、发布组
media-title-rename "D:\Videos\Lisa.Frankenstein.2024.BluRay.1080p.AVC.DTS-HD.MA5.1-ESiR.mkv"

# 确认改名
media-title-rename "D:\Videos\Lisa.Frankenstein.2024.BluRay.1080p.AVC.DTS-HD.MA5.1-ESiR.mkv" --apply

# 无法从原始文件名判断的内容可以明确填写
media-title-rename "D:\Videos\input.mkv" --title "Lisa Frankenstein" --year 2024 --source "BluRay REMUX" --group ESiR --apply

# DVD ISO：按容量自动使用 DVD5/DVD9
media-title-rename "D:\Sandra 1965 DVDiSo 576p MPEG-2 DD.iso"

# 蓝光 ISO：MediaInfo 无法展开轨道时，从规范文件名回填参数并保留 MOC
media-title-rename "E:\Movie\Sherlock, Jr 1924 MOC Blu-ray 1080p AVC LPCM 2.0-smwy8888.iso"
```

输入目录时会自动递归发现所有子目录中的视频，无需额外参数；确认预览结果无误后再添加 `--apply`。脚本会在执行前检查重名和已存在目标，任何冲突都会阻止整批改名。

来源（例如原盘、REMUX、BDRip、WEB-DL）不能由媒体编码参数可靠判定。脚本会优先采用原文件名中的来源标记；找不到时，在交互终端让你选择，或要求通过 `--source` 明确给出。DVD ISO 会按容量使用 `DVD5` 或 `DVD9`。音频编码和声道数之间不留空格，例如 `DD2.0`、`DD5.1`、`DDP5.1`、`DTS-HD MA5.1`。多音轨时默认选码率最高的音轨，但不在名称中标注音轨数量；需要 `2Audio`、`3Audio` 等标记时可添加 `--audio-count`。

## 准备 M-Team 发布资料

`prepare` 会在媒体文件旁创建一个 `.prepare` 目录，默认生成：

- M-Team 规范标题与分类
- 中文名、原文名和源语言组成的副标题
- 自动匹配的豆瓣链接
- MediaInfo 英文 Text（隐藏本地绝对路径）
- 4 张本地截图
- V1 私有种子
- 供后续网页填写器读取的 `mteam-prepare.json`

```powershell
media-title-rename prepare "F:\20.22\20.22.s01.E01.(2024).HDTV (1080i).by.Romanok8691.ts"

# 全部资料成功后，同时执行规范重命名
media-title-rename prepare "F:\Videos\Example.ts" --apply

# 整季剧集：识别每集的 SxxExx，只探测第一集，生成一个多文件种子，并整批改名
media-title-rename prepare "F:\20.22" --apply

# 分季放在子目录也会自动递归处理
media-title-rename prepare "F:\TV\20.22" --apply

# 自动匹配不确定时可以明确指定
media-title-rename prepare "F:\Videos\Example.ts" `
  --tmdb-id 12345 `
  --douban-url "https://movie.douban.com/subject/1234567/" `
  --category "影剧/综艺/HD"
```

种子配置与 qBittorrent 图示一致：V1、自动分块、`private=1`，Tracker URL、Web 种子、注释和 `source` 默认留空。种子中的文件名使用规范新名称；不带 `--apply` 时源文件保持原名，因此正式做种前应确认并执行重命名。

文件夹模式专用于剧集：它会先检查全部目标文件名，任何一集缺少 `SxxExx` 或发生重名时都不会改动任何文件。文件按季集号排序后只探测第一集（例如 `S01E01`）并生成一份 MediaInfo Text，其分辨率、视频编码和音频参数会用于整季重命名及发布页；TMDB/豆瓣也只查询一次。默认的 4 张截图仍会尽量均匀选自不同集。最终生成一个 V1 私有多文件种子，种子根目录保留原文件夹名，只重命名其中的视频文件。

### TMDB 名称增强

申请 TMDB API Read Access Token 后，在当前 PowerShell 会话中配置：

```powershell
$env:TMDB_READ_ACCESS_TOKEN = "你的 TMDB API Read Access Token"
```

配置后，`prepare` 会查询 TMDB 的电影或剧集接口，并用年份、名称别名和媒体类型选择候选。无法唯一确定时会在终端显示候选供选择。未配置 Token 时仍会使用文件名和豆瓣自动补全；也可以使用 `--tmdb-id` 强制指定条目。

This product uses the TMDB API but is not endorsed or certified by TMDB.

豆瓣自动补全使用名称和年份匹配。找不到可靠结果时，交互模式会让你粘贴链接；也可以始终使用 `--douban-url`。

### ChromeDriver 自动填入 M-Team

`prepare` 完成后，可用 ChromeDriver 打开已登录页面并填入资料。程序会上传种子和截图（需要 `--upload`），但永远停在最终发布按钮之前：

```powershell
media-title-rename mteam-fill "F:\TV\20.22.prepare\mteam-prepare.json" `
  --cookie-file "C:\Secrets\mteam-cookie.txt" `
  --url "https://kp.m-team.cc/upload" `
  --upload
```

### 一条命令完成准备和填表

首次登录完成后，可以使用 `publish` 合并 `prepare --apply` 与网页填写；`--apply` 是必填项，以保证 V1 种子中的文件名和磁盘文件一致。默认会上传种子及前 4 张截图，仍会在真正写入/上传前询问确认，且绝不会点击最终发布按钮：

```powershell
media-title-rename publish "F:\TV\20.22" `
  --apply `
  --profile-dir "$env:LOCALAPPDATA\mteam-post\chrome-profile"
```

电影、DVD ISO、蓝光 ISO 也使用相同命令。`prepare` 的参数可以直接继续使用，例如 `--tmdb-id`、`--douban-url`、`--category`、`--screenshots 4`。若只想填写文字字段而不上传文件，添加 `--no-upload`。

`--cookie-file` 同时接受两种格式：Cookie-Editor 导出的 Netscape Cookie，或从 M-Team 开发者工具复制的请求头。后者会恢复页面使用的 `localStorage` 登录值，不会错误地当成普通 Cookie。请求头、Cookie 和资料包都属于敏感内容，请勿上传到 Git 或发送给他人。

如果使用 CookieCloud，可让 ChromeDriver 使用一个单独的、已安装 CookieCloud 且已经登录的配置目录：

```powershell
media-title-rename mteam-fill "F:\TV\20.22.prepare\mteam-prepare.json" `
  --profile-dir "$env:LOCALAPPDATA\mteam-post\chrome-profile" `
  --url "https://kp.m-team.cc/upload" `
  --upload
```

不要让普通 Chrome 和 ChromeDriver 同时占用同一个配置目录；建议专门建立 `mteam-chrome-profile`。首次使用时先在该配置中手工登录并确认 CookieCloud 同步完成，再运行命令。

也可以让工具直接打开这个专用窗口供首次登录：

```powershell
media-title-rename mteam-fill --login-only `
  --profile-dir "$env:LOCALAPPDATA\mteam-post\chrome-profile" `
  --url "https://kp.m-team.cc/"
```

程序检测到登录成功后会自动继续，配置会保留下来；之后使用同一个 `--profile-dir` 即可，不再需要导出 Cookie 或请求头文件。默认等待 10 分钟，可用 `--login-timeout 1200` 调整。

### M-Team 分类

当前自动推断和 `--category` 支持：

- 电影/SD、电影/HD、电影/DVDiSo、电影/BluRay、电影/Remux
- 影剧/综艺/SD、影剧/综艺/HD、影剧/综艺/BluRay、影剧/综艺/DVDiSo
- 动画、动画/Bluray

动画类型在配置 TMDB 后可根据 Animation 类型自动识别，也可以使用 `--animation` 或 `--category` 明确指定。

### ISO 截图

Windows 下会临时挂载 ISO，从 `BDMV/STREAM` 或 `VIDEO_TS` 中选择最大的正片文件截图，并在完成后卸载；已经由用户挂载的 ISO 不会被卸载。复杂的无缝分支蓝光如果自动选择不正确，可使用：

```powershell
media-title-rename prepare "E:\Movie\Disc.iso" --screenshot-source "M:\BDMV\STREAM\00001.m2ts"
```

### 蓝光 ISO 的 BDInfo

Blu-ray/UHD ISO 不再把 ISO 容器的简略 MediaInfo 填入发布页，而是调用 `bdinfo-rs`：先列出播放列表，默认选择时长最长的一项，再完整扫描并保存经典 BDInfo Text。已知主播放列表时可明确指定：

```powershell
media-title-rename publish "D:\Movie\Disc.iso" `
  --apply `
  --bdinfo-playlist 00005 `
  --profile-dir "D:\Cinema\mteam" `
  --keep-open
```

如果已经用图形版 BDInfo 保存了 Text 报告，可以直接复用，避免再次完整扫描原盘：

```powershell
media-title-rename publish "D:\Movie\Disc.iso" `
  --apply `
  --bdinfo-report "D:\Movie\BDINFO.Disc.txt" `
  --profile-dir "D:\Cinema\mteam" `
  --keep-open
```

`D:\Cinema\tools\BDInfo\BDInfo.exe` 这类 WinForms 图形版不能静默操作；它生成的报告请通过 `--bdinfo-report` 使用。也可以用 `--bdinfo-exe` 或环境变量 `BDINFO_PATH` 指向其他兼容的 BDInfo CLI。
