# AstrBot 全球厂商状态监控

面向 `aiocqhttp` / OneBot v11 的主动状态告警插件。插件轮询官方状态接口，在服务出现异常、异常信息更新或恢复时生成精美的 PNG 状态卡并推送到指定 QQ 群。卡片图标全部来自插件内置 SVG 资产，运行时在内存中栅格化，不需要 Cairo 等原生依赖。

## 内置来源

- OpenAI、Claude / Anthropic、Groq、Cohere
- Google Vertex AI / Gemini
- Amazon Web Services、Microsoft Azure
- GitHub、Cloudflare

OpenAI、Claude、Groq、Cohere、GitHub 和 Cloudflare 使用 Statuspage JSON；Google 使用 Google Cloud 事件 JSON；AWS 和 Azure 使用官方 RSS。还可以在插件配置中添加其他兼容 Statuspage JSON 的状态页。

## 配置

在 AstrBot WebUI 的插件配置中填写：

1. `group_whitelist`：允许接收告警的 QQ 群号。默认值为空，因此刚安装时不会主动发送。
2. `platform_id`：通常保持为空。只有一个 aiocqhttp 实例时插件会自动选择；存在多个实例时必须明确填写。
3. `poll_interval_seconds`：默认 300 秒，最低 60 秒。
4. `sources`：可单独停用任意内置来源。
5. `notify_maintenance`：默认关闭，计划维护不会被当作服务故障。
6. `notify_existing_on_first_startup`：控制首次成功查询时是否推送厂商已有异常，默认开启。关闭时只建立状态基线，之后的更新和恢复仍会通知。
7. `display_language`：支持 `中英双语`、`简体中文` 和 `English`，默认中英双语。
8. `enable_ai_translation`：默认开启，调用 AstrBot 默认对话模型把官方英文事件翻译为简体中文。
9. `translation_provider_id`：通常留空；留空时使用默认对话模型，也可以为翻译单独选择模型。

插件使用 AstrBot 的全局 `http_proxy` 环境配置访问状态页，无需重复配置代理。

## 翻译与双语显示

- 双语模式把中文译文作为主内容，并在下方以较小字号保留官方英文原文；纯中文和纯英文模式可在配置中切换。
- 标题、官方更新正文和受影响服务会按轮次批量翻译，译文保存到插件独立 KV 缓存，同一内容不会反复消耗模型调用。
- 翻译只影响图片展示，不参与事件指纹和告警去重。译文变化不会制造额外的“状态更新”。
- 默认对话模型未配置、调用超时或输出格式异常时，插件会直接显示官方原文并继续发送，不会丢失告警。
- 插件不会把事件文本发送给翻译模型以外的服务；具体数据处理方式取决于你配置的默认对话模型提供商。

## 命令

- `/厂商状态`
- `/vendor_status`

以上命令行为相同：收到指令后立即抓取所有启用来源并返回最新状态总览图，不会修改自动告警的去重或送达状态。命令仅在 aiocqhttp 平台生效，群聊和私聊均可使用。

## 通知规则

- 默认在首次成功检查时立即报告当时已经存在的异常；可通过 `notify_existing_on_first_startup` 关闭首次存量告警。
- 相同状态不会重复发送；严重度、受影响服务或官方说明变化时会发送更新。
- JSON 来源明确恢复后立即通知；RSS 事件连续两轮成功检查均消失后通知恢复。
- 单个来源请求失败不会被误判为恢复，也不会额外向群内发送“监控失败”告警。
- 某个群发送失败时，下轮只重试该群。

## 图标

OpenAI、Claude、Google Vertex AI / Gemini、Groq、Cohere、AWS、Azure、GitHub 和 Cloudflare 的厂商 SVG 标识来自 [LobeHub Icons](https://icons.lobehub.com/components/lobe-hub)（`@lobehub/icons-static-svg@1.94.0`，MIT License）。许可全文随插件保存在 `assets/icons/LOBEHUB_LICENSE.txt`，各品牌名称与商标归其权利人所有。

所有图标均内置在插件中，运行时不会联网下载。告警阶段、严重度、服务、时间和链接也使用本地 SVG；自定义 Statuspage 来源没有匹配的厂商标识时继续使用通用厂商 SVG，不会生成首字母头像。

## 故障排查

- 没有主动消息：确认 `group_whitelist` 非空、群号为纯数字，并确认 aiocqhttp 已连接。
- 多个 aiocqhttp 实例：在 `platform_id` 中填写目标实例 ID。
- 状态查询显示“数据不可用”：检查 AstrBot 日志、网络连接和全局代理配置。
- 图片没有中文译文：确认已启用 AI 翻译并配置了可用的默认对话模型；详细原因会记录在 AstrBot 日志中。
- 中文显示为方框：可在 AstrBot `data/font.ttf` 放置支持中文的字体。插件也会自动尝试微软雅黑、Noto CJK 和苹方。
