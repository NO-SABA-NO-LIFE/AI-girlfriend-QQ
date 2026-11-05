# Helldivers 2 情报功能集成说明（亚托莉版）

这份文档说明：绝地潜兵 2 的情报查询功能，是怎么**直接整合进亚托莉机器人**的，以及你该怎么配置、启动和使用。

**最重要的一点：整个过程不需要安装、也不需要运行 AstrBot。** 现在只有亚托莉这一个程序、登录一个 QQ 号，它既能像原来一样陪你聊天，也能在你发命令时返回绝地潜兵的情报图片、网页截图和银河快报。

---

## 一、整体是怎么工作的

- 平时你正常跟亚托莉聊天，走的还是原来的 AI 对话，**完全不受影响**。
- 只有当你发出**以 `/` 开头的命令**（比如 `/dashboard`、`/hd2news`）时，机器人才会进入情报模式，群里**不需要 @ 它**。
- 情报命令在 AI 对话之前就被拦截处理，所以不会出现"亚托莉把你的命令当成一句话去回答"的情况。

情报能力分两类呈现方式：

1. **Pillow 图片卡片**：程序直接取官方数据，用代码画成中文 PNG（总览、最高命令、地图等）。
2. **Companion 网页截图**：用内置的 Chromium 浏览器打开 Helldivers Companion 网站，先把页面上的英文片段翻译成中文，再截图发给你（`/companion`、`/hd2news`）。
3. **B站银河快报**：读取指定 UP 主的动态，把"银河快报"图文发给你，还能订阅后自动推送。

---

## 二、这次改动了哪些东西

### 新增的目录和文件

都在 `src/atri_qq_bot/helldivers/` 下面：

| 文件 | 作用 |
| --- | --- |
| `bridge.py` | 桥接器：识别命令、调用业务逻辑、把结果转成亚托莉能发的消息、管理后台任务与推送 |
| `config.py` | 从 `.env` 读取 `HD2_*` 配置，没填的项用默认值 |
| `help_text.py` | `/hd2`、`/hd2help` 返回的帮助文字 |
| `_logging.py` | 用 Python 标准日志替代原来 AstrBot 的日志 |
| `clients/` | 绝地潜兵 API、翻译、Wiki、军需簿、Companion 截图、B站监听等网络客户端 |
| `core/` | 缓存、数据模型、格式化、诊断等核心逻辑 |
| `renderers/` | 用 Pillow 把数据画成 PNG 图片的各种渲染器 |
| `assets/` | 渲染需要的图标、素材 |
| `services.py` | 把上面各部分串起来的服务层 |

### 修改的原有文件

- `src/atri_qq_bot/onebot/server_parts/server_connection.py`：初始化时创建情报桥接器，并注入"主动推送"回调（只创建，不联网）。
- `src/atri_qq_bot/onebot/server.py`：机器人启动时一并启动情报模块，关闭时一并停止；启动失败自动降级，不影响聊天。
- `src/atri_qq_bot/onebot/server_parts/server_events.py`：消息处理最前面加"情报命令守卫"，命中就处理并返回，并把当前会话标识传给桥接器。
- `pyproject.toml`、`requirements.txt`、`uv.lock`：包含 `aiohttp`、`playwright` 依赖。
- `.env.example`：末尾追加完整 Helldivers 配置段（含网页截图、B站）。

---

## 三、部署步骤（一步步来）

### 第 1 步：安装依赖

**方式 A：用 uv（推荐，项目自带 uv.lock）**

```bash
uv sync --extra dev
```

**方式 B：用标准虚拟环境 + pip**

