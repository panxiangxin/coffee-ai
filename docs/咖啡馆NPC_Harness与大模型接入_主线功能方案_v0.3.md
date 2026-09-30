# 咖啡馆 NPC：Harness + 大模型接入主线功能方案

**版本：v0.3｜日期：2026-09-30｜定位：单房间、单角色、多人互动的功能原型**

## 0. 本版目标与范围

实现一位“娜洛式体验”的咖啡馆老板娘：能感知环境、有持续目标、会对事件作出适当反应，能够聊天、移动、服务、参与活动，并在玩家下次出现时延续共同经历。

本方案是针对当前原型的设计，不是米哈游内部实现。以下接口、状态、阈值和开发顺序均为本项目建议；本文交付的是实施方案，不代表这些模块已经实现。

**主线闭环：环境观察 → 情境理解 → 玩家记忆与当前目标 → 事件决策 → 状态转移 → 对话及动作 → 新的观察和记忆。**

本版删除安全体系、事件重放、容灾恢复、事务性 outbox、分布式一致性、审计、灰度发布、容量治理等要求。既不建设这些模块，也不将它们设为原型验收前提。

保留的状态检查、简单模型超时、玩家区分、暂停和记忆落盘，仅用于让角色功能正常工作，不扩展成生产级基础设施。

### 0.1 必须实现的核心能力

| 能力 | 第一版必须做到 |
| --- | --- |
| 环境感知 | 知道谁在场、谁在说话、谁对谁说话、自己正在做什么、场景里有什么可用设施 |
| 情境理解 | 识别新客进入、已有对话、待答问题、互动机会、任务受阻等局面 |
| 战略决策 | 根据职责、约定和环境，维持当前目标，不因每条消息不断换目标 |
| 事件决策 | 对新消息和场景变化选择回应、延后、继续、打断或重规划 |
| 状态转移 | 分别推进目标、会话、任务和身体状态，支持正常暂停与继续 |
| 玩家记忆 | 记住偏好、共同经历、关系约定、话题进度和未来承诺，支持纠正 |
| 对话与行动 | 说话、看向玩家、移动、邀请、续谈、制饮、递杯和整理 |
| 大模型接入 | 接入一个实际可用模型，完成事件响应、战略选择和经历摘要 |

### 0.2 原型场景

默认一个咖啡馆、一个老板娘、2～8 名测试玩家。使用文本聊天和简单可视化场景；玩家使用固定测试 ID，不开发账号系统。跨会话关系使用同一个 `character_id + player_id` 关联。

**角色必须在场景中真正改变朝向、位置和任务状态，不能只在台词中说“我走过去了”。** 最小表现采用网页 2D 平面图和角色图标；已有游戏项目可以直接实现相同的引擎适配接口。

写实 3D、美术精修、语音输入输出、实时生成骨骼动作和基座模型训练不属于本版必要工作。基础动作和记忆互动则不是可选项。

---

## 1. 技术架构：一个运行时，一个模型，多种任务

### 1.1 最小技术组合

| 部分 | 本版选择 | 负责的工作 |
| --- | --- | --- |
| 后端 | Python + FastAPI + WebSocket | 接收玩家输入、推送角色回复和场景变化 |
| NPC 运行时 | 自定义 `NpcRuntime` + `asyncio` | 管理事件、状态、模型任务和技能 |
| 数据结构 | Pydantic | 定义模型输出和技能参数 |
| 长期数据 | SQLite | 保存玩家资料、共同经历、话题进度与未来承诺 |
| 模型接入 | HTTPX + 配置化模型接口 | 调用支持所选 Chat Completions 协议的模型 |
| 场景 | 网页 2D 场景 + 后端离散模拟器 | 展示走路、朝向、制饮、递杯和状态变化 |
| 调试 | 同页运行状态面板 | 看到当前观察、目标、待办、记忆和动作阶段 |

为减少非主线搭建，本地原型使用 SQLite，而非要求先部署 PostgreSQL。当前房间、待办和活动技能保存在内存；跨次访问需要的记忆与约定保存到数据库。**程序重启后重新开店，不恢复运行中的动作；正常结束互动后保存的记忆与话题进度仍可读取。**

### 1.2 模块结构

```text
玩家输入 / 场景变化
        ↓
NpcRuntime：唯一状态更新入口
        ↓
PerceptionSystem → SituationBuilder
        ↓                  ↕
GoalManager / StrategyPlanner ← MemoryService
        ↓
AgendaScheduler → ContextBuilder → EventPlanner
        ↓
PlanApplier：把提议转换为状态变化
        ↓
SkillRunner → WorldAdapter / PresentationAdapter
        ↓
动作与互动结果 → 重新进入事件队列
```

这里的模块大部分是普通程序，不是独立语言模型 Agent。先复用一个模型、三组任务 Prompt，不做多 Agent 协商。

### 1.3 各模块具体职责

