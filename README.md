# 战争雷霆 · 聊天实时翻译 + 手机代发

把战局里的英文聊天**实时翻成中文**贴在屏幕左下角，并且能把你的**中文翻成英文**发进游戏。

不注入 DLL、不读游戏内存、不改游戏文件——只读游戏自带的本地接口 `127.0.0.1:8111`。

```
┌──────────────────┐   只读 HTTP     ┌──────────────┐   云端    ┌────────────┐
│ War Thunder      │ ──────────────▶ │  本程序       │ ────────▶ │ DeepSeek   │
│ localhost:8111   │                 │  （单进程）   │ ◀──────── │ 兼容端点   │
└──────────────────┘                 └──────┬───────┘           └────────────┘
                                            │ 置顶双层窗口
                                            ▼
                                     ┌──────────────┐
                                     │ 手机浏览器    │  ← 局域网
                                     └──────────────┘
```

---

## 一、功能一览

| 能力 | 入口 | 说明 |
|---|---|---|
| 实时字幕 | 自动 | 战局内 All/Team 频道即时翻成中文，左下角滚动显示 |
| 中文发话（电脑）| `Ctrl+F9` | 弹出输入框，中文 → 英文 → 复制到剪贴板 |
| 中文发话（手机）| 浏览器访问 `http://<电脑IP>:8765/` | 手机输中文，电脑译成英文并复制 |
| 显隐 | `F12` | 字幕面板显示/隐藏 |
| 退出 | `Ctrl+F12` | 结束程序 |

> 为什么发话要「复制到剪贴板」而不是自动打字：见 [技术记录.md](技术记录.md) 第四节，
> 实测本游戏会丢弃一切合成按键，这条路上限就到这。

---

## 二、部署

### 1. 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 |
| Python | 3.10 以上（本机验证 3.14.0）|
| 依赖 | `requests`、`PyYAML`（tkinter 是标准库自带）|
| 游戏设置 | **无边框窗口 / 窗口化模式**（独占全屏下任何外部窗口都盖不上）|

### 2. 安装依赖

```powershell
pip install requests pyyaml
```

### 3. 配置密钥

需要两条通道、两个环境变量（**不要写进 config.yaml，会跟着文件一起泄露**）：

| 变量 | 用途 | 说明 |
|---|---|---|
| `WT_LLM_KEY` | 英 → 中（字幕）| 任意 OpenAI 兼容端点，默认 DeepSeek 官方 |
| `WT_ZH2EN_KEY` | 中 → 英（发话）| 见 `config.yaml` 的 `llm.zh2en`，默认千问AI平台 |

写成用户级环境变量，一次配好永久生效：

```powershell
[Environment]::SetEnvironmentVariable('WT_LLM_KEY',   'sk-你的key', 'User')
[Environment]::SetEnvironmentVariable('WT_ZH2EN_KEY', 'sk-另一个key', 'User')
```

程序启动时会先读环境变量，读不到再去注册表 `HKCU\Environment` 补读，
所以**开机自启也能拿到**（Explorer 继承的环境块可能是旧的）。

#### 本地覆盖：`config.local.yaml`

想改参数但又不想让改动出现在版本库里，就在 `config.yaml` 旁边建一个
`config.local.yaml`（已在 `.gitignore` 里），内容会被**深度合并**到主配置之上：

```yaml
web:
  auth: false        # 本机不要 PIN，其他键不受影响
```

这样 `config.yaml` 保持仓库里的默认值，`git status` 永远是干净的。

### 4. 首次验证

```powershell
cd wt-chat-translate
python main.py --once
```

能列出当前战局的聊天就说明接口通了。不在对局里会返回 0 条，属正常。

### 5. 常驻运行

```powershell
# 有黑窗，方便看日志
python main.py

# 无黑窗（推荐）
start.cmd
```

日志写到 `%TEMP%\wt_service.log`，实时查看：

```powershell
Get-Content "$env:TEMP\wt_service.log" -Wait -Encoding UTF8
```

### 6. 开机自启

把 `start.cmd` 的快捷方式丢进启动目录即可：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
```

本机已注册好一个：`wt-chat-translate.lnk` → `pythonw.exe main.py`。
删掉这个 lnk 就取消自启。

### 7. 手机端放行

Windows 防火墙默认拦入站，需要一条规则（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "wt-chat-translate-8765" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 `
  -RemoteAddress LocalSubnet -Profile Any
```

`-RemoteAddress LocalSubnet` 是关键：只允许**本网段**访问，不对公网敞开。
本机已加过这条规则。

---

## 三、使用

### 日常：看字幕

游戏跑起来就行，字幕自动出现。敌方言行标红、队友蓝色、公共频道绿色前缀。

- 3 条以内消息时面板自动缩短，多了向上生长，最多 15 行
- 8 秒无新消息自动淡出（`ui.idle_hide`）
- 面板**鼠标可穿透**，不会挡住视角操作

### 发话 · 电脑端

1. 游戏中按 `Ctrl+F9`
2. 面板上方弹出输入框，输入中文，回车
3. 屏幕提示「已复制：xxx」，游戏自动切回前台
4. 游戏里按 `Enter`（开聊天）→ `Ctrl+V`（粘贴）→ `Enter`（发送）

### 发话 · 手机端

1. 手机连**同一个 Wi-Fi**
2. 浏览器打开 `http://<电脑IP>:8765/`（启动日志里有）
3. 首次填 PIN（启动日志里的 `[手机] PIN = xxxx`，每次重启会变；想固定就填 `web.token`）
4. 输入中文点发送 → 手机显示英文译文 → 回游戏按三下键发出