```bash
python -m venv .venv
# Linux / 云服务器：
.venv/bin/python -m pip install -e ".[dev]"
# Windows PowerShell：
# .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 第 2 步：安装 Chromium 浏览器内核（网页截图功能需要）

Pillow 图片卡片不需要浏览器，但 `/companion`、`/hd2news`、`/hd2refresh` 这几个网页截图命令需要 Chromium。在项目目录执行：

```bash
.venv/bin/playwright install chromium
```

如果运行截图时报缺系统库（Linux 常见，例如 libnss3、libatk 之类），用有 root 权限的账号补装系统依赖：

```bash
# Debian / Ubuntu
.venv/bin/playwright install-deps chromium
```

> 小提示：默认会把浏览器装到用户目录 `~/.cache/ms-playwright/`。如果你的环境变量 `PLAYWRIGHT_BROWSERS_PATH` 指向了一个没有写权限的目录，安装会报 `EACCES` 错误，这时可以显式指定一个可写路径，例如：
> `PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" .venv/bin/playwright install chromium`
> 运行机器人时也要带上同样的环境变量。

### 第 3 步：准备配置文件

如果还没有 `.env`，先复制一份：

```bash
cp .env.example .env
```

用文本编辑器打开 `.env`，先按亚托莉原来的要求填好 `BOT_QQ`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`（保证聊天能用）。

再找到文件末尾的 Helldivers 段，常用项如下：

```dotenv
# 情报模块总开关；false 时完全不加载，机器人只剩聊天
HD2_ENABLED=true
# 建议填联系邮箱，遵守官方 API 礼仪，不填也能用
HD2_API_CONTACT=
# 直连海外服务慢或超时，可填代理，例如 http://127.0.0.1:7890；不用留空
HD2_PROXY_URL=
# 是否把英文情报翻译成中文
HD2_TRANSLATION=true

# -- 网页截图 --
HD2_COMPANION=true                 # 截图功能总开关
HD2_COMPANION_WARMUP=true          # 后台预热，让截图尽量秒回
HD2_NEWS_PUSH=false                # 新闻更新主动推送开关（订阅还要在群里发命令）
HD2_COMPANION_SHOW_INACTIVE=false  # 首页是否显示低活跃度战役