| 模块 | 输入 | 输出 |
| --- | --- | --- |
| `PerceptionSystem` | 场景快照、玩家发言、任务反馈 | 角色观察 `Observation` |
| `SituationBuilder` | 最近观察、会话状态、任务状态 | 当前局面 `Situation` |
| `MemoryService` | 玩家、话题、当前事件 | 偏好、经历、进度、未完成约定 |
| `GoalManager` | 局面、职责、承诺 | 需要推进的候选目标 |
| `StrategyPlanner` | 候选目标、已有战略 | 当前目标优先顺序与互动方式 |
| `AgendaScheduler` | 新事件、待答事项、当前目标 | 本轮处理事项 |
| `EventPlanner` | 决策上下文 | 回复、动作、记忆更新候选 |
| `PlanApplier` | 模型提议、当前状态 | 会话/任务变化、待执行技能 |
| `SkillRunner` | 已选择技能、场景状态 | 逐步执行、完成/暂停/失败事件 |
| `PresentationAdapter` | 说话、表情、视线和动作意图 | 前端字幕、动作和场景更新 |

**同一老板娘只有一份共享运行状态。** 不为每个玩家启动互相独立的“老板娘”，否则多人请求会驱动相互矛盾的身体行为。

---

## 2. 统一状态：事实、理解、意图分开保存

### 2.1 运行时状态

```text
NpcState
├─ world_view           目前可感知的玩家、位置、设施和物品
├─ situation            对话组、待答问题、新客、空闲/忙碌、当前互动机会
├─ strategy             当前目标、互动方式、下一阶段
├─ agenda               尚未处理的事件和待答事项
├─ conversations        会话对象、主题、阶段、进度、等待回应的问题
├─ active_skills         当前技能与执行阶段
├─ body                 朝向、移动、双手、表情、发言状态
├─ working_memory       近期对话、当前意愿、正在讨论的资料
└─ player_memory_refs   本轮需要的玩家记忆与未来承诺
```

`world_view` 保存观察，不由模型任意改写；`situation` 保存对局面的理解；`strategy` 和 `active_skills` 保存目标与行动。三者不能用一段聊天摘要替代。

玩家说“我已经坐在窗边”，可以帮助理解他的意思；但角色是否应该走向窗边，仍以场景位置为准。做咖啡成功也来自任务结果，不是来自模型说“做好了”。

### 2.2 事件最小结构

```json
{
  "event_id": "e_101",
  "type": "player.utterance",
  "player_id": "p_A",
  "time": 125.4,
  "payload": {
    "text": "今天先不讲手冲了，我陪朋友坐一会儿。",
    "addressee": "npc_owner",
    "topic_id": "topic_pourover"
  }
}
```

`time` 为运行时接收该事件的场景时间。事件只在当前进程中排队处理，不设计持久事件流和历史重放。

第一版事件类型：

```text
player.entered / player.left / player.moved
player.utterance / player.interaction
world.changed
skill.started / skill.completed / skill.failed
conversation.accepted / conversation.declined / conversation.paused
model.event_ready / model.strategy_ready / model.memory_ready
model.failed
timer.tick
```

模型与技能完成后回送事件，不直接在异步回调中修改全局 NPC 状态。

---

## 3. 环境感知与情境理解

### 3.1 场景必须提供的信息

| 信息 | 最小字段 | 用于什么 |
| --- | --- | --- |
| 玩家 | ID、名字、位置、进入时间、最近发言时间 | 欢迎、找人、等待、判断互动对象 |
| 发言 | 说话者、指定对象、内容、话题、时间 | 判断轮到谁、指代和话题关系 |
| 设施 | 咖啡机、柜台、菜单、座位的位置与可用状态 | 决定能做哪些动作 |
| 自身 | 位置、朝向、活动技能、已承诺事项 | 不让新决策覆盖正在做的事 |
| 互动结果 | 接受、拒绝、暂停、完成、玩家离开 | 调整目标和会话阶段 |

原型先用距离、区域和聊天对象建立感知，不接视觉识别。角色离开某个区域后，对看不见的物体保留“最后看到的状态”，而不是继续获得其位置。首版不要求复杂光学遮挡计算。

### 3.2 情境构建规则

`SituationBuilder` 每次处理有效事件后更新下列内容：

```json
{
  "present_players": ["p_A", "p_B", "p_C"],
  "conversation_groups": [["p_A", "p_B"]],
  "newcomers": ["p_C"],
  "pending_questions": ["q_B_1"],
  "npc_activity": "brewing",
  "available_channels": ["speech", "gaze"],
  "opportunities": ["welcome_p_C_at_next_gap"]
}
```

使用显式 `addressee`、`reply_to` 和近期往返发言识别对话关系。多人同时聊不同话题时，先保留各自话题和待办；老板娘一次只推进一个公共发言窗口，不需要同时主持多条对话。

没有明确对象时，由本次事件模型顺带判断是否对老板娘说话；不为每一条输入单独增加一次“情境模型”调用。

### 3.3 从事实到行动机会

