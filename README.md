# MTeam Post

本项目把本地媒体识别、MediaInfo、规范重命名、TMDB/豆瓣匹配、V1 私有种子、截图、M-Team 发布页填写以及字幕批量上传整合为一个可安装的 Python 包。支持普通视频、整季剧集目录、DVD ISO 和 Blu-ray ISO。种子发布始终停在最终“发布”按钮之前；字幕默认也只填表，只有明确添加 `--submit` 才会实际上传。

## 在另一台 Windows 电脑复现

### 1. 安装系统依赖

本节不包含 Python 的安装；请先自行准备 Python 3.10 或更高版本。其余依赖可以在 Windows PowerShell 中使用 WinGet 安装：

```powershell
# 更新 WinGet 软件源，避免旧缓存导致“找不到程序包”
winget source update

# 必需：Git、Google Chrome、MediaInfo CLI、FFmpeg/FFprobe
winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
winget install --id Google.Chrome --exact --source winget --accept-package-agreements --accept-source-agreements
winget install --id MediaArea.MediaInfo --exact --source winget --accept-package-agreements --accept-source-agreements
winget install --id Gyan.FFmpeg --exact --source winget --accept-package-agreements --accept-source-agreements

# 仅处理 Blu-ray/UHD ISO 时需要；普通视频和 DVD ISO 可以不安装
winget install --id agentjp.bdinfo-rs --exact --source winget --accept-package-agreements --accept-source-agreements
```

各软件的用途：

- `mediainfo`：读取普通视频、剧集和 DVD ISO 的媒体轨道信息。
- `ffmpeg`、`ffprobe`：探测视频并生成4张截图，包括 HDR 到 SDR 色调映射。
- Google Chrome：打开并自动填写 M-Team 发布页。
- Git：下载和更新本项目。
- `bdinfo-rs`：为 Blu-ray/UHD ISO 生成 M-Team 所需的 BDInfo Text；当前项目已适配 4.0.0。

Selenium 会通过 Selenium Manager 自动寻找或下载与 Chrome 匹配的 ChromeDriver，通常不需要单独安装 ChromeDriver。Windows 挂载 ISO 使用系统自带的 PowerShell 功能，不需要额外安装虚拟光驱。

WinGet 安装完成后会更新 PATH，但已经打开的 PowerShell 通常不会自动刷新。请关闭所有 PowerShell 窗口，重新打开后再验证：

```powershell
git --version
mediainfo --version
ffmpeg -version
ffprobe -version
bdinfo-rs --version
```

如果不处理 Blu-ray/UHD ISO，最后一条 `bdinfo-rs --version` 检查可以跳过。若某条命令仍提示“无法识别”，先重新登录 Windows；仍无效时使用 `winget list --id <上面的包 ID> --exact` 确认软件是否已经安装。

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
  --keep-open
```

单个视频或 ISO 只需替换输入路径。该命令会完成重命名、MediaInfo（蓝光 ISO 使用 BDInfo）、4 张截图、V1 私有种子、IMDb/豆瓣链接、分类和简介，并在简介末尾自动回车两次后追加截图。填写/上传前输入 `y` 确认，最后检查页面并手工点击“发布”。`publish` 会先读取环境变量 `MTEAM_PROFILE_DIR`；未设置时，Windows 优先复用本机已有的 `D:\Cinema\mteam`，否则使用 `%LOCALAPPDATA%\mteam-post\chrome-profile`，Ubuntu 使用 `${XDG_CONFIG_HOME:-$HOME/.config}/mteam-post/chrome-profile`。通常无需再写 `--profile-dir`，仍可用该参数临时覆盖。

升级项目时执行：

```powershell
git pull
python -m pip install -e .
```

## 在 Ubuntu 22.04 电脑复现

以下命令在 Ubuntu 22.04 x86-64 上验证。普通视频和剧集只需要 MediaInfo 与 FFmpeg；ISO 自动挂载还需要 `udisks2`（UDF 原盘首选）或备用的 `fuseiso`，Blu-ray/UHD ISO 另需 `bdinfo-rs`，自动填写发布页则需要图形桌面和 Google Chrome。

### 1. 安装系统依赖

如果当前 Ubuntu 主机需要通过本机 `7890` 端口访问 GitHub、Google 或 PyPI，先在当前终端设置代理；网络可直连时跳过：

```bash
export HTTP_PROXY="http://127.0.0.1:7890"
export HTTPS_PROXY="$HTTP_PROXY"
```

这两个变量会同时被后续的 `curl`、`git` 和 `pip` 使用，只对当前终端及其子进程生效；需要恢复直连时执行 `unset HTTP_PROXY HTTPS_PROXY`。

```bash
sudo apt update
sudo apt install -y git python3-venv mediainfo ffmpeg udisks2 fuseiso curl ca-certificates

