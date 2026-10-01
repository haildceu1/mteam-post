# TL 超过 10 天的影视资源批量发布

脚本使用本地 `media-title-rename auto` 和已配置的 CLIProxyAPI。TL 做种时长来自登录后的官网 S.T. 列，默认阈值为严格大于 10 天（240 小时），按官网时长降序执行。`--hours` 的单位仍为小时；自定义阈值时可显式指定，例如 `--hours 240`。

从当前仓库直接运行，使用已安装 media-title-rename 的 Python 环境：

```bash
cd /data/Zhyw/media-stack/mteam-post

# 只列出本地候选，还没有进行 M-Team 在线查重。
/data/conda_envs/vllm_n/bin/python scripts/batch_tl_publish.py --list

# 先预览两个：AI 识别、资料、截图、私有上传种子、M-Team 查重。
/data/conda_envs/vllm_n/bin/python scripts/batch_tl_publish.py \
  --preview --web-search --limit 2 \
  --output /data/Zhyw/media-stack/downloads/.mteam-transfer/batch-publish/over-10d

# 发布同一批两个；文件未变且预览已通过时复用现有资料包。
/data/conda_envs/vllm_n/bin/python scripts/batch_tl_publish.py \
  --submit --web-search --limit 2 --interval 180 \
  --output /data/Zhyw/media-stack/downloads/.mteam-transfer/batch-publish/over-10d

# 处理全部候选：去掉 --limit 2。
```

`--submit` 完成发布后，默认下载 M-Team 官方新种子并加入 qB，标签为 `Mteam`。原文件直接复用，不改名、不整理。发布任务之间默认间隔 180 秒；M-Team 搜索还受现有 60 秒请求间隔保护。

候选要求：qB 标签包含 TorrentLeech（不区分大小写）、官网 S.T. 大于阈值、qB 已完整下载、源文件与 qB 文件清单完全相符、资源不超过 200 GiB，且能判定为电影或电视剧。纯音频、游戏/软件、压缩包和分卷、无法确认的资源列入报告，不进入自动发布。

“没有转种过”的判断依据包括 qB 中使用同一目录或同一组 inode 的 M-Team 任务、本地保存的发布 ID，以及 `auto` 发布前的 M-Team 在线查重。`--list` 的候选数只是本地预筛选结果，不能当作已确认未发布的数量。在线查重同时会跳过其他人已有的同版本资源。历史发布记录已丢失且站内种子被隐藏时，仅靠普通搜索不能确认其存在。

资料不足或 AI 置信度未通过的项目保留为 `pending`。站点限流、搜索不完整、模型不可用或提交结果不明确时保存进度并停止批次。已有发布 ID 的资源会跳过；召回失败的已发布资源可用原 `auto` 单项命令续接，不重新发布。

默认安全加载以下已有配置，只在内存使用认证信息：

- TL Cookie：`/data/Zhyw/media-stack/moviepilot-maintenance/config/torrentleech.xlsx`
- 豆瓣 Cookie：`/data/Zhyw/media-stack/moviepilot-maintenance/douban.xlsx`
- qB API 配置：`/data/Zhyw/media-stack/qbittorrent-config/qBittorrent/qBittorrent.conf`
- M-Team 站点：MoviePilot 容器里的站点 ID 1；可通过 `--site-id` 修改。
- TL 网络请求默认使用 Clash `http://127.0.0.1:7890`。

输出默认位于 `/data/Zhyw/media-stack/downloads/.mteam-transfer/batch-publish/<时间戳>/`：

- `summary.md`：候选、成功、跳过及待处理清单。
- `batch.json`、`results.jsonl`：逐项状态和原因。
- `tl-snapshot.json`：本次官网时长快照，无 Cookie。
- `tasks/<qB hash>/`：AI 输入/输出、资料包、截图、上传种子、发布/召回结果和脱敏后的 `auto.log`。

只允许一个批量任务运行；每项发布另有现有资源锁。缓存位于工作区，不使用 `/tmp`。脚本不执行删除 TL 任务或原文件的操作。