| 观察 | 可形成的情境 | 不能直接认定 |
| --- | --- | --- |
| C 新进店，尚未说话 | 可以找机会欢迎 C | C 一定孤独或尴尬 |
| A、B 正在连续对话 | 等停顿再插入 | A、B 不欢迎别人 |
| 玩家在看菜单并提问 | 可以提供饮品建议 | 玩家已经点单 |
| 玩家走远 | 暂时不适合递杯或继续近距离交谈 | 玩家永远不想继续 |

情境条目至少带 `evidence_ids` 与 `updated_at`。没有观察依据的细节不写进局面摘要。

---

## 4. 战略决策：决定“接下来想促成什么”

### 4.1 长期职责与具体目标

老板娘的长期职责固定为：经营自己的咖啡馆、兑现约定、营造自然交流、保留自己的活动节奏。具体目标随环境和关系变化产生。

| 目标 | 来源 | 主要阶段 | 完成或结束依据 |
| --- | --- | --- | --- |
| `welcome_guest` | 新客进入 | 找到窗口 → 欢迎 → 等回应 | 已提供欢迎机会，或玩家离开 |
| `continue_shared_topic` | 旧话题与未来约定 | 提醒/询问 → 接受后继续 → 保存进度 | 本次讲解结束或玩家暂缓 |
| `serve_guest` | 玩家确认饮品 | 制作 → 递交 | 完成交付或玩家取消 |
| `host_small_activity` | 玩家表达参与意愿 | 邀请 → 收集回应 → 主持 → 收尾 | 活动结束或参与者退出 |
| `maintain_cafe` | 店务日程/无紧迫任务 | 整理 → 查看菜单 → 巡视 | 单次整理结束或被新任务打断 |
| `give_space` | 玩家表示暂不互动 | 停止主动邀请 → 保持正常服务 | 玩家主动恢复互动或本次访问结束 |

“机会已提供”与“玩家已经参与”分开。不能把欢迎目标设计成必须让客人聊天才算成功。

### 4.2 目标生成与选择

`GoalManager` 用规则生成候选。例如新玩家进入产生欢迎候选，旧约定满足触发条件产生续谈候选，空闲时产生店务候选。

`StrategyPlanner` 在候选之间选择，输出最多三个活跃目标，以及一个当前互动方式。已接受的任务不能因低优先级兴趣被直接丢弃。

```json
{
  "active_goal_ids": ["g_serve_A", "g_welcome_C"],
  "primary_goal_id": "g_serve_A",
  "interaction_mode": "brief_and_warm",
  "next_step": "制作期间等待话题空隙，简短欢迎C",
  "review_on": ["primary_goal_finished", "player_declined", "scene_changed"]
}
```

`next_step` 用于约束后续语义决策，不直接当作可执行代码。真正执行时仍落到现有技能。

### 4.3 什么时候重新规划

当前目标完成、相关玩家改变意愿、执行受阻、角色得到新的明确承诺时重评。一般问答只插入反应，不推翻战略。

配置一个低频检查间隔，例如 30 秒；**只有情境或目标有值得处理的变化，才调用战略模型。** 正在服务或已明确被拒绝时，不靠定时器持续尝试新的搭话。

第一版战略使用目标模板 + 规则优先级 + 模型选择，不训练强化学习策略，也不引入自由规划搜索。

---

## 5. 事件决策：决定“这件事现在怎么处理”

### 5.1 调度与语义判断分开

`AgendaScheduler` 决定本轮看哪件事；`EventPlanner` 决定对该事项作何回应。

调度顺序：先处理停止/取消等明确控制，再处理已经答应且需要推进的事项，然后是待答问题、新客请求，最后才是可选搭话。等待过久的问题提高优先级，同一玩家短时连续输入可以合并。

每个待办保存：`item_id`、`player_id`、`topic_id`、`created_at`、`status`。回复后再标记处理完成，延后不等于删除。

### 5.2 五种反应

| 类型 | 如何改变运行时 | 例子 |
| --- | --- | --- |
| `CONTINUE` | 不增加新动作，继续当前计划 | 玩家在远处换座位 |
| `REACT` | 增加一个兼容的回复或技能 | 制作咖啡时回应一句问题 |
| `DEFER` | 保留待办并设置再次处理的条件 | 别人正在说话，稍后回答 |
| `PREEMPT` | 暂停或停止指定会话/任务 | “今天先不讲了” |
| `REPLAN` | 标记当前战略需要调整 | 活动参与者全部离开 |

### 5.3 事件提议结构

```json
{
  "reaction": "PREEMPT",
  "goal_id": "g_continue_A",
  "speech": {
    "target_player_ids": ["p_A"],
    "text": "好，那今天就先陪朋友坐坐，手冲的话题留到你想听的时候。",
    "emotion": "warm"
  },
  "action": {
    "type": "pause_topic",
    "args": {"conversation_id": "c_pourover_A"}
  },
  "memory_updates": [
    {
      "kind": "session_preference",
      "key": "proactive_pourover_invitation",
      "value": "今天不再主动邀请",
      "evidence_event_id": "e_101"
    }
  ]
}
```

