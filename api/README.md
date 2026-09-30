# 咖啡馆 NPC 后端

一个 FastAPI 进程维护一位老板娘的共享 Harness。Godot 是位置、导航、杯子和原子动作的唯一事实来源；Python 管理订单、会话、待答事项、战略和 SQLite 记忆。

## 启动与配置

要求 Python 3.11–3.13 和 uv。依赖固定在 `pyproject.toml`。从项目根目录启动的入口为：

```powershell
uv run --project api uvicorn cafe.main:app --app-dir api --host 127.0.0.1 --port 8765
```

启动入口也会自动通过 `python-dotenv` 加载本地 `api/.env`，不覆盖已有进程环境变量；文件不存在时照常启动并显示未配置。也可显式交给 uv：

```powershell
uv run --project api --env-file api/.env uvicorn cafe.main:app --app-dir api --host 127.0.0.1 --port 8765
```

网页调试只需运行上述后端命令，**无需启动 Godot 编辑器或游戏**。也可以在 `api` 工作目录使用 `uv run python -m cafe`，监听同一端口。Web 功能通过浏览器 MCP 验证；涉及真实 3D 场景时才通过 Godot MCP 驱动游戏交互和取证。

WebSocket 为 `ws://127.0.0.1:8765/ws/cafe`。后端固定使用 8765；Godot MCP Native 独立使用 9080。`GET /health` 返回配置和场景连接状态，不含密钥。只接收一个 Godot 世界连接。

复制 `.env.example` 为未跟踪的 `.env` 后设置 `MODEL_API_KEY`，或设置 `DEEPSEEK_API_KEY`。默认 `NPC_MODE=model`、`MODEL_BASE_URL=https://api.deepseek.com`、`MODEL_ID=deepseek-flash`；三类任务都通过真实 HTTPX Chat Completions 请求。没有 Key 时状态为 `unconfigured`，不会暗中切换规则。超时或校验失败保留待答项并显示错误；明确点击 `retry_pending` 才重新尝试，不无限重试。

`NPC_MODE=rule` 是显式的阶段联调替身，仅根据按钮和已知事件推进，不推测自由文本意图；状态、台词和经历均可识别为规则模式。实际模型次数保持零。主线自然语言体验必须使用 `model` 并完成事件、战略、摘要的真实调用验证。

`CAFE_DB_PATH` 默认为 `api/data/cafe.sqlite3`。联调可指定独立数据库，避免把测试人物经历混进演示资料。重启只保留玩家资料、经历、话题进度和未来约定，不恢复运行中的订单或动作。

## Web 调试台