常用语有快捷胶囊（掩护我 / 小心后面 / 需要支援…），点一下直接填进输入框。

---

## 四、配置说明

全部在 `config.yaml`。改完重启程序生效。

### llm —— 英译中（字幕）

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` / `model` | DeepSeek / `deepseek-chat` | 任意 OpenAI 兼容端点 |
| `api_key_env` | `WT_LLM_KEY` | 优先于 `api_key` 字段 |
| `temperature` | 0.1 | 翻译要稳定，别调高 |
| `retries` / `timeout` | 2 / 8s | 429 和 5xx 会重试 |
| `fallback` | 关闭 | 主通道挂了的备用端点 |

### llm.zh2en —— 中译英（发话）

| 键 | 默认 | 说明 |
|---|---|---|
| `model` | `deepseek-v4.1-flash-1017` | FLASH 档，便宜 |
| `enable_thinking` | `false` | **关掉思维链**，实测 0.65s vs 3.10s |
| `reasoning_effort` | 空 | 想要「低强度而非关闭」就填 `"low"` |

### ui —— 字幕面板

| 键 | 默认 | 说明 |
|---|---|---|
| `anchor` | `left` | 贴屏幕左侧 |
| `width` | 432 | 逻辑像素，程序内部 ×1.5 换算物理像素 |
| `margin_x` / `margin_y` | 9 / 169 | 左边距 / 距底部 |
| `max_height` | 249 | 面板固定高度（不再随消息数伸缩）|
| `font_px` | 17 | **物理像素**，不是磅值 |
| `line_gap` | 0 | 行距，行距主要由字号承担 |
| `alpha` | 0.41 | **仅背景黑带**的不透明度，文字恒 100% |
| `max_lines` | 16 | 上限，实际显示条数由高度裁剪 |
| `click_through` | true | 鼠标穿透，别关 |

### web —— 手机端

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 关掉则完全不启网页服务 |
| `host` / `port` | `0.0.0.0` / 8765 | |
| `token` | `""` | 留空 = 每次启动随机 4 位；固定值**必须加引号**，否则 YAML 当八进制解析 |
| `auth` | `true` | `false` = 完全不验 PIN，同网段任何设备都能发 |
| `mode` | `clipboard` | 见下方 |

### 中译英的对局上下文

发话时会自动附带一段当前对局上下文，让译文贴合实际战况：

- **战术态势**：本机高度/速度/马赫/升降率（`/state`）+ 进行中的任务目标（`/mission.json`）
- **本局聊天**：当前对局**全部**消息（含中文战术播报），新对局开始时自动清空；
  想限制条数改 `context.max_msgs`（0 = 整局全部）

上下文只用于理解语境（代词指代、点位、敌我关系），不会被译出来。
实测：「小心你后面那两架」在有队友报尾上下文时译为 `watch your six, two behind you`，
无上下文时只有泛泛的 `watch the two behind you`。额外延迟约 0.1–0.3s。

### keepalive —— 对局保活

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 在对局中时定期预热中译英通道，避免临时发言撞冷启动（实测 3.7–8s）|
| `interval` | 90 | 对局内每隔多少秒 ping 一次，每次约 10 token |
| `check` | 5 | 检测对局状态的间隔（本地 /state，零成本）|

对局判定用 `/state` 的 `valid` 字段（机库为 false）。进入对局立即预热一次；
离开对局完全静默，不产生任何云端请求。

进入新对局时还会**只显示当前对局**：游标对齐到最新（跳过旧对局积压）并清空面板。
机库里启动程序不会回放上一局的残留消息。注意：对局检测有约 2 秒粒度，
出生前几秒的聊天可能被跳过。

### compose.mode / web.mode

- `clipboard`（默认）：译好放剪贴板 + 自动切回游戏，你按三下键
- `inject`：尝试自动打字。**本机实测无效**，游戏会丢弃合成按键

---

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `--once` 连接被拒 | 没进对局 / 8111 未监听 | 进战局再试 |
| 面板一直不出现 | 游戏在独占全屏 | 改成无边框窗口模式 |
| 面板位置不对 | 多显示器 / 虚拟显示器 | 程序按游戏窗口所在屏定位；仍异常改 `margin_x/y` |
| 字幕全是原文 | 没密钥 / 云端挂了 | 看日志里的 `[统计]` 和警告行 |
| 手机打不开页面 | 防火墙 / 不在同一网段 | 检查第 7 步的规则；`ping` 一下电脑 IP |
| 手机显示 PIN 不正确 | 服务重启换了随机 PIN | 看启动日志里的 `[手机] PIN = `；想固定就填 `web.token` |
| 服务在跑但没反应 | 游标卡住 | 已修复：15 秒无消息自动重对齐 |
| 译文带解释 | 模型不听话 | 换模型或调低 `temperature` |
| 某些短语翻错 | 模型对缩写理解偏差 | 在 SYSTEM_PROMPT 的缩写示例里补充 |

---

## 六、已知限制

1. **好友私聊 / 机库大厅频道收不到**——走 XMPP，本地接口不提供。
2. **不能自动打字**——游戏过滤合成按键，实测判定见技术记录。
3. **发话固定三下按键**——Enter / Ctrl+V / Enter，绕不过去。
4. **聊天内容会发到云端**（DeepSeek）。介意就把 `privacy.allow_cloud` 设 `false`，那时原文直显。
5. **游戏聊天框会随消息增多向上长**，所以面板底边和它可能偶尔重叠；改 `ui.margin_y` 调整。
6. 手机端 4 位 PIN 挡不住同网段的暴力尝试，长期用建议换长字符串。