每次事件提议最多一个需要改变活动状态的技能，允许附带兼容的回复、表情和记忆更新。`action` 或 `speech` 没有需要时为 `null`。

`REACT` 可以只有一句话；`DEFER` 可以没有回复；`REPLAN` 可以先结束失效活动，再请求战略更新。第一版不让模型一次输出无限动作序列。

### 5.4 几个必须区分的自然语言意图

“别说了”结束当前发言，不一定取消订单；“这杯不要了”取消对应饮品任务；“等我一下”暂停当前互动；“下次再聊”保存进度并结束本轮。

对含糊指代先问清对象。界面同时提供暂停、继续、结束活动按钮，便于最先跑通功能，再优化语言理解。

---

## 6. 状态转移：让目标、会话、动作相互配合

### 6.1 并行状态区域

| 区域 | 最小状态 |
| --- | --- |
| 目标 | `pending / active / suspended / completed / dropped` |
| 会话 | `idle / inviting / waiting_reply / engaged / paused / closed` |
| 发言 | `silent / generating / presenting` |
| 技能 | `queued / running / waiting / paused / done / failed / cancelled` |
| 身体 | `movement`、`hands`、`gaze`、`expression` 各自状态 |

不要使用一个总的 `is_busy`。她可以双手制作咖啡，同时讲话；可以暂停讲解，同时继续服务其他玩家。

### 6.2 状态转移契约

```text
当前状态 + 本次事件 + 必要条件
    → 新状态 + 开始/暂停/结束的技能 + 新待办
```

| 当前状态 | 事件 | 新状态和动作 |
| --- | --- | --- |
| 话题 idle | 决定邀请 | inviting，发出邀请 |
| inviting | 邀请实际显示 | waiting_reply，等待玩家 |
| waiting_reply | 玩家接受 | engaged，从话题进度开始讲解 |
| waiting_reply | 玩家拒绝 | closed，本次不继续追问 |
| engaged | 玩家说稍后再聊 | paused，保存已讲部分与下一阶段 |
| paused | 玩家要求继续 | engaged，读取进度后继续 |
| engaged | 讲解结束 | closed，保存共同经历 |

玩家没有回答不等于接受。设一个简单等待期限，超过后结束本次邀请即可，不建设复杂计时服务。

### 6.3 普通中断与继续

只暂停与当前请求相关的会话或技能；保留其他玩家的任务。继续时检查玩家仍在、目标仍适用、需要的资源可用。

模型还在生成时，新的明确暂停/取消照常处理。使用一个简单 `decision_id` 对应当前待办；返回结果若已经不适用，则不再执行旧计划。除此之外，不引入多层版本票据或复杂并发协议。

---

## 7. 玩家记忆：直接影响目标和行动

### 7.1 五类信息

| 类型 | 内容 | 保存位置 |
| --- | --- | --- |
| 当前交互 | 近期对话、指代、本次意愿、未答问题 | 内存 working_memory |
| 玩家偏好 | 口味、称呼、偏好的交流方式 | player_profiles |
| 共同经历 | 双方讨论和参与过什么、实际结果 | episodes |
| 话题/关系进度 | 讲到哪里、双方认可的称呼、未结束互动 | topic_progress + profile |
| 未来承诺 | 下次提醒、约定继续、待完成事项 | commitments |

同一个模型服务所有玩家，但用稳定 `player_id` 区分记忆。换显示名不清空关系，也不能把 A 的话题进度拿给 B。

### 7.2 即时更新与经历整理

**即时更新：**明确口味修正、称呼变化、当前不想聊天、暂停和下次继续，先更新状态。需要跨次访问保留的内容立即落盘；“今天”之类只保留在本次访问。

**经历整理：**一段主题结束、正常离店或用户点击结束本次互动后，异步提取双方共同经历。只记录已经发出的对话和已经发生的动作，不记录尚未执行的模型提议。

异步摘要只增加经历，不修改最新玩家资料。明确偏好修正由事件流程处理，因此不需要为主线额外设计复杂版本合并系统。

### 7.3 读取顺序

先精确查询当前玩家的偏好、有效约定、未完话题；再从该玩家经历中按话题和时间取少量相关条目。首版用 SQL + 关键词匹配，记忆量增长后再决定是否加入向量检索。

建议本轮只带最近 8～12 轮相关对话、当前话题进度、必要偏好和 3～5 条相关经历。数量均为初始配置，不是经过测量的最优值。

必须让模型区分“玩家过去说过”“共同完成过”“这次正在发生”。没有找到证据时，应询问或承认记不清，而不是补出一段共同历史。

### 7.4 前瞻记忆必须按条件触发

“下次我来继续讲手冲”需要保存 `on_return` 承诺，不能只存成一段摘要。

```json
{
  "player_id": "p_A",
  "character_id": "owner_01",
  "kind": "offer_topic_resume",
  "topic_id": "pourover_intro",
  "trigger": "on_next_visit",
  "created_visit_id": "visit_A_01",
  "status": "pending"
}
```

