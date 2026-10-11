# 当前运维版用法

这是 `Gwihwan-Go/ClaudeTeam` 的本地运维分支，基于上游
`zylMozart/ClaudeTeam` 的 `1ea5e45`，保存截至 2026 年 9 月的部署修补。
发布来源是实际安装使用的代码及 `team-setup` 会话对应的恢复脚本。
上游后续更新尚未合并；原始部署流程见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 运行结构

- 一个团队对应一个工作目录、`claudeteam.toml`、tmux session 和飞书群。
- `state/` 保存共享 inbox、status、task、router cursor、守护进程 PID。
- `state/agents/<agent>/` 保存身份及持久记忆。
- `state/agent-home/<agent>/` 保存各 agent 的 CLI 会话和配置。
- router 接收飞书消息并投递到 agent；watchdog 检查并重启 router。

部署为 Docker 且 `/data/agent-home` 可写时，agent HOME 使用该挂载点。
本文恢复示例来自 Linux 主机部署，未验证 Docker 恢复流程。

## 安装与团队环境

```bash
git clone https://github.com/Gwihwan-Go/ClaudeTeam.git
cd ClaudeTeam
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

凭据放在本机的 `~/.claudeteam-teams/common.env` 和各团队 `.env` 中。
示例模板位于 [examples/team_ops](../examples/team_ops/)。这些文件由 shell
`source`，仅加载自己维护的文件；不要提交真实 token、App Secret 或 CLI auth.json。

```bash
source ~/.claudeteam-teams/common.env
source ~/.claudeteam-teams/symcli.env
export CLAUDETEAM_STATE_DIR="$CLAUDETEAM_WORKDIR/state"
export CLAUDETEAM_CONFIG_FILE="$CLAUDETEAM_WORKDIR/claudeteam.toml"
cd "$CLAUDETEAM_WORKDIR"
```

**工作目录、STATE_DIR、CONFIG_FILE 必须同时对应同一团队。** 显式设置
CONFIG_FILE 很重要：agent 执行任务时可能切换到另一个项目目录。
每个团队还应配置自己的 `CLAUDETEAM_SESSION`、`LARK_CLI_PROFILE`、
`FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 TOML 中的 `chat_id`。

首次部署先按 DEPLOYMENT.md 建飞书应用、登记 lark-cli profile、配置 TOML，
再用 `claudeteam start`、`claudeteam up` 和 `claudeteam health` 检查。

## 日常命令

```bash
claudeteam team
claudeteam health
claudeteam peek manager 80
claudeteam send manager worker_cc '检查测试结果'
claudeteam say manager '任务完成'
claudeteam up
```

`up` 负责 router/watchdog；已有 agent 会话不会因此恢复历史。
新增 agent 仍使用 `claudeteam hire`，移除使用 `claudeteam fire`。

飞书群内：

| 命令 | 作用 |
| --- | --- |
| `/team` | 查看员工状态 |
| `/health` | 查看资源与运行信息 |
| `/t manager 80` | `/tmux manager 80` 的简写 |
| `/s manager 消息` | `/send manager 消息` 的简写 |
| `/s m 消息` | agent 名支持唯一前缀；完全匹配优先，歧义时报出候选 |

## 恢复团队及会话

原部署的脚本在 `~/.claudeteam-teams/`；仓库保存可调整的部署示例：

```bash
bash examples/team_ops/revive.sh symcli --resume
bash examples/team_ops/revive.sh symcli --resume manager data
bash examples/team_ops/revive.sh symcli --resume --add paper
bash examples/team_ops/revive.sh symcli --resume --max-lines 8000
bash examples/team_ops/check.sh symcli
```

完整恢复会重建指定团队的 tmux session，终止其中正在运行的进程。
`--add` 保留其他窗口，但会重建指定 agent 已存在的窗口。
未带 `--resume` 时启动新会话。只有确定需要恢复的团队才运行这些命令。

示例保留原部署的团队名称和默认名单：`symcli`、`ClaudeTeam2`、`startup`、
`skilltest`。请先修改 `revive.sh` 的 `case` 表，或在命令行明确指定 agent。
名单应与该团队 TOML 一致；这不是自动发现当前活跃员工的功能。
`ClaudeTeam2` 对应 `team2.env`，其他团队对应 `<team>.env`。

可通过环境变量调整部署路径：

- `CLAUDETEAM_TEAMS_DIR`：环境文件目录，默认 `~/.claudeteam-teams`。
- `CLAUDETEAM_TMUX_SOCKET`：tmux socket，默认当前用户的默认 socket。
- `CLAUDETEAM_PREFLIGHT`：可选本机预检查脚本，例如检查出网代理。

恢复脚本源于独立运维工具，使用 ClaudeTeam 内部接口，尚未整合成正式 CLI
子命令。`claudeteam resume` 不是此版本的可用命令。

恢复细节：

1. 按各 agent 自己的 HOME 扫描会话，按文件修改时间选最近记录，跳过不足
   10 行的记录。这是避开启动即崩溃记录的启发式判断。
2. Claude 经正常 provision 流程附加 `--resume <session-id>`。
3. Codex 使用 `codex resume <session-id> <prompt>`。新身份提示先落盘，
   tmux 只传短命令，由 pane 内 shell 读取文件，避免超长粘贴及 send-keys 限制。