# 需要自动填写 M-Team 发布页时安装 Google Chrome
chrome_deb="$(mktemp --suffix=.deb)"
curl -fL "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" -o "$chrome_deb"
sudo apt install -y "$chrome_deb"
rm -f "$chrome_deb"

# 仅处理 Blu-ray/UHD ISO 时需要；使用 bdinfo-rs 4.0.0 官方安装脚本
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/agentjp/bdinfo-rs/releases/download/v4.0.0/bdinfo-rs-installer.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

验证系统依赖：

```bash
git --version
python3 --version
mediainfo --version
ffmpeg -version
ffprobe -version
fuseiso --version
udisksctl status
google-chrome --version
bdinfo-rs --version
```

不使用网页填写或 Blu-ray ISO 时，可以分别跳过 Chrome 或 `bdinfo-rs` 的安装及检查。若安装器把 `bdinfo-rs` 放到 `~/.local/bin`，请把上面的 `export PATH=...` 加入 `~/.profile`，以后重新登录也能直接调用。

### 2. 下载并安装本项目

```bash
git clone https://github.com/haildceu1/mteam-post.git
cd mteam-post
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

验证安装和 Linux 回归测试：

```bash
media-title-rename --help
python -m unittest discover -s tests -v
```

### 3. 配置 TMDB（推荐）

```bash
export TMDB_READ_ACCESS_TOKEN="你的 API Read Access Token"
```

只需当前终端使用时执行上面一行即可；长期使用可把它安全地配置到 `~/.profile` 或你使用的密钥管理工具中。不要把令牌、Cookie 或请求头提交到 Git。

### 4. 首次登录 M-Team

请在 Ubuntu 图形桌面的终端中运行，不要在没有 `DISPLAY` 的纯 SSH 会话中启动浏览器：

```bash
media-title-rename mteam-fill --login-only \
  --profile-dir "${XDG_CONFIG_HOME:-$HOME/.config}/mteam-post/chrome-profile" \
  --url "https://kp.m-team.cc/"
```

在打开的 ChromeDriver 窗口中完成登录。程序检测到 `localStorage auth` 后会自动继续；以后复用同一个配置目录。

### 5. 一条命令准备并填写发布页

```bash
media-title-rename publish "/data/TV/20.22" \
  --apply \
  --keep-open
```

Ubuntu 下 `publish` 的默认 Chrome 配置目录为 `${XDG_CONFIG_HOME:-$HOME/.config}/mteam-post/chrome-profile`。处理 ISO 时，程序优先通过 `udisksctl` 只读挂载（支持 Blu-ray 使用的 UDF），没有桌面授权时改用免密码 `sudo mount`，最后再回退到 `fuseiso`，截图结束后自动卸载；复杂原盘仍可通过 `--screenshot-source` 明确指定正片文件。

升级项目时执行：

```bash
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

# 4K 蓝光 ISO：4K 会规范为 2160p，x265 轨道会规范为 HEVC，但 ISO 仍按 UHD BluRay 原盘处理
media-title-rename "D:\Movie\Sympathy for Mr Vengeance 2002 4K BluRay x265 DTS-HD.MA.5.1-fda80@CHDBits.iso"
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