下次进入时，查询该玩家的 pending 承诺；确认不是当前同一次访问，再生成续谈候选目标。首次发出续谈机会后记录本次已提示，不反复触发。

“下次再讲”的话题进度仍保留，玩家主动提出继续时可以恢复；不需要再创建一个隐形订单或强制安排活动。

### 7.5 暂缓、纠正和忘记

“今天不讲”只抑制本次主动邀请，不删除共同经历。“以后不需要提醒”关闭对应承诺。“忘掉上次那段聊天”删除对应经历和与之直接关联的续谈记录。范围不清楚时澄清。

这些是玩家可感知的记忆交互功能。本版只实现当前数据的更新/删除，不建设删除审计、备份清除或历史重放屏障。

### 7.6 记忆要改变行为

同一玩家今天说“老样子”，应先结合他的偏好确认；说“继续上次那个”，应结合未完话题恢复。被明确告知不喜欢突然被拉进活动后，老板娘应调整邀请方式，而不是只记住这句话后照样行动。

**过去的兴趣不能覆盖当前的拒绝。资料记忆、互动目标和实际行动之间必须有这条联系。**

---

## 8. 动作与场景：不能只产生一段描述

### 8.1 最小空间设计

场景锚点：`entrance`、`counter`、`coffee_machine`、`menu_board`、`table_1`、`table_2`。为锚点配置坐标和可通行连接。

移动先采用连接图路径与线性插值；角色沿路径在画面上移动，到达锚点后产生完成事件。无需第一版就开发完整寻路系统，但不能瞬移并声称走到了目的地。

### 8.2 首批技能

| 技能 | 输入 | 具体执行 | 完成依据 |
| --- | --- | --- | --- |
| `look_at` | player_id/entity_id | 转头/改变朝向 | 朝向更新 |
| `move_to` | anchor_id | 选路径并逐步移动 | 到达锚点 |
| `invite_topic` | player_id、topic_id | 发邀请，建立等待回应状态 | 邀请进入等待阶段 |
| `continue_topic` | conversation_id | 根据阶段产生下一段内容 | 该段已显示，进度推进 |
| `pause_topic` | conversation_id | 停止后续讲解，保存进度 | 会话 paused |
| `prepare_and_serve` | order_id | 到设备 → 制作 → 递交 | 场景任务完成 |
| `host_activity` | activity_id、participant_ids | 按轮次给题、收回应、点评 | 活动轮次结束 |
| `tidy_counter` | 无 | 前往柜台，播放整理过程 | 简单计时结束 |
| `wait` | 无 | 不新增动作 | 下一个有效事件 |

### 8.3 做咖啡的状态机

```text
CONFIRM_ORDER → MOVE_TO_MACHINE → BREW → READY → SERVE → DONE
                               ↘ CANCELLED
```

点单确认来自玩家的明确表达或按钮。模型只推荐、解释和提议，不负责逐帧制作。`BREW` 用场景计时推进，`SERVE` 更新饮品位置和订单完成状态。

“咖啡好了”对应 READY；“给你”对应实际递交。不要在任务开始时就输出完成台词。

### 8.4 小活动不是只有活动名称

首个活动固定为“咖啡口味猜想”：给出一轮描述 → 收集参与玩家的选择 → 按预设答案判断 → 模型生成简短点评 → 进入下一轮或结束。

模型负责主持表达，程序记录参与者、题目、回答和轮次。这样能测试多人互动，也能留下真正的共同经历，而不是只有闲聊。

### 8.5 身体通道

移动、双手、说话和视线分别占用。做咖啡时可说话，等待回应时可继续整理；移动目标一次只有一个，双手不能同时递杯和擦台面。

最小表演包含 `idle / listen / talk / greet / brew / serve / tidy`。可以先用图标变化、短动效和文字标签表示，但必须与运行状态一致。

---

## 9. 大模型接入与 Prompt

### 9.1 一个模型，三种调用

| 任务 | 调用入口 | 本轮必须提供 | 返回 |
| --- | --- | --- | --- |
| 事件响应 | `generate_event_plan` | 事件、情境、战略、记忆、技能 | EventPlan |
| 战略选择 | `choose_strategy` | 候选目标、现有目标、玩家意愿 | StrategyProposal |
| 经历整理 | `summarize_episode` | 实际对话、动作结果、话题进度 | EpisodeSummary |

首版不指定“最新”模型名称。使用实际账号已开通、能输出中文和所需 JSON 的模型，将模型 ID 与地址设为配置。三个任务可以先使用同一个模型，之后根据实际体验再区分。

接口示例沿用 v0.2 的兼容 Chat Completions 接入方式。[R1] 供应商原生接口不相同时，只替换 `ModelGateway` 的请求和响应适配，角色运行时不用改。

### 9.2 配置

```dotenv
MODEL_BASE_URL=https://your-provider.example/compatible-mode/v1
MODEL_ID=your-enabled-model-id
MODEL_API_KEY=your-api-key
MODEL_TIMEOUT_SECONDS=15
MODEL_JSON_MODE=false
```