启动后端后，打开 [Harness 调试台](http://127.0.0.1:8765/debug/)。默认使用独立调试，启动会话后让 A/B 入店即可测试，无需 Godot。页面与 API 同端口，由现有 FastAPI 提供，无需安装 Node.js 或额外前端服务。

- **独立调试**：选择真实模型或规则模式并启动会话；选择 A/B、入店后发送对话或交互。订单动作必须通过页面中的手动模拟回执推进，可选择完成、失败、暂停或恢复。这里测试 Harness 的决策和状态，不验证 Godot 的位置、动画或杯子实体。
- **实时 Godot**：查看已连接游戏的状态、模型调用和实际动作回执。可以代表当前在场玩家发送发言和交互；入店、离店、字幕展示及动作结果仍由 Godot 回报。该模式不会占用第二个游戏 WebSocket 连接。
- **流程与调用**（默认工作区）：首先按玩家消息查看处理流程，也可从每条玩家发言旁的“查看处理流程”进入。流程图实时展示接收、感知、排队、数据注入、生成提议、校验、采纳变更、发言动作和回执；点击节点展开实际证据与原始事件。数据注入节点按来源展开本轮上下文，并可跳转到实际模型输入。状态和拒绝原因来自运行记录；未记录或未进入的阶段明确标注，不按时间顺序猜测消息关系。
- **模型调用与日志**：在分析区侧栏切换“调用／日志”；宽屏左侧选记录、右侧阅读全文，窄窗口通过列表按钮打开侧栏。支持类型筛选、全文搜索、上一条／下一条和跟随最新；调用详情保留“实际输入、模型输出、处理结果、可选动作、系统规则、原始 JSON”六个页签，可返回所属消息流程。规则模式明确标注无 HTTP。
- **交互测试**：进入该工作区后操作玩家输入、对话、状态、记忆和动作回执。切换工作区保留输入草稿及当前会话；提示、动作和待展示发言在导航中显示数量。只有对话实际可见时才回传字幕展示，不会把调用分析里的文本当作已经向玩家讲解。

调试记忆默认保存到 `api/data/harness_debug.sqlite3`，与游戏的 `cafe.sqlite3` 分开。停止独立会话会取消其进行中的模型任务，保留调试记忆；再次启动可验证跨访问续谈。可用 `CAFE_DEBUG_DB_PATH` 指定其他调试数据库。

每个独立会话或 Godot 连接都会创建一份 `api/logs/*.jsonl`，路径显示在页面中，可直接导出；`CAFE_TRACE_DIR` 可修改目录。内存保留最近 500 条事件，完整会话详情在文件中。日志记录玩家事件、重要输出和每次模型调用；世界快照、定时 tick 和完整运行状态不会逐帧写入日志。密钥和授权头不进入日志，已知凭证字符串会脱敏。页面关闭后仍可查看已保存的 JSONL；后端重启后，页面从新会话开始，不自动重放旧日志。

新发言以运行时生成的 `flow_id` 关联待办、模型调用、发言、动作和回执；`flow_step.data.stage` 记录各阶段实际判断。多人共享战略以明确的关联依赖展示，不归入某个玩家的私有决策。旧日志没有关联标识时仅展示已知输入；日志窗口缺少起点时显示证据缺口，完整记录仍可导出。

常用调试接口位于 `/debug/api/`：`state`、`events?after=序号`、`export` 为读取入口，`session` 管理独立会话，`input` 接收玩家事件；`feedback` 与 `speech` 仅允许独立调试使用。带 `session_id` 的过期页面写入会被拒绝。

## 模型输入与动作候选

事件模型使用 `model_context.py` 从当前运行时生成中文情境摘要：本次事件、在场人物和已观察设施、NPC 通道及当前动作、战略方向、当前玩家记忆、相关订单／会话和最近交互。实际 `messages[1].content` 是分行的事实文本，不再序列化环境 JSON 对象；战略和经历摘要的输入也使用文本。坐标转换为附近站位时只表示观察位置，不表示已经抵达；旧观察明确写为“上次看见，当前情况未知”。完整 `runtime_update` 和调试状态仍保留原始事实。

内部 `available_actions` 每轮按当前玩家和状态生成，实际输入以“本轮可选动作”文本列表列出动作名及各参数允许值。例如有本人未结束订单时不再提供点单；运行中提供暂停，暂停后提供继续；不列其他玩家的订单控制。身体忙时不提供移动或整理，但仍可点单、回应和邀请讲解。玩家拒绝后停止主动邀请，主动要求续谈时仍可继续；段落讲解和活动结果事件只生成发言，没有额外动作。等待用 `action=null`。

固定规则保留在系统消息中，仅追加本轮候选动作的说明；输出 schema 同步缩小动作类型和参数键，删除展示用 `title/default`，仅玩家发言事件允许记忆修改。模型返回后和执行前分别校验候选，防止非法参数、越权对象或异步期间已失效的动作。页面给玩家的交互选项不受此次筛选影响。

模型输出和接口封装继续使用 JSON，方便严格校验和执行；内部追踪记录也保留结构化数据。调用分析的“实际输入”显示实际发送的 USER 消息，“系统规则”显示 SYSTEM，“原始 JSON”可查看完整请求、HTTP 响应、校验、采纳结果及整次调用日志。

2026-09-30 最终中文情境版实测：无旧记忆的首次入店请求由原日志 1986 个输入 token 降到 1138 个（约减少 43%）。该数值只比较这类输入，复杂多人事件会随有效状态增加；不据此推断固定的响应耗时改善。验证记录见 [开发记录](../docs/开发记录.md)。

## 接口与业务约束

握手协议 `cafe3d.v1`，`room_id=cafe_demo`，`npc_id=npc_owner`，`character_id=owner_01`。测试身份固定 `p_A` 至 `p_H`，当前 UI 使用 A/B。场景须注册 `coffee_machine`、`counter`、`recycle_spot` 站位，`machine_01`、`counter_01` 实体，`pickup_socket`、`recycle_socket` 插槽，以及支持的原子动作。

主要输入：`scene_ready`、`world_snapshot`、`player_entered/left`、`utterance`、`interaction`、`action_feedback`、`speech_feedback`、`memory_request`。世界快照只将可见实体与自己同步至模型观察；不可见对象保留最后观察。`utterance` 的 `target_id=room,channel=room` 是有效公共发言；仍要求玩家在场。带 `perceived_by` 时，以 Godot 对此次发言实时计算的听觉事实为准；缺失时，本地频道回退最近快照的听觉列表，公共频道回退当前在场关系，不用旧快照否定新的事件感知。

`interaction` 都包含 `player_id` 和 `kind`，其余字段如下：

| kind | 必需或可选字段 | 作用 |
| --- | --- | --- |
| `order_drink` | `value=recipe_id` 或 `{recipe_id}` | 发出点单确认，饮品为 `americano/latte/pourover/decaf` |
| `confirm_order` | `order_id,prompt_id` | 玩家确认后排队制作 |
| `collect_cup` | `order_id` | 发送领取动作，距离由 Godot 实际检查 |
| `pause_order/resume_order/cancel_order` | `order_id` | 只控制该订单，取消后实际回收已存在杯子 |
| `retry_cleanup` | `order_id` | 明确重试失败的实体杯回收 |
| `invite_topic` | `value=pourover_intro`，可省略 | 发起手冲邀请 |
| `accept_invitation/decline_invitation` | `conversation_id,prompt_id` | 接受或本次暂缓 |
| `pause_topic/resume_topic/next_topic/next_visit/end_topic` | `conversation_id` | 暂停、继续、下一段、下次继续或结束 |
| `correct_preference` | `value:{key,value}` | 立即落盘玩家偏好纠正 |
| `forget_topic/disable_reminder` | `value=topic_id` | 忘掉特定话题或关闭提醒 |
| `stop_speech/give_space` | 可选 `conversation_id` | 停止发言或本次不再邀请 |
| `ask_menu` | 无 | 排入菜单待答项 |
| `start_activity/choose_activity/leave_activity/decline_activity` | 选择时 `value=light/dark` | 一轮咖啡口味猜想；主动邀请先等明确接受 |
| `retry_pending/tidy_counter/end_visit` | 无 | 重试失败待答、整理柜台或正常结束访问 |

`interaction_prompt` 包含 `prompt_id,player_id,kind,text,options` 以及相关 `conversation_id/order_id/activity_id`；`options` 每项为 `{label,kind,value}`。前端必须原样回送业务 ID，不发送脱离上下文的 yes。`runtime_update.prompts` 为当前有效提示全集，前端据此移除失效卡片。

邀请到期每轮按单一的一秒时间门限检查；持续到达的世界快照不会阻止 90 秒邀请到期。

`runtime_update` 包含 `mode,model,present_players,world_view,situation,strategy,goals,agenda,conversations,orders,active_skills,body,memory_refs,prompts,activity,last_error`。`model.calls` 是实际请求次数；`model.successes` 分别在事件提议被接受、战略被更新、经历被写入后增加，过期或校验拒绝的结果不计成功，按 `event/strategy/episode` 分组。规则模式不增加真实调用次数或成功次数。

暂停、继续订单会触发战略复评。调度器还比较候选目标的稳定 ID 与业务状态，待答新增或结束后更新战略；同一待答从 `pending` 到 `generating/presenting` 不重复触发。发言展示期间仍保留待答目标，结束回执后才移除。无候选实质变化、无显式复评事件时，世界快照和空闲 tick 不增加战略请求。

`memory_view` 包含 `player_id,character_id,profile,topics,commitments,episodes`。`profile.preferences` 为键值对象；每个主题的 `stage` 表示下一段，从 `beans`、`water_temperature`、`pouring` 到 `finished`。

订单顺序是移动到咖啡机、`brew`、移动柜台、`place_cup`、等待玩家实际领取、`collect_cup`。每一步必须收到匹配 `request_id` 的 `completed` 回执，移动必须含实际 `position/anchor_id`；杯子动作须含匹配的 `cup_id` 和对应 `order_id/socket_id/holder_id`。`started/paused/resumed` 不推进。动作暂停后收到迟到完成只保留下一阶段，不能自动继续。

经历摘要中的实际动作暂停与继续仅来自匹配的 Godot `paused/resumed` 回执，并记录 `request_id`、命令与实际结果；发送控制请求不等于动画已暂停或恢复。没有活动动作的排队／可领取订单，可直接记录 Harness 的暂停／继续状态变化，并标明 `source=harness_state`。摘要因此可以区分“请求暂停、实际暂停、实际继续、最后完成”的顺序。

为实际回收柜台杯，增加薄原子动作 `retrieve_cup(cup_id)`，不在 Godot 复制订单流程。取消时若 NPC 持杯，走到回收位后放杯；若柜台持杯，走到柜台→取回杯子→走到回收位→放杯。杯子由玩家持有后不能撤回。单取餐插槽未清空时，新订单不会开始制作。

字幕回执使用 `displayed / finished / interrupted`。HUD 一次展示完整段落，并在真实画面绘制后发送 `displayed`；后端据此幂等推进讲解进度并立即落盘。因此玩家看见水温全文后立即选“下次继续”，保存的下一段就是注水。`finished` 只释放发言通道并完成该待答项；`interrupted` 不撤回已经展示的进度。尚未收到 `displayed` 的内容，即使收到 `finished`，也不能推进或记成已经讲过。发言队列与身体动作分开，因此 A 的咖啡制作时仍可回应 B。

## 实现与验证记录

复用 FastAPI WebSocket、`asyncio.Queue`、HTTPX、Pydantic 和 `sqlite3`。仅补充当前业务需要的事件调度、三组 Prompt、服务阶段、话题进度和玩家记忆，没有新建通用状态机、协议栈或测试运行器。模块为 `main`（连接）、`runtime`（唯一事件入口）、`orders/topics`（具体业务）、`memory/model/config/schemas`（边界）。

DeepSeek 低延迟参数按[官方 Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)设置 `thinking.type=disabled`。JSON 输出可通过 `MODEL_JSON_MODE` 关闭以适配其他兼容服务。供应商 URL/模型固定在实际配置中；模型接口不返回或记录密钥。

本目录的 `tests/test_runtime.py` 是标准库 `unittest` 用例源码，覆盖回执配对、暂停竞态、多人隔离、取餐位占用、迟到提议、字幕进度和 SQLite 重开。未通过 MCP 执行这些 Python 用例时，必须标为未执行；Native 的 Python 测试入口还要求受支持的测试路径、解释器和依赖，不能将发现工具等同于已运行本目录用例。真正的场景、后端和模型验收由根项目的 MCP 游戏交互记录给出。