# 整季剧集：识别每集的 SxxExx，只探测第一集，按 Season xx 分季整理并整批改名
media-title-rename prepare "F:\20.22" --apply

# 分季放在子目录也会自动递归处理
media-title-rename prepare "F:\TV\20.22" --apply

# 剧集蓝光 ISO：从“第一季/第1碟”识别为 S01D01，并自动使用 BDInfo
media-title-rename prepare "D:\永不者-The.Nevers-{tmdb=80828}" --apply

# 自动匹配不确定时可以明确指定
media-title-rename prepare "F:\Videos\Example.ts" `
  --tmdb-id 12345 `
  --douban-url "https://movie.douban.com/subject/1234567/" `
  --category "影剧/综艺/HD"
```

种子配置与 qBittorrent 图示一致：V1、自动分块、`private=1`，Tracker URL、Web 种子、注释和 `source` 默认留空。种子中的文件名使用规范新名称；不带 `--apply` 时源文件保持原名，因此正式做种前应确认并执行重命名。文件名最开头的已知发布站/来源前缀（例如 `[BDshare.org].`）会在识别片名、生成规范名称和种子路径前自动剥离；中后部的制作组标签（例如 `[TTG]`）不会被误删。制种哈希时终端会显示实时进度条、已读取/总大小、读取速度、预计剩余时间，以及当前文件序号和名称；整季大目录不再只显示开始信息。

制种过程支持自动续传。首次开始哈希时，程序会在目标 `.torrent` 同目录建立两个临时检查点文件：`<种子名>.torrent.resume.json`（输入文件清单和属性）与 `<种子名>.torrent.resume.pieces`（已经完成的 V1 分块哈希）。如果因终端、USB 磁盘或读取错误中断，直接以**完全相同的输入路径和命令**再次运行即可；程序会提示“发现未完成的种子哈希，将从检查点继续”，并从上一个完整分块续算，而不是从 0% 重新读取。为保证种子正确性，续传期间不能移动、改名、修改源文件，也不能改变文件清单、大小或修改时间；发生这些变化时程序会拒绝继续，并明确提示检查点位置。种子成功写出后，两个检查点会自动删除。读取再次失败时，错误会给出具体文件、文件内偏移和全局进度，便于判断是否总在同一位置失败。

文件夹模式专用于剧集：它会先检查全部目标文件名，支持 `S01E01`、`S01E01-E02` 和 `S01E01E02`（自动规范为 `S01E01-E02`）等季集写法；任何一集缺少季集号、根目录缺少年份或发生重名时都不会改动任何文件。执行 `--apply` 时会按 M-Team 的剧集文件夹规则，将根目录改为 `{title}-{year}-[tmdb={tmdb_id}]`（没有可用 TMDB ID 时省略最后一段），自动建立 `Season 01`、`Season 02` 等分季子文件夹，把全部视频移动到对应季并规范改名。文件按季集号排序后只探测第一集并生成一份 MediaInfo Text，其分辨率、视频编码和音频参数会用于整季重命名及发布页；TMDB 主条目只查询一次，并在需要时再查询一次目标季信息，豆瓣会按目标季名称和季数匹配。默认的 4 张截图仍会尽量均匀选自不同集。最终生成一个 V1 私有多文件种子，种子根目录和目录结构都与改名后的结果一致。