地址和模型 ID 是占位符，必须替换。`MODEL_JSON_MODE` 默认关闭；确认当前供应商和模型支持 `response_format=json_object` 后再开启。原型只保留简单超时和明确错误，不建设重试、熔断、计费治理等组件。

### 9.3 事件 Prompt 核心模板

```text
你是咖啡馆主理人的事件决策模块。人设温暖、机灵、有自己的店务和兴趣。
根据当前事件、环境、目标、会话与玩家记忆，提出一次行为。

优先兑现已接受的事项；普通问答不随意推翻持续目标。
当前玩家意愿优先于过去兴趣；区分今天的意愿和长期偏好。
只回应需要回应的事件；WAIT/CONTINUE 是正常选择。
使用现有技能，不描述实际上没有开始或完成的动作。
共同经历只依据提供的记忆；指代不清时询问。

输出一个 JSON 对象，字段为：
reaction, goal_id, speech, action, memory_updates。
reaction 只能是 CONTINUE/REACT/DEFER/PREEMPT/REPLAN。
speech 或 action 没有需要时为 null。
每轮最多一个活动技能，可附带兼容的说话、表情和记忆更新。
不要输出代码围栏或解释性正文。
```

`ContextBuilder` 将 `persona / event / situation / strategy / commitments / relevant_memories / conversation / available_skills` 作为 JSON 输入附在任务后。提示词不负责直接修改场景。

### 9.4 战略 Prompt 核心模板

```text
从提供的候选目标中选择接下来应维持的目标与互动方式。
优先已有承诺，考虑当前环境、玩家明确意愿和可用能力。
已有目标仍合理时继续，不为表现自主性频繁更换。
“提供机会”不等于“必须让玩家参与”。
返回 active_goal_ids、primary_goal_id、interaction_mode、next_step、review_on。
next_step 是给事件模块的方向，不是执行命令。
没有候选时返回空目标列表和 quiet 模式。
```

### 9.5 经历 Prompt 核心模板

```text
根据本次已经实际发生的对话和动作，整理一条共同经历。
记录参与玩家、话题、双方做过什么、结果、下一次可接续的信息。
未执行提议不算经历；发出邀请不等于接受；讲过不等于玩家理解。
不要修改玩家长期偏好；明确偏好由事件流程单独处理。
返回 summary、topic_id、participant_ids、resume_hint。
```

### 9.6 接入代码示例

下面是模型网关的最小示例，不是完整 NPC 服务。安装 `httpx` 并设置环境变量后可以用于接入验证；此文档没有对真实模型执行调用。

```python
import asyncio
import json
import os
from typing import Any

import httpx


class ModelGateway:
    def __init__(self) -> None:
        self.base_url = os.environ["MODEL_BASE_URL"].rstrip("/")
        self.model_id = os.environ["MODEL_ID"]
        self.api_key = os.environ["MODEL_API_KEY"]
        self.timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "15"))
        self.json_mode = os.getenv("MODEL_JSON_MODE", "false").lower() == "true"
        if self.timeout <= 0:
            raise ValueError("MODEL_TIMEOUT_SECONDS 必须大于零")
        self.client = httpx.AsyncClient(timeout=self.timeout)

    async def generate(self, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("模型没有返回完整的文本结果")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("模型正文不是字符串")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("模型结果必须是 JSON 对象")
            return result
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("本次模型请求或 JSON 解析失败") from exc

    async def close(self) -> None:
        await self.client.aclose()
```

本示例需要 Python 3.11 或更新版本。调用后分别交给 Pydantic 的 `EventPlan`、`StrategyProposal` 或 `EpisodeSummary` 结构解析。当前文档中的 JSON 展示字段含义，实际项目应据此定义完整类型。

事件字段解析失败时保留原待办，不启动动作；在调试面板显示错误即可。必要时让角色用固定短句表示稍后回答，不额外构建模型修复循环。

### 9.7 为什么仍然需要少量结果检查

只检查影响主线的内容：技能是否存在、目标是否在场、会话能否继续、所需设备是否可用、身体通道是否空闲。否则模型输出再像人，也无法转化为场景里的正常行为。

这些检查属于动作连接逻辑，本版不将其扩展为安全框架。

---

## 10. 主循环与执行节奏

### 10.1 三种节奏

| 循环 | 本版起始设置 | 主要工作 |
| --- | --- | --- |
| 场景/技能 | 约 5 次/秒更新，前端平滑显示 | 位置、朝向、制备计时、技能完成 |
| 事件决策 | 有有效事件时触发 | 回复、意图、当前记忆修正 |
| 战略与摘要 | 目标/情境变化或主题结束时 | 目标选择、共同经历整理 |

以上为可修改的原型参数。技能更新不能按这个频率调用模型；角色不需要每一帧“思考”如何继续走路。

### 10.2 主循环逻辑伪代码