# -- B站银河快报 --
HD2_BILIBILI_ENABLED=false         # 后台自动监听开关（手动 /银河快报 不需要开）
HD2_BILIBILI_COOKIE=               # 后台监听必填，见下方第 5 步
HD2_BILIBILI_UID=3546777720457465  # 一般不用改
HD2_BILIBILI_KEYWORD=银河快报
HD2_BILIBILI_INTERVAL=300
```

其余配置项都有内置默认值，一般不用动。

### 第 4 步：Linux 服务器装中文字体（防止图片乱码）

Linux（Debian / Ubuntu）确认装了中文字体，否则图片中文可能变成方框：

```bash
apt update && apt install -y fonts-noto-cjk
```

Windows 和 macOS 一般自带中文字体，可跳过。

### 第 5 步：获取并填写 B站 Cookie（用银河快报时需要）

由于 B站接口的签名机制，**即便是手动发 `/银河快报`，也需要登录 Cookie**。获取方法：

1. 在电脑浏览器里登录 B站（bilibili.com）。
2. 按 `F12` 打开开发者工具，切到 **Network（网络）** 标签。
3. 刷新一下页面，在请求列表里点任意一个发往 `bilibili.com` 的请求。
4. 在右侧 **Request Headers（请求标头）** 里找到 `Cookie:` 那一行，把**整段内容**复制下来。
5. 粘贴到 `.env` 的 `HD2_BILIBILI_COOKIE=` 后面（一整行，不要换行）。

复制的 Cookie 必须包含 `SESSDATA`，最好同时保留 `DedeUserID`、`buvid3`、`buvid4` 等字段，否则会提示"Cookie 未通过登录验证"。

### 第 6 步：启动机器人

按亚托莉原本的方式启动（确保 NapCat 的反向 WebSocket 已配好）：

```bash
# Linux / 云服务器
.venv/bin/python -m atri_qq_bot
# Windows PowerShell：
# .\.venv\Scripts\python.exe -m atri_qq_bot
```

启动后稍等，情报模块会在后台预热、拉取数据。在 QQ 里发 `/hd2` 试试，能收到帮助文字就说明成功了。

---

## 四、命令清单

命令**不区分大小写**，群里私聊都能用，群里无需 @。

### 基础情报（Pillow 卡片 + 文本）

| 命令（含别名） | 功能 | 返回形式 |
| --- | --- | --- |
| `/hd2`、`/helldivers` | 简短帮助 | 文本 |
| `/hd2help`、`/hdhelp` | 完整帮助 | 文本 |
| `/dashboard`、`/总览`、`/战况总览` | 银河战争总览（实时） | 图片 |
| `/hd2stats` | 在线人数、击杀、命中率等详细统计 | 图片 |
| `/order`、`/major_order`、`/最高命令` | 当前最高命令任务卡 | 图片 |
| `/map`、`/地图` | 银河战争地图 | 文字 + 图片 |
| `/global_events`、`/events`、`/全球事件` | 全球事件 | 图片 |
| `/warfront <阵营>`、`/战线 <阵营>` | 某阵营战线战况 | 图片 |
| `/planet <名称/编号/slug/链接>`、`/星球 ...` | 单个星球详情；playwright 下为网页截图+中文文本，失败回退 Pillow | 文字 + 图片 |
| `/dss`、`/空间站`、`/民主空间站` | 民主空间站；playwright 下为网页截图+中文文本，失败回退 Pillow 卡片 | 文字 + 图片 |
| `/steam` | Steam 更新公告 | 图片（失败回退文本） |
| `/wiki <关键词>`、`/维基`、`/百科` | Wiki 查询；加 `-f` 看全文 | 文本 |
| `/hd2data <名称>`、`/军需簿`、`/装备`、`/武器库` | 武器/护甲/敌人资料 | 图片 |
| `/hd2ping`、`/诊断`、`/自检` | 数据源健康与缓存自检 | 文本 |

### 网页截图、银河快报与订阅（第二阶段）

| 命令（含别名） | 功能 | 返回形式 |
| --- | --- | --- |
| `/companion`、`/首页`、`/companion_home` | Companion 首页网页截图；失败时尽量返回缓存 | 图片 |
| `/hd2news` | 最新新闻网页截图；失败自动回退 Pillow 新闻卡 | 图片（或文字+图片） |
| `/hd2refresh`、`/刷新缓存`、`/hd2clear` | 强制刷新全部网页截图并汇总成功/失败 | 文本 |
| `/银河快报`、`/galaxy_news` | 立即获取最新一期 B站银河快报 | 文字 + 图片 |
| `/hd2news订阅`、`/news_subscribe` | 在当前群订阅 Companion 新闻自动推送 | 文本 |
| `/hd2news退订`、`/news_unsubscribe` | 取消当前群的新闻推送 | 文本 |
| `/银河快报订阅`、`/galaxy_news_subscribe` | 在当前群订阅银河快报自动推送 | 文本 |
| `/银河快报退订`、`/galaxy_news_unsubscribe` | 取消当前群的银河快报订阅 | 文本 |

带参数的例子：

```text
/warfront Automaton        看机器人阵营战线
/planet 科埃佩萨            看某个星球
/hd2data 磁轨炮             看某件武器
/wiki 泰坦 -f               查 Wiki 并看全文
```

阵营名称：`Terminids`（终结族）、`Automaton`（机器人）、`Illuminate`（光能者）。

---

## 五、订阅与主动推送是怎么工作的

- 每个会话都有一个标识：群聊是 `group:<群号>`，私聊是 `private:<QQ号>`。你在某个群发订阅命令，机器人就把这个群记下来。
- **Companion 新闻推送**：后台预热循环会定期截图新闻页，用"内容指纹"判断新闻有没有变化。一旦变化，就把新截图发给所有订阅群。需要在 `.env` 里把 `HD2_NEWS_PUSH=true`、`HD2_COMPANION_WARMUP=true`，再在群里发 `/hd2news订阅`。
- **银河快报推送**：需要 `HD2_BILIBILI_ENABLED=true` 且填了含 `SESSDATA` 的 Cookie，机器人在后台轮询 UP 主动态，发现含"银河快报"的新内容就推给订阅群。
- 真正的发送动作复用亚托莉现有的 OneBot 连接（`send_group_msg` / `send_private_msg`），不需要额外开任何服务。

---

## 六、常见问题

**1. 发命令后等很久，或者提示超时怎么办？**

海外服务个别接口本身偏慢（几秒很正常）。如果经常超时，在 `.env` 给 `HD2_PROXY_URL` 填可用的 HTTP 代理；也可以发 `/hd2ping` 看各数据源通不通、缓存新不新鲜。

**2. 日志里出现 `translation ... 429` 是怎么回事？**

免费翻译接口被限流了。程序会自动降级——用已有翻译缓存，或暂时保留英文，**不影响图片生成**，过一会儿自动恢复。

**3. 图片里的中文变成方框 / 乱码？**

系统缺中文字体。Linux 执行 `apt install -y fonts-noto-cjk`，然后重启机器人。

**4. 网页截图提示浏览器相关错误？**

先确认装了 Chromium（第 2 步）。如果是 `Page.goto ... Timeout`，多半是当前网络到 Companion 站点太慢，重试或配代理即可；程序在截图失败时会自动回退到 Pillow 新闻卡，不会让你空手而归。

**5. 银河快报提示"Cookie 未通过登录验证"？**

Cookie 没复制完整或已过期。按第 5 步重新从浏览器复制整段 Cookie，确保含 `SESSDATA`，粘贴后重启。

**6. 我想暂时关掉情报功能，只保留聊天？**

把 `.env` 里 `HD2_ENABLED=false`，重启即可。情报模块不会加载，也不发起任何网络请求。

**7. 情报模块启动失败，会不会导致整个机器人用不了？**

不会。启动逻辑做了降级保护，情报模块即使报错（例如 Chromium 没装），亚托莉聊天也能正常运行，日志里会留下具体原因。

---

## 七、当前阶段范围

- **第一阶段（已完成）**：纯 Pillow 图片卡片和文本查询。
- **第二阶段（已完成）**：Companion 网页截图（`/companion`、`/hd2news`、`/hd2refresh`）、B站银河快报、新闻与快报的订阅/退订和主动推送。

至此，源插件 `astrbot_plugin_Helldivers` 的主要功能都已在亚托莉框架内落地，全程不运行 AstrBot。

---

## 八、验证情况

移植完成后做过以下验证：

- **编译与导入**：全部代码编译通过，完整导入无循环依赖；53 个命令、22 个处理方法一一对应、均可解析。
- **截图链路（本地确定性验证，通过）**：用本地页面验证了就绪检测、翻译回调被正确调用并应用译文、按裁剪尺寸输出 PNG（1280×900）、TTL 缓存命中、force 强制重截、内容指纹文本记录。
- **回退机制（真实生效）**：网页截图失败时，`/hd2news` 成功回退生成 Pillow 新闻卡（960×520 有效 PNG）。
- **dss/planet 网页截图分支（通过）**：用本地页面验证了 `/dss`、`/planet` 在 playwright 后端下返回「中文文本 + 真实 PNG 截图」，并覆盖 local 后端走 Pillow、截图失败回退、星球 slug/链接解析等全部分支。
- **订阅逻辑（通过）**：订阅、重复订阅、退订均返回正确提示。
- **回归测试**：全量测试 1031 通过、3 跳过；12 个失败全部集中在 `test_toolbox.py`，原因是环境缺少 `docx`、`openpyxl`、`pandas`、`ollama` 等可选依赖，且未改动任何 toolbox 文件，与本次移植无关。

> 说明：验证期间，当前工作云环境到 `helldiverscompanion.com` 的网络链路持续劣化（首字节时间在 5–18 秒间抖动、偶发完全超时、SSL 连接中断），因此未能在该环境抓到一张完整的真实网页截图。但截图代码的完整逻辑已通过本地等价验证，且失败回退真实有效；在网络正常的环境（或配置 `HD2_PROXY_URL` 代理）下即可正常使用网页截图。

为了超级地球！