4. 每次重新生成身份信息，绑定当前团队。`--max-lines N` 超限时改为新会话。
5. 清理明显失效的 router/watchdog PID 文件，再运行 `claudeteam up`。

`check.sh` 只扫描 pane 尾部常见错误并提示仅剩 shell 的窗口；它不能证明
agent 正在工作，也不能替代在飞书发消息验证完整消息链路。

### watchdog 在运行，但 manager 收不到飞书消息

先查看 `state/router.log` 和 `state/watchdog.log`。watchdog 进程存活只说明
守护循环在运行；若持续出现 `router respawned`，同时 router 每次补读消息
都报相同异常，说明重启没有解决消息处理错误。

2026-10-08 修复了一个实际故障：纯文本 `1` 被解析成 JSON 整数，随后调用
`.get()` 导致 router 崩溃；重启补读同一消息时再次崩溃。现在数字、布尔值、
`null` 和数组等非对象 JSON 文本均保留原文投递，实时订阅和历史补读共用
此修复。不要删除 cursor 或 inbox 来跳过消息；部署修复后由 watchdog 重启，
确认 router 持续存活、cursor 前进，并检查 manager inbox 的投递及读取状态。

若本机的 Feishu WebSocket 长期收不到实时事件，可在团队 TOML 的
`[router]` 中设置 `poll_interval_s = 20`。router 将按间隔读取飞书历史，
自动翻页并按消息 ID 去重；单次请求失败会在下轮重试，写 manager inbox
失败时也不会把消息标记为已处理。轮询依赖飞书历史读取权限，且消息
延迟通常不超过一个轮询周期加 API 调用时间。切换模式后只重启 router，
不要重建 agent 会话。

watchdog 本身也需要宿主机托管。Linux 用户 systemd 可用时，复制
`examples/team_ops/run-watchdog.sh` 到 `~/.claudeteam-teams/`，复制
`examples/team_ops/claudeteam-watchdog@.service` 到
`~/.config/systemd/user/`，然后运行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now claudeteam-watchdog@symcli.service
systemctl --user status claudeteam-watchdog@symcli.service
```

登录用户还需启用 linger，才能在注销后继续运行服务。systemd 会在
watchdog 异常退出或主机重启后启动它；watchdog 再恢复 router。

## 认证与网络改动

**Claude（当前 Linux 部署）**：通过 `CLAUDE_CODE_OAUTH_TOKEN` 传入部署
token，停止从宿主机交互登录文件复制和周期性分发 `.credentials.json`。
macOS 的 Keychain 分支仍保留，未在本次发布中做实机验证。

**Codex**：启动时清空 `OPENAI_API_KEY` / `OPENAI_ADMIN_KEY`，使用各
agent 的 `.codex/auth.json`。watchdog 按 `last_refresh` 同步宿主机和
当前团队的已有 auth.json，并拒绝超过当前时间一小时的候选时间戳。
这沿用了原部署中所有候选使用同一账号的前提；它不按账号分组，也不能保证
消除并发 refresh 竞争。不要用它混合同步多个账号。

**飞书**：tenant token 缓存按 App ID 分开，避免不同 bot 互用缓存。
代理由 TOML 的 `[feishu] no_proxy` 控制：`false` 保留代理环境变量，
`true` 去除 HTTP/HTTPS 代理。旧的 `LARK_CLI_NO_PROXY` 会从子进程环境移除。
若本机存在 mitmproxy CA，沿用合并系统 CA 的支持。
旧版 `claudeteam health` 的代理提示仍按旧环境变量判断，以这里和 TOML 为准。

**飞书应用创建器**：保留 Linux 按键、无头登录二维码、代理参数和引导浮层
处理补丁。`CREATOR_HEADLESS=1` 开启无头模式，`CREATOR_PROXY` 指定代理；
二维码截图沿用 `/tmp/skilltest-qr.png`。创建器仍可能随飞书 UI 更新失效，
此发布未执行真实应用创建。当前快照沿用 `ignoreHTTPSErrors: true`，仅适用于
受信任的部署网络，后续通用化时应调整为显式配置。

## 与原部署的发布差异

- 核心行为保留实际部署补丁；修正了过期的 Claude 认证注释。
- 将飞书缓存路径设为含 App ID 的模板，便于测试隔离，实际路径不变。
- 恢复示例去除固定用户名、UID 和 mihomo 启动路径，提供环境变量入口；
  失败时返回非零状态，Python 和 shell 使用一致的 tmux socket。
- 测试补齐本地行为，并修正上游旧测试依赖宿主机 CLI、真实 `/proc` 和
  外部团队配置的问题。

原安装目录、实际团队环境、凭据、聊天日志和运行状态不属于仓库内容。

验证：在独立 HOME、state 和 tmux 目录下运行 `python3 tests/run.py`，
1041 项通过、0 项失败；恢复 shell 脚本通过 `bash -n`，应用创建器通过
`node --check`。2026-10-11 在 Linux 宿主机为 symcli 启用了历史轮询与
systemd watchdog，确认轮询成功、watchdog 被 SIGKILL 后自动重启，
router 与 agent pane 保持运行；未执行飞书应用创建。