例如，输入 `D:\幸存者：真人秀  第七季`，并使用 `--title Survivor --year 2000 --tmdb-id 14658 --apply` 后，目录会变为 `D:\Survivor-2000-[tmdb=14658]\Season 07\`；如果未能取得 TMDB ID，则会变为 `D:\Survivor-2000\Season 07\`，程序会在输出中明确提示。

剧集蓝光 ISO 也支持文件夹模式。季数可以写成 `第一季`、`第1季`、`Season 1` 或 `S01`；碟号可以写成 `第1碟`、`第1盘`、`Disc 1`、`Disk 1` 或 `D01`。例如 `[永不者第一季.The.Nevers.2021][第1碟][TTG].iso` 会规范为含 `S01D01` 的文件名并移入 `Season 01`，第 2 碟相应为 `S01D02`。程序只对第一张光盘运行一次 BDInfo 并从第一张光盘生成 4 张截图，其余光盘复用技术参数；所有光盘仍会一起写入同一个 V1 私有多文件种子。大于 DVD9 容量且没有写 `BluRay` 的 ISO 会按 Blu-ray 原盘识别，目录名中的 `{tmdb=80828}`、`[tmdb=80828]` 也会自动作为 TMDB ID。若容量信息或命名不足以判断来源，可显式添加 `--source BluRay` 或 `--source "UHD BluRay"`。

### TMDB 名称增强

申请 TMDB API Read Access Token 后，在当前 PowerShell 会话中配置：

```powershell
$env:TMDB_READ_ACCESS_TOKEN = "你的 TMDB API Read Access Token"
```

配置后，`prepare` 会查询 TMDB 的电影或剧集接口，并用年份、名称别名和媒体类型选择候选。无法唯一确定时会在终端显示候选供选择。未配置 Token 时仍会使用文件名和豆瓣自动补全；也可以使用 `--tmdb-id` 强制指定条目。

This product uses the TMDB API but is not endorsed or certified by TMDB.

豆瓣自动补全使用名称和年份匹配。对于剧集，程序会从 `SxxExx`、`SxxDxx` 或 `Season xx` 识别季数，先向 TMDB 查询该季的正式名称（例如 `Pearl Islands`），再用“剧名 + 季名/季数”查询豆瓣，并只接受季数完全相同的条目；因此不会把《幸存者》第七季误填成当前搜索结果中的第五十季。若没有找到目标季的可靠条目，链接会留空并提示手工补充。找不到可靠结果时，交互模式会让你粘贴链接；也可以始终使用 `--douban-url` 覆盖自动结果。

### ChromeDriver 自动填入 M-Team

`prepare` 完成后，可用 ChromeDriver 打开已登录页面并填入资料。程序会上传种子和截图（需要 `--upload`），但永远停在最终发布按钮之前：

```powershell
media-title-rename mteam-fill "F:\TV\20.22.prepare\mteam-prepare.json" `
  --cookie-file "C:\Secrets\mteam-cookie.txt" `
  --url "https://kp.m-team.cc/upload" `
  --upload
```

### 一条命令完成准备和填表

首次登录完成后，可以使用 `publish` 合并资料准备与网页填写。程序会先按 `input_path`/`prepared_path` 自动寻找旁边已经完成的 `.prepare` 资料包：找到就直接复用，不会再次执行 MediaInfo/BDInfo、TMDB/豆瓣查询、截图和种子哈希；找不到时才开始新的 `prepare`，这时必须提供 `--apply`。对于输入为单个文件的情况，如果资料包是在未加 `--apply` 时生成的，随后执行 `publish ... --apply` 会把资料包中的 `filename` 安全应用到本地文件，并同步更新 `prepared_path`；因此本地文件名始终与 M-Team 标题/种子内名称一致。目录资料包仍保持原有的整季/分季改名逻辑。默认会上传种子及前 4 张截图，仍会在真正写入/上传前询问确认，且绝不会点击最终发布按钮：

```powershell
# 已有匹配资料包：最简命令，自动复用默认 Chrome 配置
media-title-rename publish "F:\TV\20.22"

# 没有资料包：执行一次新的准备流程
media-title-rename publish "F:\TV\20.22" --apply

# 剧集蓝光 ISO 光盘文件夹：识别“第一季/第1碟、第2碟”，一次准备并填表
media-title-rename publish "D:\永不者-The.Nevers-{tmdb=80828}" --apply

# 即使存在资料包，也强制重新准备
media-title-rename publish "F:\TV\20.22" --refresh-prepare --apply

# 重新获取 M-Team 填写资料，但复用已完成的种子，不重新哈希视频
media-title-rename publish "F:\TV\20.22" --refresh-prepare --reuse-torrent --apply
```