```text
接到玩家或场景事件：
  更新观察、情境、会话和技能状态
  明确暂停/取消先改变相关活动
  若玩家进入，查询其偏好、话题进度与待触发承诺
  根据事件新增或调整待办
  若目标需要重评，标记 strategy_dirty

当前没有事件模型任务且存在有效待办：
  调度一个待办
  构建包含战略和玩家记忆的快照
  异步调用事件模型，继续接收其他事件

事件模型完成：
  结果作为 model.event_ready 回到主循环
  确认它仍对应有效待办
  应用即时记忆修正
  按 reaction 转移目标/会话状态
  启动兼容技能，发布适当的回复

每次场景 tick：
  推进移动、制作、等待等技能
  将技能结果放回事件队列

目标需要重评且没有更紧迫的模型任务：
  异步调用战略模型
  结果回来时，仅应用仍然有效的候选目标

话题结束或正常离店：
  立即保存话题进度和未来承诺
  空闲时异步整理共同经历并写入数据库
```

同一 NPC 只保持一个有效事件推理。战略和摘要低优先处理；已有技能照常运行。第一版先用简单优先队列，不建设通用任务平台。

---

## 11. 最小数据库：只保存跨会话功能所需信息

| 表 | 关键字段 | 用途 |
| --- | --- | --- |
| `player_profiles` | character_id、player_id、display_name、preferences_json、interaction_style_json | 偏好、称呼与交互约定 |
| `episodes` | id、character_id、player_id、topic_id、summary、visit_id、created_at | 共同经历，每位参与者关联自己的条目 |
| `topic_progress` | character_id、player_id、topic_id、stage、summary、next_step、status | 正常暂停后接续讲解 |
| `commitments` | id、character_id、player_id、kind、topic_id、trigger、created_visit_id、last_offered_visit_id、status | 下次提醒和续谈机会 |

`player_profiles` 用角色 + 玩家作为主键；`topic_progress` 用角色 + 玩家 + 话题作为主键。其他表使用简单 ID。

当前订单、正在移动的技能、发言窗口和临时待办放在内存。不建立事件表、outbox、命令回执账本、删除屏障、审计表、租约或迁移平台。

数据库只要提供以下方法即可：

```text
get_profile(character_id, player_id)
update_profile(character_id, player_id, changes)
find_episodes(character_id, player_id, topic_id)
save_episode(...)
get_topic_progress(...)
save_topic_progress(...)
list_due_commitments(character_id, player_id, visit_id)
update_commitment(...)
forget_topic(character_id, player_id, topic_id)
```

记忆落盘是主线，因为没有它就无法跨次访问继续关系；恢复整个运行中的游戏现场不在本版范围。

---

## 12. 前端与接口

### 12.1 一个页面完成演示

左侧展示咖啡馆平面图、设施、玩家和老板娘；中间是多人聊天窗口；右侧展示运行状态。

玩家可以选择固定测试身份，点击进入、移动、发言、接受邀请、拒绝、暂停、继续和点单。无需先开发完整角色控制器。

右侧面板至少展示：观察与情境、当前目标、待答队列、会话阶段、正在使用的记忆、动作阶段。这是开发主线功能的必要反馈，不做独立可观测平台。

### 12.2 输入与输出

统一使用一个 WebSocket 入口：`/ws/cafe`。

```json
{
  "type": "utterance",
  "player_id": "p_A",
  "text": "继续上次的手冲吧",
  "target_id": "npc_owner"
}
```

```json
{
  "type": "npc_update",
  "speech": {"text": "上次聊完了水温，这次我们接着说注水。", "targets": ["p_A"]},
  "presentation": {"gaze_target": "p_A", "emotion": "interested", "animation": "talk"},
  "activity": {"type": "continue_topic", "topic_id": "pourover_intro", "stage": "pouring"}
}
```

后端在最小场景中负责位置和任务状态，前端按更新绘制。接入 Unity 或其他引擎时，替换 `WorldAdapter / PresentationAdapter`；战略、事件模型和玩家记忆继续复用。

---

## 13. 项目目录与开发顺序

### 13.1 最小目录

```text
cafe_npc/
├─ app.py
├─ config.py
├─ domain.py                 事件、状态、目标、模型提议
├─ runtime.py                主循环与异步结果回送
├─ perception.py             观察与情境
├─ strategy.py               候选目标与战略模型
├─ agenda.py                 多人待办和话轮调度
├─ context.py                决策上下文
├─ model_gateway.py          模型 API
├─ memory.py                 玩家资料、经历、进度、承诺
├─ storage.py                SQLite 读写
├─ skills.py                 社交、话题、店务技能
├─ world.py                  场景模拟器
├─ prompts/                  event、strategy、episode
└─ web/                      场景、聊天和状态面板
```

### 13.2 实施阶段

