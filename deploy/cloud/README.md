# 云端离线运行方案

这里的“离线”指你的电脑可以关机，但亚托莉仍然在线。实现条件是：

- 亚托莉服务运行在一台 24 小时在线的设备上，比如云服务器、NAS、软路由或旧电脑。
- NapCat/QQ 也在同一台在线设备上保持登录。
- 模型接口在云端可访问。电脑关机后，`127.0.0.1:11434` 这种本机 Ollama 地址不会再可用，除非 Ollama 也装在云服务器上。

## 推荐结构

```text
云服务器 / NAS
├─ NapCat QQ，登录你的机器人 QQ
├─ 亚托莉服务，监听 ws://127.0.0.1:8765/onebot
└─ 模型接口：云端 Ollama 或 OpenAI 兼容 API
```

不要把 `8765` 端口直接暴露到公网。NapCat 和亚托莉在同一台机器上时，用 `127.0.0.1` 最稳。

## Ubuntu 一键安装服务

把项目复制到服务器后，在项目根目录执行：

```bash
sudo bash deploy/cloud/install-ubuntu.sh
```

从另一份检出覆盖升级时，安装器保留部署目录中的 `.env`、`data/`、`logs/` 和虚拟环境。发行版附带的语音词典只补齐缺失文件。

然后编辑：

```bash
sudo nano /opt/atri-qq-bot/.env
```

至少检查这些项：

```env
BOT_QQ=YOUR_BOT_QQ
OWNER_QQ=你的QQ号
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_MODEL=qwen2.5:7b
MORNING_GREETING_TIME=07:30
```

启动亚托莉：

```bash
sudo systemctl start atri-qq-bot
sudo systemctl status atri-qq-bot
```

查看日志：

```bash
sudo journalctl -u atri-qq-bot -f
```

## NapCat 配置

NapCat 的 OneBot v11 反向 WebSocket 地址保持：

```text
ws://127.0.0.1:8765/onebot
```

如果 NapCat 和亚托莉不在同一台机器上，需要用内网、隧道或防火墙白名单连接，不建议直接公网裸奔。

## 群图归属、回复预算和审核补图

升级前记录当前 Git SHA，并备份生产 `.env`、记忆和表情审核目录。配置值可参考 `.env.example`。P0 的 `IMAGE_ATTRIBUTION_ENABLED` 和 `TURN_BUDGET_ENABLED` 独立控制图片选择和回复预算，默认开启。视觉单图和材料阶段默认分别为 45 秒和 90 秒，进入回复处理后的预算为 120 秒。普通多段回复接近时限时会缩短拟人停顿。约 125 秒是材料桶最多 3 秒、处理 120 秒和临近截止时的 2 秒本地发送余量。会话排队、QQ 确认和语音合成的耗时需分别核对。

P1 可分别开启 `MATERIAL_BUCKET_ENABLED` 和 `VISUAL_HISTORY_REFS_ENABLED`。开启审核补图前，先在测试环境确认 NapCat 能按原消息回取图片，并确认 8788 走本机、内网或有 TLS 的受保护入口。通过后设置 `STICKER_CAPTURE_DEFER=true`、`STICKER_HYDRATION_ENABLED=true` 和私密的 `STICKER_HYDRATION_TOKEN`，保留 `STICKER_REVIEW_ENABLED=true`。重启两个独立单元并检查状态。

图库根据 `BOT_QQ` 选择审核账号。确认它与 NapCat 的实际登录号一致，并确认机器人和图库使用同一套表情目录。如果启用了账号独立表情目录，启动图库时用 `--dir` 指定该账号实际使用的目录。

```bash
sudo systemctl restart atri-qq-bot sticker-gallery.service
systemctl is-active atri-qq-bot sticker-gallery.service
sudo journalctl -u sticker-gallery.service -n 50 --no-pager
```

在 8788 输入补图口令，从“待下载”选择“补图”，确认记录进入“已下载”、审核列表出现实际图片。失败记录显示原因。达到三次尝试上限后，需要人工点选该条记录才能开启下一轮尝试。原始消息或图片标识已失效时，重新发送图片可提供新的取图来源。待审核记录和任务由表情目录下的 `_review/.hydration.sqlite3` 持久化，同一个账号和会话中的同一发送者如有可用的稳定图片标识，重复发图会共用一条待审核记录。

审核图过期清理后，页面显示“审核图已过期”。可以直接重新补图，也可以重发同图，让待下载记录采用新消息。通过审核或手动归类的图片标记为“已通过审核”。在任务记录保留期内，重发同图会沿用已有审核结果。

亚托莉日志目录默认位于部署目录的 `logs/`，以下 JSONL 按日轮转，保留约七天。

| 日志 | 核对内容 |
| --- | --- |
| `visual-attribution.jsonl` | 原消息 ID、原始段号、来源、证据等级、是否按上限丢弃 |
| `visual-budget.jsonl` | 材料等待、处理、聊天和发送耗时，及预算耗尽阶段 |
| `sticker-hydration.jsonl` | 登记、尝试次数、补图结果和失败原因 |

灰度时比对首次和最后一次发出的延迟，分别记录正常单图、多图部分超时和分段发送。再验证旧视频、链接图片的失败提示、两批图接同一次裸 `@`、明确引用第二张图、补图失败和重启恢复。向外分享日志时保留必要的消息定位字段，口令和生产配置单独保管。

开关回滚可在 `.env` 中按需关闭对应功能。关闭 `STICKER_CAPTURE_DEFER` 可恢复入站下载。配置或代码回滚涉及补图时，重启亚托莉和图库两个单元，保留审核数据库和历史记录，使用升级前记录的 SHA。回滚后验证普通群聊与私聊均可回复，并确认审核列表正常。

## 每天 7:30 早安

默认开启：

```env
MORNING_GREETING_ENABLED=true
MORNING_GREETING_TIME=07:30
MORNING_GREETING_TIMEZONE=Asia/Shanghai
MORNING_GREETING_CATCHUP_MINUTES=90
```

防重复逻辑：

- 每天只发一次。
- 如果服务 7:30 正好重启，90 分钟内恢复也会补发一次。
- 如果 `OWNER_QQ` 留空，会发给已经和亚托莉私聊过的人；建议正式使用时填你的 QQ。