电影、DVD ISO、蓝光 ISO 也使用相同命令。文件名中的通用发布版本标记（例如 `V1`、`V2`）会自动忽略，不会被误判为 BluRay 原盘的地区/版本标注。`prepare` 的参数可以直接继续使用，例如 `--tmdb-id`、`--douban-url`、`--category`、`--screenshots 4`。若只想填写文字字段而不上传文件，添加 `--no-upload`。

`--refresh-prepare --reuse-torrent` 适合需要修正 TMDB/豆瓣、分类、简介、MediaInfo/BDInfo 或截图，但媒体内容和种子内部文件名没有变化的情况。程序会自动找到原资料包，重新探测并更新 M-Team 字段，同时保留原 `.torrent`；不会重新读取整部视频计算分块哈希。刷新前会比较单文件名或电视剧目录/集文件的逻辑路径，若规范名称发生变化会停止并要求去掉 `--reuse-torrent` 完整重新制种，避免页面标题、实际文件名和种子元数据不一致。电视剧资料包若原来尚未使用 `--apply` 生成目录种子，不能在复用种子的同时改根目录名，也需要完整重新制种。

## 批量准备和上传字幕

M-Team 的[官方字幕命名规则](https://wiki.m-team.cc/zh-tw/upload-subtitle-rules)要求：字幕主文件名与种子内对应视频的主文件名相同，并在扩展名前加入语言标识；简体中文使用 `.chs`。例如视频为 `Survivor.S07E01.mkv`，对应字幕应为：

```text
Survivor.S07E01.chs.srt
```

剧集字幕应先打包成 `zip/rar/7z` 再上传。程序接受单个字幕、多个字幕、已有压缩包或包含这些文件的目录；传入多个裸 `.srt/.ass` 时，会自动打成一个 `.chs.zip`。上传页中的语言统一选择“简体中文”，标题默认使用文件名去掉最后一个扩展名后的结果。

先生成《幸存者》第七季的界面测试文件：

```powershell
media-title-rename subtitle-generate "D:\Subtitle-Test\Survivor.S07" `
  --series "Survivor" `
  --season 7 `
  --episode-count 15
```

这会生成 `Survivor.S07E01.chs.srt` 至 `Survivor.S07E15.chs.srt`，以及包含全部15集的 `Survivor.S07.chs.zip`。这些 SRT 是空文件，只能测试选择文件和填表，不能发布；即使添加 `--submit`，程序也会检查 ZIP 内容并拒绝上传空字幕。

手动传入种子 ID，选择测试包并打开字幕页：

```powershell
media-title-rename subtitle-upload `
  --torrent-id 123456 `
  "D:\Subtitle-Test\Survivor.S07\Survivor.S07.chs.zip" `
  --keep-open
```

默认完成以下操作后停在最终“提交”按钮之前：

- 填写纯数字种子 ID；
- 批量增加所需的字幕上传行；
- 选择每个字幕文件或压缩包；
- 将每一行的字幕语言设为“简体中文”；
- 将标题设为对应文件名去掉 `.srt/.ass/.zip/.rar/.7z` 后的名称。

若传入的是一个包含多集裸字幕的目录，程序会按剧集规则先自动打包：

```powershell
media-title-rename subtitle-upload --torrent-id 123456 "D:\Real-Subtitles" --keep-open
```

只有字幕内容、时间轴和文件名都已经人工检查，并确认与该种子内的视频文件一一对应后，才使用 `--submit` 实际上传：

```powershell
media-title-rename subtitle-upload `
  --torrent-id 123456 `
  "D:\Real-Subtitles\Survivor.S07.chs.zip" `
  --submit `
  --keep-open
```

实际提交前程序会再次要求确认。使用 `--submit --yes` 可以跳过确认，但仍不会绕过空字幕/空 ZIP 检查。可同时传入多个合规压缩包，页面会自动增加多行并依次上传：

```powershell
media-title-rename subtitle-upload --torrent-id 123456 `
  "D:\Subs\Part1.chs.zip" `
  "D:\Subs\Part2.chs.zip" `
  --submit
```

字幕功能默认沿用 `publish` 的 Chrome 登录目录：优先读取环境变量 `MTEAM_PROFILE_DIR`；Windows 其次复用已有的 `D:\Cinema\mteam`，否则使用 `%LOCALAPPDATA%\mteam-post\chrome-profile`；Ubuntu 使用 `${XDG_CONFIG_HOME:-$HOME/.config}/mteam-post/chrome-profile`。也可以用 `--profile-dir` 或 `--cookie-file` 临时覆盖。

如果之前已经完成 `prepare`，可直接把资料包 JSON 或整个 `.prepare` 目录交给 `publish`。此模式不需要 `--apply`，并会跳过 MediaInfo/BDInfo、TMDB/豆瓣查询、截图生成、种子哈希和改名；如果改为传入原始单个文件路径并加 `--apply`，程序会复用资料包并只应用其中记录的规范文件名，不会重新生成资料：

```powershell
media-title-rename publish "F:\TV\The Office S01-S09.prepare\mteam-prepare.json" `
  --keep-open

# 也可以直接传 .prepare 目录
media-title-rename publish "F:\TV\The Office S01-S09.prepare"
```

使用原媒体路径时也会默认自动复用，不再需要 `--reuse-prepare`：

```powershell
media-title-rename publish "F:\TV\The Office"
```

兼容参数 `--reuse-prepare` 仍然保留：使用它表示“必须找到现有资料包”，找不到时直接报错而不是重新准备。若媒体内容发生变化，请使用 `--refresh-prepare --apply` 强制重建；若只是资料字段需要更新、媒体内容和种子逻辑文件名不变，则使用上面的 `--refresh-prepare --reuse-torrent`，避免重复哈希。

若希望在其他电脑使用自定义默认配置目录，可以在新终端中设置：

```powershell
[Environment]::SetEnvironmentVariable("MTEAM_PROFILE_DIR", "E:\Cinema\mteam", "User")
```

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

自动分类时，非 DVD/BluRay/Remux 来源按视频高度判断：低于 720p（包括 480p、540p、544p、576p）归入 SD，720p 及以上归入 HD。因此 544p 的《幸存者》第七季会自动选择 `影剧/综艺/SD`；也可以用 `--category` 手工覆盖。

动画类型在配置 TMDB 后可根据 Animation 类型自动识别，也可以使用 `--animation` 或 `--category` 明确指定。

### ISO 截图

Windows 使用系统磁盘映像功能，Ubuntu 依次尝试 `udisksctl`、免密码 `sudo mount` 和 `fuseiso`，都会以临时挂载方式从 `BDMV/STREAM` 或 `VIDEO_TS` 中选择最大的正片文件截图，并在完成后卸载；Windows 下已经由用户挂载的 ISO 不会被卸载。复杂的无缝分支蓝光如果自动选择不正确，可使用：

```powershell
media-title-rename prepare "E:\Movie\Disc.iso" --screenshot-source "M:\BDMV\STREAM\00001.m2ts"
```

JPG 截图会自动识别 HDR10、HDR10+、HLG 和 Dolby Vision，并转换为适合网页显示的 BT.709 SDR。对 Blu-ray M2TS/HEVC 会先解码 3 秒预滚区再截取目标帧，避免随机定位到缺少参考帧的位置而出现整张发白、彩色块或马赛克。普通 SDR 视频不会进行 HDR 色调映射。

### 蓝光 ISO 的 BDInfo

Blu-ray/UHD ISO 不再把 ISO 容器的简略 MediaInfo 填入发布页，而是调用 `bdinfo-rs`：先列出播放列表，默认选择时长最长的一项，再完整扫描并保存经典 BDInfo Text。

带有 `4K` 标记的 ISO 会自动使用 `2160p`；文件名中的 `x265` 会作为 HEVC 视频轨信息，但不会把完整 ISO 误判成 BDRip。PowerShell 双引号路径中的 `@` 不需要转义，也不要写成 `\@`。例如：

```powershell
media-title-rename prepare "D:\W-我要复仇-2002-[tmdb=4689]\我要复仇 (2002) - 4K - BluRay - x265 - DTS-HD.MA.5.1 - fda80@CHDBits.iso" `
  --apply `
  --tmdb-id 4689
```

已知主播放列表时可明确指定：

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

## 常见故障排查

### `prepare` 不接受 `--profile-dir`

`prepare` 只在本地生成技术信息、截图、种子和 JSON，不会打开浏览器，因此没有 `--profile-dir` 参数。需要直接填写 M-Team 发布页时使用：

```powershell
media-title-rename publish "D:\Movie\Disc.iso" `
  --apply `
  --profile-dir "D:\Cinema\mteam"
```

如果已经完成 `prepare`，可传入现有资料包，避免重新扫描和哈希：

```powershell
media-title-rename publish "D:\Movie\Disc.prepare\mteam-prepare.json" `
  --profile-dir "D:\Cinema\mteam"
```

### `Blu-ray ISO 必须使用 BDInfo`

Windows 下先安装并重新打开 PowerShell：

```powershell
winget install agentjp.bdinfo-rs
bdinfo-rs --version
```

Ubuntu 下使用官方安装脚本，并确认 `~/.local/bin` 已加入 `PATH`：

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/agentjp/bdinfo-rs/releases/download/v4.0.0/bdinfo-rs-installer.sh | sh
export PATH="$HOME/.local/bin:$PATH"
bdinfo-rs --version
```

若不方便重启终端，可以暂时用 `--bdinfo-exe "bdinfo-rs 或 bdinfo-rs.exe 的完整路径"`。已有图形版 BDInfo Text 报告时则使用 `--bdinfo-report`。

### Ubuntu 无法自动挂载 ISO

先确认 `udisks2`、备用的 `fuseiso` 与 FUSE 卸载工具可用：

```bash
sudo apt install -y udisks2 fuseiso
udisksctl status
fuseiso --version
fusermount3 --version
```

程序使用 `ro,nosuid,nodev,noexec` 只读挂载 ISO，并在截图完成后自动卸载临时设备或目录。`udisksctl` 需要可用的系统 D-Bus/udisks 服务；没有桌面授权的纯 SSH 会话会继续尝试 `sudo -n mount`，再尝试 `fuseiso`。如果这三种方式都不可用，请先在宿主机挂载 ISO，再用 `--screenshot-source /挂载点/BDMV/STREAM/00001.m2ts` 指定正片文件。

### `REPORT_DEST must be given if BD_PATH is an ISO`

这是旧版 `mteam-post` 调用 `bdinfo-rs 4.0.0` 时缺少 ISO 报告目录造成的。`0.8.2` 起已自动传入 `.prepare` 输出目录。进入项目目录并升级：

```powershell
git pull
python -m pip install -e .
```

升级后直接重新执行原来的 `prepare` 或 `publish` 命令；失败时留下的 `.prepare` 目录无需手工删除。

### `TMDB 查询失败：<urlopen error timed out>`

这表示连接 TMDB 超时，通常不是 Token 类型或密钥错误；错误 Token 一般会返回 HTTP 401。可稍后重试，并用 `--tmdb-id 4689` 明确指定条目。无法访问 TMDB 时程序会回退到文件名和豆瓣结果，也可使用 `--offline` 禁止联网查询。

### PowerShell 路径中包含 `@`

双引号中的 `@` 是普通字符，不要在它前面添加反斜杠：

```powershell
# 正确
"D:\Movie\Film-fda80@CHDBits.iso"

# 错误：会被当成多一级目录
"D:\Movie\Film-fda80\@CHDBits.iso"
```