| 阶段 | 需要完成 | 阶段结果 |
| --- | --- | --- |
| 1. 场景与骨架 | 2D 咖啡馆、固定玩家、事件、NpcState、主循环 | 能看到角色感知和场景变化 |
| 2. 完整规则闭环 | 目标模板、话轮队列、会话状态机、基础动作、记忆与进度落盘 | 先不靠模型就能欢迎、邀请、拒绝、暂停和下次继续 |
| 3. 接入真实大模型 | 网关、三组 Prompt、EventPlan、战略结果应用、摘要 | 自由表达可以驱动既有行为，不再依赖固定话术 |
| 4. 补全互动内容 | 咖啡讲解、小活动、服务中聊天、主动目标、经历检索与偏好修正 | 多人连续互动与多次到店都成立 |
| 5. 主线联调 | 下节的功能场景逐项演示，调整 Prompt、目标与动作 | 角色说、记、想、做保持一致 |

规则闭环是用来验证接口与状态，不是最终产品。第 3 阶段必须接入真实模型，第 4 阶段必须完成跨会话记忆和实际动作，不能以“后续再做”代替。

---

## 14. 功能验收：只验证角色主线，不做基础设施项目

| 场景 | 通过标准 |
| --- | --- |
| 新客进店 | 观察到新客，选择合适窗口欢迎；不是对每条位置更新都搭话 |
| 多人对话 | 知道自己在回应谁，保留没回答的问题，不混淆不同玩家 |
| 持续目标 | 正在进行的讲解/服务不会因普通插话被忘记 |
| 战略变化 | 玩家改变意愿或环境不再支持时，会换目标或停止 |
| 主动行为 | 没有新问题时可以整理、巡视或提出合适活动，也能保持安静 |
| 对话加动作 | 走向设施、转头、制作与递交在场景中真实发生 |
| 并行行为 | 做咖啡时可回答其他玩家；暂停讲解不停止无关订单 |
| 邀请与拒绝 | 邀请后等接受；拒绝后不继续拉人参加 |
| 记忆接续 | 第二次到店记得已保存的偏好和共同话题 |
| 记忆纠正 | 新偏好和新的称呼要求立即影响下一次回应 |
| 前瞻记忆 | 玩家回来时能检查旧约定，不需要他说出相似关键词 |
| 暂停后续谈 | “下次再说”保存进度；下次从正确位置继续，不全部重讲 |
| 完整模型接入 | 三类任务都接上模型，输出真正进入状态和行动链路 |

不安排攻击测试、事件重放、崩溃恢复、并发事务故障注入、压测指标或灰度发布验收。

### 14.1 首要演示脚本：三次到店

**第一次：**A 听手冲介绍，在水温部分结束时说“下次继续”。保存 topic_progress 和下一次提供续谈机会的承诺；本次聊天结束后整理共同经历。

**第二次：**A 与 B 同来，系统识别 A 并找回进度。A 表示今天陪朋友，老板娘暂缓自己的续谈目标，正常接待 B，不反复邀请 A。

**第三次：**A 说“接着上次那个吧”。老板娘读取进度，从注水部分继续；同时可以朝向器具、走到讲解位置，或按角色设定使用相关动作。

这条轨迹必须同时经过环境、战略、事件、状态、记忆和身体执行，不能只用一次长 Prompt 演出结果。

### 14.2 第二条演示脚本：服务中交流

A 点饮品并确认，老板娘开始制作。B 在过程中提问，她回答 B，但 A 的制作任务持续。A 说稍等递杯，她进入等待交付；A 表示可以了再递交，随后记录实际服务经历。

**验收结论以可见状态与互动结果为准，不以模型自己说“我记住了”“我完成了”为准。**

---

## 15. 相比 v0.2 的范围变化

保留完整角色功能，缩小工程外壳：长期数据改用 SQLite；运行状态留在内存；只有一个 NPC 运行时和一个模型网关；不需要上一版的 SQL 大表集、开发 ZIP 或任何生产级运行组件。

保留玩家身份区分、记忆纠正、状态条件、简单异步和普通任务中断，因为去掉它们会直接导致串人、忘事、说做不一致或无法响应玩家。

删除重放、崩溃恢复、复杂事务、消息可靠发送、多地域/多房间一致性、访问控制框架、安全审计、隐私版本屏障、灰度、容量规划等工作，不把它们藏进主线里重新增加。

**本版完成的判据只有一个：老板娘能基于环境与关系保持目标，用模型理解和回应玩家，把决定变成真实动作，并在下一次互动中接续共同经历。**

---

## 参考与来源

本版基于本次讨论及《咖啡馆NPC_Harness与大模型落地方案_v0.2.md》重写。下列链接沿用上版中与主线直接相关的资料；本次为范围调整，不新增模型排名、性能数据或米哈游内部技术结论。

- [R1] [阿里云：Chat 接口兼容](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)；用于所选接口的配置参考，实际模型与地址由账号决定。
- [R2] [Generative Agents](https://arxiv.org/html/2304.03442v2)；记忆、计划与即时反应的研究参考。
- [R3] [W3C SCXML](https://www.w3.org/TR/scxml/)；分层状态与状态转移的参考。
- [R4] [LongMemEval](https://arxiv.org/html/2410.10813v2)；长期对话记忆的阅读参考。
- [R5] [Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)；项目实现输出结构时的接口文档。
