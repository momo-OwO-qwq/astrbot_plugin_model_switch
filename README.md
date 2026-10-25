# astrbot_plugin_model_switch

[GitHub 仓库](https://github.com/momo-OwO-qwq/momo-OwO-qwq) · AstrBot 插件：按时间段 / 会话场景自动切换模型，省 token。

按时间段 / 会话场景自动切换模型。只补 AstrBot 缺的三件事：

1. **时间段规则**：到点切、时段保持、跨夜。
2. **场景分流**：群聊用便宜模型、私聊用强模型，不用逐个群配 umo。
3. **空闲再切**：会话正热的时候不切，避免打断 prompt 缓存。

手动切模型继续用 AstrBot 自带的 `/provider`，故障回退继续用 `fallback_provider_ids`，本插件都不重复实现。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/momo-OwO-qwq/momo-OwO-qwq.git astrbot_plugin_model_switch
```

重启 AstrBot，或在 WebUI 插件页重载。也可以把目录打包成 zip 后从插件页上传。

## 配置

在 WebUI 插件配置里：

- `switch_policy`：`idle`（默认）或 `immediate`
- `baseline_provider`：所有计划不命中时回到的模型；留空自动记录首次启动时的默认模型
- `plans`：添加计划

计划字段：

| 字段 | 说明 |
|---|---|
| scope | `global` / `group` / `private` / `session` |
| session | 仅 `session` 作用域填写 umo |
| provider | 目标模型 |
| start | `HH:MM`，留空=常驻 |
| end | `HH:MM`，留空=从 start 起一直生效 |
| weekdays | 留空=每天，可写 `1-5` / `mon-fri` |
| priority | 同作用域内数字大者优先 |

示例：

- `scope=group`、`provider=便宜模型`、无时间 → 群聊常驻便宜模型
- `scope=global`、`provider=强模型`、`start=09:00`、`end=18:00`、`weekdays=1-5` → 工作日白天用强模型
- `scope=global`、`provider=便宜模型`、`start=23:00`、`end=07:00` → 夜间便宜模型

## 命令

| 命令 | 说明 |
|---|---|
| `/model` | 当前模型、命中计划、待切换、下次边界 |
| `/model plan` | 查看所有计划 |
| `/model pause [分钟]` | 暂停调度，0 表示直到 `/model resume` |
| `/model resume` | 恢复调度 |
| `/model apply` | 忽略空闲等待，立即应用 |

`pause/resume/apply` 需要管理员权限。

## 生效方式

- 全局规则：修改 `agent_runner.config.model.provider_id`（WebUI 里的默认模型），下一轮请求生效。
- 群聊/私聊/指定会话规则：写入会话级 provider 偏好，只影响该会话。
- 计划结束时：只清除插件自己写过的会话偏好，用户用 `/provider` 手动改过的不动。

## 注意

- 全局切换只对 `runner_type=local` 生效。
- 多配置文件（abconf）下只改默认配置。
- 切换会改变计费模型，但不会重置对话历史。
