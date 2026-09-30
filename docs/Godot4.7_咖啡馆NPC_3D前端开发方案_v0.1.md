# Godot 4.7 咖啡馆 NPC：3D 前端开发方案

**前端版本：v0.1｜日期：2026-09-30｜配套后端：Harness 主线功能方案 v0.3**

## 0. 实施结论

使用 **Godot 4.7 + GDScript + 一个 3D 咖啡馆测试场 + WebSocket 连接 Python Harness**。Godot 不只是聊天界面的外壳，而是角色的身体、空间环境和交互执行端。

**Godot 负责世界里真正发生什么；Python Harness 负责角色想做什么、对谁做、何时做，以及记住什么。**

Godot 4.7 stable 于 2026-06-18 发布；官方归档也提供 2026-08-18 发布的 4.7.2 stable。本方案建议团队固定使用 **4.7.2 Standard + 对应导出模板**，文档查询固定在 `/en/4.7/`，不混用滚动更新的 `latest` 文档。[R01][R02]

本方案是项目设计，不是米哈游内部实现。场景尺寸、频率和动作时长是起始参数。文中的 GDScript 为按官方 API 编写的实现示例，**尚未在 Godot 编辑器中执行或与真实 Harness 联调，不代表完整工程已经实现**。

### 0.1 本版必须交付

| 功能 | 必须达到的效果 |
| --- | --- |
| 3D 咖啡馆 | 玩家能走进、离开、接近设施，老板娘能在场景中移动 |
| 多玩家测试 | 同一场景有 2～8 个固定测试身份，可以切换控制与使用脚本配角 |
| 环境感知 | 输出在场人员、位置、朝向、区域、可见／可听信息与设施状态 |
| NPC 身体 | 行走、转向、注视、招呼、制作、递放杯子、整理，与后台任务一致 |
| 对话和活动 | 聊天、选择说话对象、接受／拒绝邀请、暂停／继续话题、多人活动 |
| 记忆交互 | 同一玩家多次到店、继续旧话题、修正偏好，界面能够检查真实读取的记忆 |
| Harness 联调 | 动作指令实际执行，结果回传后再推进后端状态 |
| 功能调试面板 | 直接看到观察、目标、会话、动作和本轮记忆，不依赖猜测 NPC 正在做什么 |

不开发账号系统、生产级联网同步、安全审计、事件重放、崩溃恢复或容灾。语音、写实美术、实时生成动作和开放世界也不作为本版前提。保留暂停、状态检查和动作反馈，因为它们直接决定角色能否正常互动。

### 0.2 关于“多人测试”的明确范围

**首版采用一个 Godot 进程、一份共享 3D 世界。** 所有测试玩家都在这个世界中；测试者切换控制身份，其他角色保持原位或执行预设输入脚本。

它可以验证多人对象区分、同时在场、交错提问、等待和群体活动，**但不等同于多台电脑各开一个客户端的真实联网测试**。后者不是本版开发前提；未来需要时另加输入汇聚和世界状态同步，不让多个 Godot 实例各自控制同一个 NPC。

---

## 1. 与 Harness v0.3 的衔接：替换世界模拟器，不重写大脑

### 1.1 必须修改的边界

v0.3 使用网页平面图和 Python 离散世界模拟器。接入 Godot 后，**Python 不再计算角色移动路径、不再插值坐标，也不再用自己的计时器猜测 3D 动作完成**。

| 数据或行为 | 唯一主控模块 | 另一端如何使用 |
| --- | --- | --- |
| 玩家和 NPC 的真实位置、速度、碰撞 | Godot | Python 接收快照，形成观察与距离判断 |
| 场景锚点、可通行区域、设备实体 | Godot | Python 使用稳定 ID 和状态，不保存另一套手写坐标 |
| NPC 目标、战略、待答队列 | Python Harness | Godot 显示，不自行生成战略 |
| 会话阶段、邀请、活动轮次 | Python Harness | Godot 展示内容、采集玩家选择 |
| 完整技能流程，如 `prepare_and_serve` | Python SkillRunner | 拆成少量 Godot 可执行动作 |
| 移动、制备表现、拿放物品等单步执行 | Godot WorldActionDriver | 回传开始、暂停、完成或失败 |
| 杯子的实体、所在插槽、持有人 | Godot PropController | Python 据结果更新订单阶段 |
| 订单是否确认／取消／完成 | Python Harness | Godot 根据关联动作和 UI 更新表现 |
| 玩家资料、经历、话题进度、下次约定 | Python MemoryService + SQLite | Godot 查询和展示，发送修改意图 |
| 当前字幕、动画、视线与面部表现 | Godot | Python 下发表达意图，接收显示结果 |

**一个业务技能只有一个流程负责人。** 不要在 Python 和 GDScript 中各写一份完整“点单—制饮—交付”状态机。

Godot 可以有局部动作状态，例如 `brew` 的开始、进行、完成；但它不决定该给谁做、是否开始下一订单、玩家拒绝后是否重新邀请。

### 1.2 运行结构

```text
Godot 3D 测试场                          Python Harness v0.3
──────────────────────────────────      ──────────────────────────────
玩家控制、聊天、交互按钮 ────────────→ 事件入口 / NpcRuntime
场景实体和角色观察 ─────────────────→ Perception / Situation
                                             ↓
                                      Memory / Goal / Strategy
                                             ↓
                                      EventPlanner + 大模型
                                             ↓
场景动作执行器 ←──────────────────── WorldAdapter / SkillRunner
字幕、动画、视线 ←────────────────── PresentationAdapter
        ↓                                    ↑
位置、物品变化、实际动作结果 ─────────┘
```

前后端使用普通 JSON WebSocket。Godot 的高层 Multiplayer/RPC 协议不适合直接当作 Python FastAPI 的 JSON 接口；本项目使用 `WebSocketPeer`，而不是把 `@rpc` 发给 Python。[R03][R17]

### 1.3 更新节奏

| 更新内容 | 起始设置 | 说明 |
| --- | --- | --- |
| 玩家／NPC 物理运动 | 60 Hz 物理步 | Godot `_physics_process` 执行 |
| 镜头、UI、动画表现 | 每渲染帧 | 不被模型调用阻塞 |
| 环境快照与感知采样 | 5 Hz | 先发小规模全量快照，不做复杂差分同步 |
| 进入、离开、说话、接受、拒绝、动作结束 | 事件发生时 | 不等下一次 5 Hz 快照 |
| NPC 状态面板 | 后端状态变化时 | 直接显示后台结果 |
| 大模型决策 | 后端按事件和目标变化触发 | 绝不每帧调用 |

这是本项目配置，不是 Godot 的性能保证。Godot 物理循环替代 v0.3 中的低频位置模拟；5 Hz 只用于报告，不用于移动角色。

---

## 2. 工程基线与目录

### 2.1 工程设置

采用桌面测试优先，GDScript 类型标注，窗口起始尺寸 1440×900。先用 Compatibility 渲染器、简单灯光和普通材质完成互动，不依赖高级全局光照；Godot 的不同渲染器有不同硬件与功能范围，后续改用 Forward+ 属于表现选择，不改变交互协议。[R18]

在 Project Settings 中设置 Input Map 和 3D Physics Layers。选定一套物理配置完成整个原型，不在中途更换物理后端来解决业务问题。

约定 **1 个世界单位代表 1 米，Y 轴向上，地面使用 XZ 平面，角色逻辑正前方统一为 -Z**。Godot 3D 使用右手坐标系；导入模型朝向不一致时在 `VisualRoot` 的模型子节点上一次性校正，不修改协议中坐标含义。[R19]

### 2.2 建议目录

```text
cafe_godot/
├─ project.godot
├─ scenes/
│  ├─ cafe_test.tscn
│  ├─ world/cafe_world.tscn
│  ├─ actors/cafe_owner.tscn
│  ├─ actors/test_player.tscn
│  ├─ props/coffee_station.tscn
│  ├─ props/cup.tscn
│  └─ ui/hud.tscn
├─ scripts/
│  ├─ cafe_test.gd                 # 场景装配和信号接线
│  ├─ network/harness_client.gd    # 普通 JSON WebSocket
│  ├─ network/event_bridge.gd      # 前后端消息映射
│  ├─ world/entity_registry.gd     # 稳定 ID → 场景节点
│  ├─ world/interaction_anchor.gd  # 可交互设施和站位
│  ├─ world/observation_exporter.gd
│  ├─ actors/player_controller.gd
│  ├─ actors/npc_motor.gd
│  ├─ actors/npc_senses.gd
│  ├─ actors/npc_gaze.gd
│  ├─ actors/npc_animation.gd
│  ├─ actions/world_action_driver.gd
│  ├─ actions/prop_controller.gd
│  ├─ ui/chat_panel.gd
│  ├─ ui/speech_presenter.gd
│  ├─ ui/interaction_panel.gd
│  ├─ ui/npc_debug_panel.gd
│  └─ testing/test_director.gd
├─ resources/
│  ├─ cafe_config.tres
│  ├─ test_players.json
│  └─ animation_bindings.tres
└─ assets/
   ├─ characters/
   ├─ environment/
   └─ ui/
```

只有一个房间，不需要先建立通用 ECS、插件平台或复杂全局单例。通过 `cafe_test.gd` 注入节点引用并连接 signal 即可。

---

## 3. 3D 场景：先按可交互空间设计

### 3.1 场景布局

建议室内可活动范围约 14×10 米；有柜台后工作区、客人区、入口、两桌座位和一个讲解位置。地面平坦，首版不做楼梯和自动门。

柜台必须留出可走到前厅的侧面通道。不能把老板娘永远关在柜台后，再要求她走到桌边主持活动。

| 区域 | 主要用途 | 必要内容 |
| --- | --- | --- |
| 门外／入口 | 测试真实进店、离店和再次到店 | 门外出生点、入店区域、入口标记 |
| 柜台工作区 | 制饮、整理、取杯 | 咖啡机、工作站位、杯子插槽 |
| 柜台取餐区 | 点单和领取 | 玩家站位、取餐点、交互触发器 |
| 座位区 | 多人交谈 | 两张桌子、不同参与者的位置 |
| 菜单区 | 看菜单和推荐 | 菜单牌与阅读站位 |
| 讲解区 | 续谈和小活动 | 老板娘讲解站位、2～4 个参与站位 |

### 3.2 锚点是“站位”，不能是物体模型中心

稳定语义 ID 延续 v0.3：`entrance`、`counter`、`coffee_machine`、`menu_board`、`table_1`、`table_2`；补充 `demo_spot` 与 `pickup_spot`。

每个设施包含：

```text
设施根节点：entity_id、display_name、available、supported_actions
├─ Visual                    可见模型
├─ StaticBody3D              必要的实体碰撞
├─ WorkPoint (Marker3D)      NPC 应站到哪里
├─ LookPoint (Marker3D)      NPC 应看向哪里
├─ PropSocket (Marker3D)     杯子等道具放在哪里
└─ InteractionArea (Area3D)  玩家在哪个范围可以交互
```

后端发 `anchor_id="coffee_machine"`，Godot 解析到其 `WorkPoint.global_position`，而不是让 NPC 走到咖啡机内部。

以下为白盒起始坐标，可以按模型尺寸调整；**实际坐标由 Godot 注册上报，不在后端再维护一份**。

| 语义锚点 | WorkPoint 示例 `(x,y,z)` | 说明 |
| --- | --- | --- |
| `entrance` | `(0,0,4.3)` | 门内欢迎位置；门外出生点放在 `(0,0,6)` |
| `counter` | `(0,0,-2.8)` | NPC 位于柜台后方 |
| `coffee_machine` | `(-1.8,0,-2.8)` | 朝向位于柜台上的设备 |
| `pickup_spot` | `(0,0,-0.7)` | 玩家在柜台前侧领取 |
| `menu_board` | `(-5.2,0,2.8)` | 留出阅读和转身空间 |
| `table_1` | `(-3.5,0,2.4)` | 桌旁可站位置，不是桌子中心 |
| `table_2` | `(3.5,0,2.4)` | 同上 |
| `demo_spot` | `(4.6,0,-2.6)` | 柜台右侧讲解位置 |

柜台实体可放在 `(0,0.5,-1.7)`，约 6×1×0.8 米；取餐杯位在柜台上表面，例如 `(0,1.05,-1.7)`。工作区与客区通过左右两侧通道连接。主要通道建议留 1.2 米以上净宽，最终通过角色胶囊体实际行走验证。

### 3.3 碰撞与导航分层

| 物理层 | 内容 | 用途 |
| --- | --- | --- |
| Layer 1 | 墙、地面、柜台、桌子 | 玩家和 NPC 的实体阻挡、视线射线 |
| Layer 2 | 玩家胶囊体 | 区域检测、交互目标 |
| Layer 3 | NPC 胶囊体 | 区域检测、身体定位 |
| Layer 4 | 交互 Area | 设施触发和选择 |

首版可让人物只与 Layer 1 碰撞，人物之间暂不做实体互推；交互和感知仍通过 Layer 2、3 检测。这样不会因测试配角挡住窄路而掩盖 Harness 功能。需要人物避让时，再单独加入导航避让。

把墙、柜台和桌子的阻挡几何纳入导航烘焙。导航不会自动把所有实时碰撞体当成不可走区域；路径计算主要依赖导航网格，必须在烘焙数据中表达障碍和人物尺寸。[R06]

---

## 4. 节点树与模块接线

### 4.1 主场景

```text
CafeTest (Node)
├─ World (Node3D)
│  ├─ WorldEnvironment
│  ├─ Lights (Node3D)
│  ├─ NavigationRegion3D
│  │  └─ StaticGeometry (Node3D)
│  │     ├─ Floor
│  │     ├─ Walls
│  │     ├─ Counter
│  │     └─ Tables
│  ├─ Stations (Node3D)
│  │  ├─ CoffeeStation
│  │  ├─ MenuBoard
│  │  └─ DemoStation
│  ├─ Anchors (Node3D)
│  ├─ Actors (Node3D)
│  │  ├─ CafeOwner
│  │  ├─ PlayerA
│  │  ├─ PlayerB
│  │  └─ ...
│  ├─ Props (Node3D)
│  ├─ CafeZone (Area3D)
│  └─ CameraRig (Node3D)
│     └─ YawPivot
│        └─ PitchPivot
│           └─ SpringArm3D
│              └─ Camera3D
├─ Services (Node)
│  ├─ HarnessClient
│  ├─ EntityRegistry
│  ├─ EventBridge
│  ├─ ObservationExporter
│  ├─ WorldActionDriver
│  └─ TestDirector
└─ HUD (CanvasLayer)
   └─ Root (Control)
      ├─ SessionBar
      ├─ ChatPanel
      ├─ InteractionPanel
      ├─ OrderActivityPanel
      ├─ MemoryPanel
      ├─ NpcDebugPanel
      └─ ConnectionStatus
```

Stations 的阻挡实体如果不在 `NavigationRegion3D` 子树内，需通过导航烘焙的几何分组配置加入；不要只烘焙 Floor。[R06]

### 4.2 NPC 场景

```text
CafeOwner (CharacterBody3D，根节点位于脚底)
├─ CollisionShape3D          胶囊体中心向上偏移
├─ NavigationAgent3D
├─ VisualRoot (Node3D)
│  └─ ImportedCharacter     实例化的 .glb 或包装场景
│     ├─ Skeleton3D
│     │  ├─ LookAtModifier3D
│     │  ├─ HeadAttachment (BoneAttachment3D)
│     │  │  └─ EyePoint (Marker3D)
│     │  └─ HandAttachment (BoneAttachment3D)
│     │     └─ CupSocket (Marker3D)
│     ├─ Meshes...
│     └─ AnimationPlayer
├─ AnimationTree
├─ AttentionTarget (Marker3D)
├─ SpeechAnchor (Marker3D)
├─ NpcSenses (Node3D)
├─ NpcGaze (Node)
└─ NpcAnimation (Node)
```

这是逻辑结构；导入模型的实际骨架与 `AnimationPlayer` 路径可能不同。包装场景保留导入实例，通过导出的 `NodePath` 或节点引用绑定，不要求改写导入文件的节点命名。

`CupSocket` 跟随手部骨骼；`EyePoint` 跟随头部，校正其局部朝向用于视觉采样；`AttentionTarget` 独立于被修改的头骨骼。避免把注视目标放到头骨骼自己的子节点下，形成互相追逐。

### 4.3 信号连接约定

```text
ChatPanel.utterance_submitted        → EventBridge.send_utterance
InteractionPanel.choice_submitted   → EventBridge.send_interaction
CafeZone.body_entered/exited        → EventBridge.visit_event
ObservationExporter.sample_ready   → HarnessClient.send_message
HarnessClient.message_received     → EventBridge.route_message
WorldActionDriver.feedback         → EventBridge.send_action_feedback
SpeechPresenter.feedback           → EventBridge.send_speech_feedback
后端 runtime_update                 → NpcDebugPanel.render_state
后端 memory_view                    → MemoryPanel.render_memory
```

网络回调只路由消息；物理移动由 `_physics_process` 推进，不在 WebSocket 收包函数里循环移动到目标。

---

## 5. 玩家、相机和测试身份

### 5.1 玩家控制

玩家使用 `CharacterBody3D`，胶囊碰撞、重力和相机相对移动。首版不做跳跃、攀爬、复杂交互姿势，避免把测试重点变成角色操控系统。

| 输入 | 行为 |
| --- | --- |
| WASD | 控制当前测试玩家行走 |
| Shift | 快速移动，便于切换测试位置 |
| 鼠标右键拖动 | 旋转第三人称镜头 |
| E | 与当前选中且在范围内的设施／物品交互 |
| Enter | 打开聊天输入；输入框内 Enter 发送 |
| Esc | 关闭当前输入／面板，不默认结束整个访问 |
| Tab | 切换说话目标：老板娘／指定玩家／公共话题 |
| Ctrl+1～8 | 切换当前受控玩家，不触发离店／进店 |
| F1 | 显示运行状态面板 |
| F2 | 第三人称与俯视测试镜头切换 |
| F3 | 显示感知区域、锚点和导航调试 |

输入框有焦点时，屏蔽人物 WASD 和交互快捷键。聊天提交只处理一次，不让同一个 Enter 又发送文字又重新打开输入。中文输入、文本换行和不同窗口尺寸必须实际试用。

`CharacterBody3D.velocity` 是速度，不是每帧位移；调用 `move_and_slide()` 时不再乘一次 `delta`，它在物理步中处理运动。[R04]

### 5.2 相机

第三人称相机跟随当前玩家，不因 NPC 回复而强制切换过场。用 `SpringArm3D` 处理镜头与墙体之间的距离变化；相机碰撞只检测场景阻挡，排除受控玩家或仅使用环境碰撞层。[R09]

保留俯视镜头用于检查寻路和多人站位。**调试镜头转向不改变 NPC 的感知方向**，老板娘的感知依据自身位置与朝向，而不是当前 Camera3D。

### 5.3 稳定身份与真实访问

固定测试身份建议为 `p_A`、`p_B`、`p_C` 等。`display_name` 可更改，但 `player_id` 不随场景重建随机生成。

进入／离开由覆盖室内的 `CafeZone` 判断，不用“是否看得见玩家”判断。`Area3D` 可以通过 `body_entered`、`body_exited` 检测物理对象出入，检测需要正确配置 monitoring 和碰撞 mask。[R08]

为避免门口来回抖动，离开后可等待约 0.5 秒、确认仍在区域外再结束访问。角色暂时躲到柱子后面不算离店。

`visit_id` 由后端在真实进入时生成；相机切换、身份切换不改变它。正常离店后，后端保存话题进度；再次进店触发旧约定查询。这是记忆互动功能，不是历史重放。

### 5.4 脚本配角

`TestDirector` 只驱动玩家输入，例如让 B 走到第二桌、说一句问题、拒绝邀请。配角本身不另接一个聊天模型。

脚本应等待真实条件，如“邀请卡出现后接受”“A 的制饮进入 BREW 后提问”，不要使用固定 3 秒、5 秒来假定模型已完成。脚本不得直接把后台目标改成成功，也不得替老板娘生成回应。

---

## 6. 环境感知：Godot 提供几何事实，后端理解局面

### 6.1 必须上报的信息

| 类别 | 必须上报 | 不在前端推断 |
| --- | --- | --- |
| 场景实体 | ID、位置、朝向、区域、设施可用性、杯子所在位置 | 玩家内心、性格、真实意图 |
| NPC 观察 | 可见对象、可听事件、距离、最后观察时间 | 该优先回应谁、应否改变战略 |
| 玩家交流 | 说话者、指定对象、内容、topic、reply_to | 自动认定玩家接受某个活动 |
| 身体执行 | 当前动作、占用通道、开始／暂停／完成 | 任务总目标已经完成 |
| 访问状态 | 进入、离开、当前在场人员 | 离开视线等于结束关系 |

### 6.2 起始感知规则

视觉先使用距离与视角：例如半径 8 米、水平视野 120°。听取使用独立规则：本地发言按 6 米距离，`room` 公共频道按当前咖啡馆在场关系。

**背对说话者不等于听不见。** 视觉和听觉不能共用一个 `visible_players` 列表。

需要遮挡测试时，追加从 `EyePoint` 到对方头部的射线。射线只检查墙、柜台等静态环境层；物理空间查询应在物理更新路径中执行。[R10] 首个白盒阶段可先关闭遮挡，只验证距离和朝向，再开启。

概念算法：

```text
can_see = 距离满足 AND 视角满足 AND 射线未被环境阻挡
can_hear_local = 事件是发言 AND 发言时距离满足
can_hear_room = 事件是公共频道 AND 双方处于当前房间
```

视角用已校正的 EyePoint 正前方与目标方向的点积计算，不使用玩家相机；尚未接骨架的白盒阶段可先用 VisualRoot 正前方。`look_at` 先实现转向或头部注视；感知方向也应随实际选定的感知朝向更新。

### 6.3 完整世界快照与角色观察分开

Godot 可以发送完整的小房间 `world_snapshot` 供动作条件和开发面板使用，同时发送 `perception` 结果，其中可以附上 `eye_position` 和 `eye_forward` 方便检查头部感知方向。

Python `PerceptionSystem` 依据 `perception` 构建角色 `world_view`，不能直接把完整快照全部塞进模型。因此，“当前屏幕能看到其他角落”并不会让老板娘全知。

观察不到的移动对象不继续更新其“最后已知位置”。对自己店内设施的静态位置，可以作为已知场景布局；设备突然故障若角色尚未观察或收到通知，不应提前在台词中宣布。

### 6.4 事件变化不等于必须说话

快照每秒数次，只用于更新状态。新增人物、进入近距交流、收到发言、动作完成、设施可用性变化才产生需要后端进一步判断的语义事件。

Python 原有 `SituationBuilder` 继续识别新客、对话组、待答问题和互动机会；前端不再复制一套同名战略和情境模型。

---

## 7. 寻路与站位：角色必须真正走到合适的位置

### 7.1 导航配置

使用 `NavigationRegion3D + NavigationMesh + NavigationAgent3D + CharacterBody3D`。

烘焙的起始参数可以为：人物碰撞半径 0.3 米，导航烘焙半径约 0.35～0.4 米，人物高度约 1.7～1.8 米。具体数值按角色实际碰撞体调整。

**`NavigationAgent3D.radius` 用于避让，并不会自动改变寻路所需的人物体积；寻路宽度要在 `NavigationMesh.agent_radius` 等烘焙参数中体现。**[R05][R06]

`NavigationAgent3D` 给出路径，不自动移动父节点。移动器在有效移动任务期间，每个物理步调用一次 `get_next_path_position()`，计算速度，再由 `CharacterBody3D` 移动。[R05]

场景开始后先等待导航地图首次同步，再允许发出移动动作。可以检查 `NavigationServer3D.map_get_iteration_id(map_rid)` 是否非零，不能把节点 `_ready()` 当成导航数据一定就绪。[R07]

### 7.2 到达判定

到达建议要求：接近目标站位，处于正确高度，并按需要转到目标朝向。不能仅靠“路径结束”宣布到达；`navigation_finished` 也可能表示不可达目标的最后一个路径点已经走完。[R05]

对于平面咖啡馆，起始到达容差可取水平 0.2 米、垂直 0.4 米。靠近设备后再执行短程转向，不让人物长时间背对设备制作咖啡。

本地动作失败可用简单原因：`NO_ANCHOR`、`NO_PATH`、`BLOCKED`、`TARGET_LEFT`。返回给 Harness 后，由其解释、等待或调整目标。

### 7.3 动态目标与玩家站位

首版“走向玩家”不直接把终点设为玩家脚底。先从玩家周围选择一个可站点，保持约 1 米交流距离；也可以将玩家引导到已定义的 `demo_spot`、`pickup_spot`。

给多个参与者安排不同站位，不能让所有人争抢同一个 Marker3D。讲解点使用 `participant_1`、`participant_2` 等固定站位就足够。

### 7.4 避让的接入顺序

先完成静态寻路，`avoidance_enabled=false`。需要人物之间避让时，再开启 `NavigationAgent3D` 的 RVO，设置期望速度并通过 `velocity_computed` 获得调整后的速度。[R05]

RVO 不代替物理碰撞，也不自动替代导航烘焙。加入避让后仍保持**每个角色每个物理步只调用一次 `move_and_slide()`**，不要在 `_physics_process` 和信号回调里各移动一次。

---

## 8. 动作与动画：把身体分成可组合的通道

### 8.1 角色资源要求

最终功能演示至少需要一个有骨架的低模角色，或者一个可明确看出头、手、腿动作的程序化角色。胶囊占位可以用于第一阶段，但不能作为“已完成制饮、递放和注视”的最终演示。

建议模型统一导出 `.glb`。Godot 支持 glTF 等 3D 资源导入，官方也推荐 glTF 作为通用交换格式；不同角色与动作骨架不一致时，需要处理 BoneMap／骨架重定向，而不是仅重命名动画。[R15][R16]

保持模型根节点缩放一致、脚底与碰撞体对齐。首版使用原地行走动画，位移由移动器负责，避免根运动和脚本位移叠加。

### 8.2 最小动画清单

| 动画 | 类型 | 用途 |
| --- | --- | --- |
| `idle` | 循环 | 普通待机 |
| `walk` | 循环 | 由真实水平速度驱动 |
| `listen` | 循环或轻微姿态 | 等待玩家／聆听 |
| `talk_subtle` | 循环 | 轻量讲话，不强占双手 |
| `greet` | 单次 | 招呼 |
| `point` | 单次 | 指向菜单、器具或讲解目标 |
| `brew` | 循环 | 制作咖啡的手部工作 |
| `serve` | 单次 | 递放饮品 |
| `tidy` | 循环 | 整理柜台 |

资源名通过 `animation_bindings.tres` 映射到实际导入动画名。缺少片段时显示明确的开发提示，不默默把所有指令映射成同一个 `talk`。

### 8.3 AnimationTree 结构

建议使用一个 `AnimationTree` 驱动同一骨架：

```text
Locomotion（idle / walk，按速度混合）
      ↓
WorkBlend（上半身轨道过滤；brew / tidy / neutral）
      ↓
GestureOneShot（greet / point / serve，手臂等指定轨道）
      ↓
Output

骨骼姿态生成后：LookAtModifier3D 调整头部注视
面部：少量表情或 blend shape，避免与手部工作互相覆盖
```

`AnimationTree` 使用 `AnimationPlayer` 中的动画资源进行混合，不是另一套动画素材库。混合节点支持轨道过滤，适合将工作动作和行走组合。[R11]

切换动画时统一通过 `NpcAnimation` 调整 `AnimationTree` 参数；不要一边直接 `AnimationPlayer.play()`，一边又让激活的 `AnimationTree` 驱动同一骨架。

`AnimationNodeOneShot` 可以发起和中止单次动作。[R12] 工作循环的结束由本地动作执行器控制；不要等待循环动画“自然播放结束”。

### 8.4 通道竞争

| 通道 | 主控 | 规则 |
| --- | --- | --- |
| `movement` | NpcMotor | 同时只有一个移动目标 |
| `hands` | 工作／手势动作 | 制作、递杯、整理互斥 |
| `speech` | SpeechPresenter | 公共表现一次一条发言 |
| `gaze` | NpcGaze | 一个主要关注目标，可以与工作兼容 |
| `face` | NpcAnimation | 表情平滑切换 |

制作咖啡时收到 `npc_speech`：继续 `brew`，只改变视线、面部和轻微讲话，不用全身 `talk` 覆盖制作。挥手动作若占用正在工作的手，可延后或改为点头，不能同时挥手和握杯。

### 8.5 注视

`look_at` 的目标是玩家眼部或设施 `LookPoint`，不是玩家脚底。先平滑转头，必要时在移动停止且任务允许时再转身体。

Godot 4.7 提供 `LookAtModifier3D` 调整骨骼朝向，支持角度限制；使用多个父子骨骼 modifier 时顺序需要正确。[R13]

本版先使用单个头部 modifier，起始左右转头限制可取约 60°。不要求整套 IK。使用导入模型的真实头骨名称与 forward axis，不假定所有骨架同名、同方向。

移动时身体朝行进方向，工作时身体朝设备；这两种情况下，视线模块不能每帧把整个角色扭向另一个方向。目标离场则恢复中性关注。

---

## 9. 道具与原子动作：前端执行到哪一级

### 9.1 下发的是原子动作，不是整个角色策略

后端高层技能和前端命令的映射：

| 后端技能 | Godot 接收的内容 |
| --- | --- |
| `move_to` | `move_to_anchor` |
| `look_at` | `look_at_entity` |
| `continue_topic` | 适当的移动／指向命令 + `npc_speech` |
| `pause_topic` | 停止对应发言或相关动作；话题进度由后台保存 |
| `prepare_and_serve` | 移动 → 制备 → 端杯到柜台 → 放杯 → 等待领取 → 转移杯子 |
| `host_activity` | 活动卡片、参与选择、主持台词、轻量手势 |
| `tidy_counter` | 移动 → `tidy` |
| `wait` | 不下发新动作，维持允许的待机 |

### 9.2 Godot 最小命令集

| command | args 核心字段 | 占用通道 | 完成条件 |
| --- | --- | --- | --- |
| `move_to_anchor` | anchor_id | movement | 实际到达站位 |
| `look_at_entity` | target_id、hold_seconds | gaze | 朝向进入容差；持续注视按指定时长保持 |
| `gesture` | animation_key | hands 或相应轨道 | 单次片段结束或被明确中止 |
| `brew` | order_id、cup_id、station_id、recipe_id | hands、设备 | 场景制备完成，杯子存在且处于规定位置 |
| `place_cup` | cup_id、socket_id | hands | 杯子已放到指定插槽 |
| `collect_cup` | cup_id、player_id | 玩家携带槽 | 玩家在可领取范围，杯子转到该玩家 |
| `tidy` | station_id、duration | hands | 整理动作持续到结束 |

`duration` 表示本地动作时长配置，不由模型任意延长到无限。首版设备过程可使用 Godot 本地计时器与循环动作，关键是场景结果由实际执行器生成，Python 不另开一个计时器抢先完成。

### 9.3 制饮与领取的一条完整链路

```text
玩家点单并确认（Python 建立订单）
  → move_to_anchor(coffee_machine)
  → Godot 到达并回报
  → brew(order_id, cup_id)
  → Godot 播放制作，将同一杯子挂到 NPC 手部 CupSocket，回报 cup_ready
  → move_to_anchor(counter)，NPC 实际端杯走到取餐柜台对应站位
  → place_cup(cup_id, pickup_socket)
  → Godot 将杯子放到柜台取餐插槽，回报完成
  → Python 状态变为可领取，发“咖啡好了”的台词和领取提示
  → 玩家走近按 E，发送 interaction(kind=collect_cup)
  → Python 下发 collect_cup
  → Godot 检查取餐距离，将杯子可见地移到玩家 CarrySocket，回报持有人改变
  → Python 标记服务完成并记录共同经历
```

首版采用柜台领取，减少自由空间手对手交接的动作难度，但仍然是实际放杯、拿杯，不能只更新文字。

若玩家没走近或暂时离开，杯子留在柜台，订单等待，不把物体瞬移到远处玩家手中。

### 9.4 杯子实体

杯子先用普通 `Node3D + MeshInstance3D`，不做真实液体和复杂刚体投掷。保存 `cup_id`、`order_id`、`holder_id`、`socket_id`。

`BoneAttachment3D` 可让子物体跟随指定骨骼；用于老板娘手中的杯子。玩家没有完整手部骨架时，`CarrySocket` 可先是胸前或手侧的普通 Marker3D。[R14]

放杯时改变父节点和局部变换，或在两个插槽间做短程可见过渡。柜台／手／玩家三种状态使用同一个杯子节点，不分别创建三只杯子冒充转移。

### 9.5 暂停、继续和取消

`WorldActionDriver` 用内存字典保存当前请求及进度。暂停移动时停止水平速度、保留目的地；暂停整理时暂停本地进度；继续时从当前有效阶段推进。

取消只作用于指定 `request_id` 或对应的业务动作，不停止所有角色。已做好的杯子不会因取消动画而凭空变回原材料；原型可把它放到回收插槽，后台记录本次服务取消。

这只是正常交互的状态管理，不做持久命令日志、自动恢复或重放。

---

## 10. 前后端通信协议

### 10.1 连接与启动顺序

连接地址沿用 v0.3：

```text
ws://127.0.0.1:8000/ws/cafe
```

启动顺序：

1. Python 启动 Harness，并选择 `world_mode=godot`，停止原离散场景模拟器。
2. Godot 加载场景，注册实体与锚点，等待导航可用。
3. WebSocket 建立后发送 `scene_ready`。
4. Python 注册 ID 和能力，回复 `scene_accepted`。
5. Godot 发送初始快照，然后开放测试玩家进入、发言和交互。

`scene_accepted` 只是本次初始化完成，不是账号验证流程。连接出错时显示错误和手动重连按钮；本版不做自动断线恢复。

### 10.2 消息目录

| 方向 | type | 用途 |
| --- | --- | --- |
| Godot → Python | `scene_ready` | 场景实体、锚点、支持的原子动作 |
| Python → Godot | `scene_accepted` | 本次场景注册完成 |
| Godot → Python | `world_snapshot` | 真实位置、实体状态与感知采样 |
| Godot → Python | `player_entered` / `player_left` | 开始／结束一次访问 |
| Godot → Python | `utterance` | 玩家文字，延续 v0.3 字段 |
| Godot → Python | `interaction` | 接受、拒绝、暂停、点单、取杯等 |
| Python → Godot | `action_command` | 一个场景原子动作 |
| Python → Godot | `action_control` | 指定动作 pause／resume／cancel |
| Godot → Python | `action_feedback` | 执行状态与真实结果 |
| Python → Godot | `npc_speech` | 一段 NPC 发言及表现参数 |
| Python → Godot | `speech_control` | 指定发言停止 |
| Godot → Python | `speech_feedback` | 内容显示／展示结束／中断 |
| Python → Godot | `interaction_prompt` | 邀请、订单确认、活动选项 |
| Python → Godot | `runtime_update` | 目标、战略、会话、待办、动作和记忆引用 |
| Godot → Python | `memory_request` | 查询指定测试玩家的记忆 |
| Python → Godot | `memory_view` | 资料、经历、话题进度和未来约定 |

v0.3 的 `utterance` 不变；原本合在一起的 `npc_update` 拆成上述输出。**这些是本次新增的接口契约，不是现有后端已经支持的接口。**

### 10.3 场景注册示例

以下只截取咖啡机锚点；实际注册必须包含全部设施、锚点和道具插槽。设施项用 `entity_id`，站位用 `anchor_id`，放置点用 `socket_id`，三者不能混用。

```json
{
  "type": "scene_ready",
  "protocol": "cafe3d.v1",
  "room_id": "cafe_demo",
  "scene_id": "cafe_3d_01",
  "npc_id": "npc_owner",
  "character_id": "owner_01",
  "units": "meters",
  "entities": [
    {"entity_id": "machine_01", "kind": "coffee_station", "available": true},
    {"entity_id": "counter_01", "kind": "counter", "available": true}
  ],
  "sockets": [
    {"socket_id": "pickup_socket", "entity_id": "counter_01", "position": [0.0, 1.05, -1.7]}
  ],
  "supported_commands": [
    "move_to_anchor", "look_at_entity", "gesture",
    "brew", "place_cup", "collect_cup", "tidy"
  ],
  "anchors": [
    {
      "anchor_id": "coffee_machine",
      "entity_id": "machine_01",
      "work_position": [-1.8, 0.0, -2.8],
      "look_position": [-1.8, 1.3, -1.7],
      "available": true
    }
  ]
}
```

向量使用 JSON 数组 `[x,y,z]`，角度统一用弧度；ID 使用稳定字符串，不用 NodePath 或实例内存 ID 作为后端业务 ID。

### 10.4 世界快照示例

```json
{
  "type": "world_snapshot",
  "room_id": "cafe_demo",
  "snapshot_id": 42,
  "observed_ms": 12800,
  "entities": [
    {
      "entity_id": "npc_owner",
      "kind": "npc",
      "position": [-1.8, 0.0, -2.8],
      "forward": [0.0, 0.0, 1.0],
      "body": {"movement": "idle", "hands": "brewing"}
    },
    {
      "entity_id": "p_A",
      "kind": "player",
      "position": [0.0, 0.0, -0.7],
      "forward": [0.0, 0.0, -1.0],
      "inside_cafe": true
    }
  ],
  "perception": {
    "observer_id": "npc_owner",
    "visible_ids": ["p_A", "machine_01"],
    "audible_player_ids": ["p_A"],
    "sample_time_ms": 12800
  }
}
```

`observed_ms` 是 Godot 本次启动后的相对时间，用于观察顺序；Python 自己计算模型等待时间，不拿两个进程的相对时钟直接相减。

`audible_player_ids` 表示本地发言的可听范围，不等于这些玩家正在说话。`utterance` 仍单独发送。

### 10.5 发言与交互

```json
{
  "type": "utterance",
  "player_id": "p_A",
  "text": "继续上次的手冲吧",
  "target_id": "npc_owner",
  "topic_id": "pourover_intro",
  "reply_to": null,
  "channel": "local",
  "perceived_by": ["npc_owner"]
}
```

`perceived_by` 由当前感知规则计算；听不见时后台可以记录测试输入，但不应让 NPC 直接回答该句话。

```json
{
  "type": "interaction",
  "player_id": "p_A",
  "kind": "accept_invitation",
  "prompt_id": "invite_12",
  "conversation_id": "conv_A_pourover",
  "value": true
}
```

邀请必须携带具体对象与 `prompt_id`，按钮不能只发无上下文的 `yes`。暂停／继续绑定 `conversation_id`，取消饮品绑定 `order_id`，这些 ID 由后端提示和状态更新提供。

### 10.6 动作与回执

```json
{
  "type": "action_command",
  "request_id": "act_004",
  "parent_skill_id": "serve_order_01",
  "npc_id": "npc_owner",
  "command": "move_to_anchor",
  "args": {"anchor_id": "coffee_machine"}
}
```

```json
{
  "type": "action_feedback",
  "request_id": "act_004",
  "npc_id": "npc_owner",
  "status": "completed",
  "result": {
    "anchor_id": "coffee_machine",
    "position": [-1.8, 0.0, -2.8]
  }
}
```

动作状态取 `started / paused / resumed / completed / failed / cancelled`。反馈失败时增加 `reason`；完成消息只发一次。`request_id` 只用于当前运行期配对动作和结果，不建立可靠消息系统。

```json
{
  "type": "action_control",
  "request_id": "act_004",
  "operation": "pause"
}
```

未知动作明确返回 `failed + UNKNOWN_COMMAND`，不能静默忽略后让后台无限等待。

### 10.7 台词与表现

```json
{
  "type": "npc_speech",
  "speech_id": "speech_021",
  "npc_id": "npc_owner",
  "conversation_id": "conv_A_pourover",
  "segment_id": "pourover_pouring_01",
  "targets": ["p_A"],
  "text": "上次讲到了水温，这次我们接着说注水。",
  "emotion": "interested",
  "gaze_target": "p_A",
  "gesture": "point"
}
```

`gesture` 是可选轻量表现。如果双手在制作，不能强制覆盖工作；真正需要等待完成的独立指点动作使用 `action_command`。

前端返回 `speech_feedback`，状态取 `displayed / finished / interrupted`。它只能证明内容显示或表现结束，不能证明玩家理解、满意或接受活动。

---

## 11. 聊天、活动与记忆 UI

### 11.1 UI 布局

中央保留最大面积的 3D 场景。底部放最近聊天和输入；左上显示当前测试身份、在场人员和说话对象；右侧默认折叠调试面板；需要接受、拒绝和确认时才显示交互卡。

不把整个屏幕做成聊天软件，也不把调试字段做成 NPC 自己的台词。

| 面板 | 主要内容 |
| --- | --- |
| `ChatPanel` | 说话者、目标、文本、输入框、发送按钮 |
| `InteractionPanel` | 邀请接受／拒绝，话题暂停／继续，订单确认／取消 |
| `OrderActivityPanel` | 当前饮品阶段，活动轮次与选项 |
| `MemoryPanel` | 指定玩家的偏好、共同经历、未完话题、下次约定 |
| `NpcDebugPanel` | 观察、情境、战略、待答事项、会话与动作阶段、本轮记忆 |
| `ConnectionStatus` | 后端连接与模型当前状态；失败原因简短可见 |

### 11.2 文本显示与发言结束

首版直接完整显示一小段台词，不做模型 token 流式播报。完整文本进入 UI 且完成一次绘制后发送 `displayed`；再按一个短展示时间结束说话姿态，发送 `finished`。

`SpeechPresenter` 用 `speech_id` 跟踪当前展示。点击“停止讲话”或收到 `speech_control` 时停止该段表演，不影响无关的制作任务。

当全文已经显示，后台可把该段记为“已展示”；不能把它记为“玩家已理解”。下一段讲解可等待“继续”或自然语言请求，避免 NPC 自动连续输出长段内容。

以后加入打字机效果时，可用 `RichTextLabel.visible_characters`；需在全文显示后才发 `displayed`，中途停止上报已显示长度，而不是提前把整段记成已讲完。[R20]

### 11.3 多人对象

聊天消息显式显示“谁 → 谁”。切换当前测试身份只影响后续输入和角色控制，既有气泡、订单和邀请仍属于原玩家。

一次公共发言占用一个 speech 通道。另一位玩家的问题显示为待答事项，而不是在前端生成第二个老板娘来回答。

### 11.4 小活动

先做 v0.3 的“咖啡口味猜想”：后端给出题目与轮次，前端显示参与者和选项，每位参与玩家提交自己的答案，后端记录并返回结果与点评。

前端不能根据自己拿到的题目直接认定所有人已经作答。玩家退出、拒绝和等待状态都需要能在 UI 上操作并看见。

### 11.5 记忆交互入口

保持固定 `player_id`。界面至少提供：查看自己的记录、通过自然语言修改偏好、暂停当前话题、下次继续、取消某个提醒、忘记某段主题。

“今日不再邀请”是当前访问状态，“以后不要提醒”是约定变化，不能用同一个“清空记忆”按钮代替。

`MemoryPanel` 数据来自 Python SQLite 查询；`NpcDebugPanel.memory_used` 显示本轮 ContextBuilder 实际选择的内容。**前端本地缓存一段旧聊天，不等于已经实现跨会话记忆。**

正常离店时显示后端的“本次进度已保存”状态。再次进入必须触发真实的资料和承诺读取；重新启动游戏后可以开新店，但不删除原型后端的 SQLite 记忆库。

---

## 12. 前端最小状态与后台状态的关系

前端只维护执行与展示状态：

```text
FrontendState
├─ connection                 后端是否连接
├─ controlled_player_id       当前操作者控制谁
├─ entities                   当前 3D 实体和设施引用
├─ player_visits              后端返回的访问标识
├─ running_actions            request_id → 本地动作阶段
├─ speech                     当前 speech_id 和显示阶段
├─ interaction_prompts        待接受／拒绝的提示
└─ backend_view               调试用的后台目标、会话、记忆镜像
```

`backend_view` 只读。不能在 GDScript 里把 `goal.status` 改成 completed 来推动剧情。

前端局部动作转移：

```text
idle → running → completed
           ↕
         paused
           ↘ cancelled / failed
```

后台会话转移仍由 Python 决定：邀请 → 等待回应 → 开始 → 暂停／结束。前端负责提交真实按钮与展示回执。

动画状态又是另一层：`brew` 动画可循环，但本地 brew 动作在到达其结束条件后停止；后台服务技能收到该结果后才发出下一步 `place_cup`。

---

## 13. 关键 GDScript 实现示例

以下示例用于具体实现入口，不构成完整项目。节点引用需要按第 4 节场景树绑定；动作总路由、UI 和真实后端按第 10 节协议开发。没有在本环境中运行 Godot，所以应先在编辑器验证这些脚本，再做功能联调。

### 13.1 WebSocket 客户端

脚本挂在 `Services/HarnessClient`。`connect_to_url()` 非阻塞，必须持续 `poll()`，收到 OPEN 后才发送 JSON；文本接口使用 `send_text()`。[R03]

```gdscript
# scripts/network/harness_client.gd
class_name CafeHarnessClient
extends Node

signal link_opened
signal link_closed
signal message_received(message: Dictionary)
signal connection_error(detail: String)

@export var endpoint: String = "ws://127.0.0.1:8000/ws/cafe"
@export var auto_connect: bool = true

var _socket: WebSocketPeer = WebSocketPeer.new()
var _last_state: int = -1

func _ready() -> void:
    set_process(false)
    if auto_connect:
        connect_link()

func connect_link() -> void:
    if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
        connection_error.emit("已有连接正在运行")
        return
    _socket = WebSocketPeer.new()
    _last_state = -1
    var err: Error = _socket.connect_to_url(endpoint)
    if err != OK:
        connection_error.emit("无法开始连接，错误码：%s" % err)
        return
    set_process(true)

func _process(_delta: float) -> void:
    _socket.poll()
    var state: int = _socket.get_ready_state()
    if state != _last_state:
        _last_state = state
        if state == WebSocketPeer.STATE_OPEN:
            link_opened.emit()
        elif state == WebSocketPeer.STATE_CLOSED:
            link_closed.emit()
            set_process(false)

    if state != WebSocketPeer.STATE_OPEN:
        return

    while _socket.get_available_packet_count() > 0:
        var text: String = _socket.get_packet().get_string_from_utf8()
        var parser: JSON = JSON.new()
        if parser.parse(text) != OK:
            connection_error.emit("收到无法解析的 JSON")
            continue
        var data: Variant = parser.data
        if data is Dictionary:
            message_received.emit(data)
        else:
            connection_error.emit("消息根节点必须是 JSON 对象")

func send_message(message: Dictionary) -> Error:
    if _socket.get_ready_state() != WebSocketPeer.STATE_OPEN:
        return ERR_CONNECTION_ERROR
    return _socket.send_text(JSON.stringify(message))

func disconnect_link() -> void:
    if _socket.get_ready_state() == WebSocketPeer.STATE_OPEN:
        _socket.close(1000, "end test")
        # _process 继续 poll，直到连接关闭。
```

`link_opened` 只表示 WebSocket 建立，主场景仍要确认本地导航和注册数据已准备好，再发 `scene_ready`。不要在每一帧重复发注册消息。

### 13.2 设施锚点

```gdscript
# scripts/world/interaction_anchor.gd
class_name CafeInteractionAnchor
extends Node3D

@export var anchor_id: StringName
@export var entity_id: StringName
@export var display_name: String = ""
@export var available: bool = true
@export var work_point: Marker3D
@export var look_point: Marker3D

func _ready() -> void:
    assert(not String(anchor_id).is_empty(), "必须填写稳定 anchor_id")
    assert(work_point != null, "必须绑定 WorkPoint")
    add_to_group("cafe_anchors")

func as_dict() -> Dictionary:
    var work: Vector3 = work_point.global_position
    var gaze: Vector3 = global_position
    if look_point != null:
        gaze = look_point.global_position
    return {
        "anchor_id": String(anchor_id),
        "entity_id": String(entity_id),
        "display_name": display_name,
        "work_position": [work.x, work.y, work.z],
        "look_position": [gaze.x, gaze.y, gaze.z],
        "available": available
    }
```

`EntityRegistry` 在启动时收集 `cafe_anchors`，形成 `anchor_id → CafeInteractionAnchor`。接到移动命令时查这个表，不用模型生成节点路径。

### 13.3 最小 NPC 移动器

脚本挂在 `CafeOwner` 根节点，已有 `NavigationAgent3D` 与 `VisualRoot`。根节点位于脚底；地面平坦；这个示例不启用 RVO，展示一次移动、暂停、继续和结果反馈。[R04][R05][R07]

```gdscript
# scripts/actors/npc_motor.gd
class_name CafeNpcMotor
extends CharacterBody3D

signal motion_event(request_id: String, status: String, reason: String)

@export var walk_speed: float = 1.8
@export var gravity_acceleration: float = 9.8
@export var arrival_radius: float = 0.2
@export var height_tolerance: float = 0.4
@export var turn_rate: float = 8.0
@export var max_move_seconds: float = 20.0

@onready var navigation: NavigationAgent3D = $NavigationAgent3D
@onready var visual: Node3D = $VisualRoot

var _request_id: String = ""
var _goal: Vector3 = Vector3.ZERO
var _paused: bool = false
var _needs_target: bool = false
var _elapsed: float = 0.0

func _ready() -> void:
    navigation.avoidance_enabled = false
    navigation.path_desired_distance = 0.25
    navigation.target_desired_distance = arrival_radius

func start_move(request_id: String, world_target: Vector3) -> bool:
    if not _request_id.is_empty() or request_id.is_empty():
        return false
    _request_id = request_id
    _goal = world_target
    _paused = false
    _needs_target = true
    _elapsed = 0.0
    motion_event.emit(_request_id, "started", "")
    return true

func pause_move(request_id: String) -> void:
    if _request_id != request_id or _request_id.is_empty() or _paused:
        return
    _paused = true
    velocity.x = 0.0
    velocity.z = 0.0
    motion_event.emit(_request_id, "paused", "")

func resume_move(request_id: String) -> void:
    if _request_id != request_id or _request_id.is_empty() or not _paused:
        return
    _paused = false
    _needs_target = true
    motion_event.emit(_request_id, "resumed", "")

func cancel_move(request_id: String) -> void:
    if _request_id == request_id and not _request_id.is_empty():
        _finish("cancelled", "")

func _physics_process(delta: float) -> void:
    var desired: Vector3 = Vector3.ZERO
    if not is_on_floor():
        velocity.y -= gravity_acceleration * delta
    else:
        velocity.y = 0.0

    if not _request_id.is_empty() and not _paused:
        _elapsed += delta
        var nav_map: RID = navigation.get_navigation_map()
        var map_ready: bool = false
        if nav_map.is_valid():
            map_ready = NavigationServer3D.map_get_iteration_id(nav_map) > 0

        if _elapsed > max_move_seconds:
            _finish("failed", "MOVE_TIMEOUT")
        elif map_ready:
            if _needs_target:
                navigation.target_position = _goal
                _needs_target = false

            # 活动移动期间，每个物理步只查询一次下一路径点。
            var next: Vector3 = navigation.get_next_path_position()
            var flat_goal: Vector3 = _goal - global_position
            flat_goal.y = 0.0
            var close_enough: bool = flat_goal.length() <= arrival_radius
            var height_ok: bool = absf(_goal.y - global_position.y) <= height_tolerance

            if close_enough and height_ok:
                _finish("completed", "")
            elif navigation.is_navigation_finished():
                _finish("failed", "TARGET_UNREACHABLE")
            else:
                var offset: Vector3 = next - global_position
                offset.y = 0.0
                if offset.length() > 0.001:
                    var speed: float = minf(walk_speed, offset.length() / maxf(delta, 0.001))
                    desired = offset.normalized() * speed

    velocity.x = desired.x
    velocity.z = desired.z
    move_and_slide()

    var horizontal_velocity: Vector3 = Vector3(velocity.x, 0.0, velocity.z)
    if horizontal_velocity.length() > 0.05:
        var yaw: float = atan2(-horizontal_velocity.x, -horizontal_velocity.z)
        visual.rotation.y = lerp_angle(
            visual.rotation.y, yaw, minf(turn_rate * delta, 1.0)
        )

func _finish(status: String, reason: String) -> void:
    var finished_id: String = _request_id
    _request_id = ""
    _paused = false
    _needs_target = false
    velocity.x = 0.0
    velocity.z = 0.0
    motion_event.emit(finished_id, status, reason)
```

`WorldActionDriver` 把 `motion_event` 包装成第 10 节的 `action_feedback`，并附上此刻 `global_position`。开始新任务返回 false 时报告通道忙，不默默覆盖旧目的地。

示例只旋转 `VisualRoot`。观察导出中的 `forward` 因此应取 `-VisualRoot.global_basis.z`，而不是未旋转的 CharacterBody3D 根节点方向。模型的美术朝向修正放在更下一级子节点。

站位后的最终朝向和头部注视由 `NpcGaze` 完成。初期也可以让单独的转向命令旋转 `VisualRoot`，但它不能与移动器同时争抢身体旋转。

### 13.4 原子动作驱动器的流程

下面是逻辑伪代码，避免把尚未实现的动画与道具 API 冒充完整代码：

```text
收到 action_command：
  检查目标 NPC、设施和命令是否存在
  检查本地需要的身体通道是否空闲
  保存 request_id → 本地执行上下文

  move_to_anchor：查询 WorkPoint，调用 NpcMotor
  brew：启动工作动作和设备表现，逐帧累计本地制备进度
  place_cup：播放递放片段，在放置节点时刻转移杯子父节点
  collect_cup：检查玩家实际在取餐范围，把杯子移到 CarrySocket
  tidy：启动整理循环，到时停止
  gesture：触发单次动作，观察本次动作结束

动作推进：
  只更新自己的局部阶段
  产生杯子／位置变化时更新场景实体
  结束时释放通道，反馈结果

收到 action_control：
  只处理相同 request_id
  pause 保留本地阶段；resume 继续；cancel 停止后续步骤
```

制作、放杯的关键点可以选动画方法轨道，或者先用动作配置中的阶段时间。**同一个效果只采用一个触发来源**，不能计时器生成一只杯子、动画回调又生成一只。

Godot 本地技能应同时支持 `MockHarness` 和真实网络命令。Mock 只用于验证场景执行，进入真实模式后关闭，不让两套来源同时驱动 NPC。

---

## 14. Python 侧必须配套修改的最小内容

这不是单改 Godot 就能完成的 UI 替换。后端 v0.3 需要增加一个很薄的 Godot 适配层。

### 14.1 替换 WorldAdapter

```text
GodotWorldAdapter
  register_scene(manifest)
  update_world(snapshot)
  start_action(npc_id, command, args) -> request_id
  control_action(request_id, operation)
  handle_action_feedback(message) -> RuntimeEvent
```

`start_action` 仅表示命令已发出，不能直接返回成功。`SkillRunner` 保存正在等待的 `request_id`，收到 completed 后推进下一阶段。

取消原 Python 模拟器对 NPC 坐标和杯子实体的更新。保留 Python 对高层订单与技能阶段的管理。

### 14.2 映射事件

| Godot 消息 | 后端如何处理 |
| --- | --- |
| `player_entered` | 开新 visit，读取玩家资料、话题进度和 pending 承诺 |
| `player_left` | 结束本次访问，保存进度，触发经历摘要 |
| `world_snapshot` | 更新世界副本和观察，不自动触发每次模型调用 |
| `utterance` | 转成 v0.3 的 `player.utterance` |
| `interaction` | 转成接受、拒绝、暂停、订单等具体交互 |
| `action_feedback` | 交回 SkillRunner；某个原子动作完成不等于整个高层技能完成 |
| `speech_feedback` | 更新已显示段落和发言状态，不认定玩家已经理解 |
| `memory_request` | 查询现有 SQLite 资料，不引入第二个记忆存储 |

收到动作回执时先合并其结果中的位置、杯子状态，再让 SkillRunner 推进。不能要求关键结果必须等下一次快照才出现。

### 14.3 PresentationAdapter

把 v0.3 的 `npc_update` 拆分：

```text
publish_speech(...)        → npc_speech
publish_prompt(...)        → interaction_prompt
stop_speech(speech_id)     → speech_control
publish_runtime_view(...) → runtime_update
publish_memory(...)       → memory_view
```

动作执行不再藏在一个模糊 `animation` 字段里。`runtime_update` 的 activity 是后台状态镜像，不可再被前端当成新的动作命令重复执行。

### 14.4 模型 Prompt 的小幅调整

模型输入中的 `available_skills` 仍是高层技能，不是 Godot 节点列表。它可以选择 `continue_topic`、`prepare_and_serve`、`move_to`，无需理解 `NavigationAgent3D` 或骨骼名字。

将设施 ID、可用状态、当前站位和已完成结果加入上下文。仅当场景完成后再生成相应完成台词；模型返回期间角色继续正常运动和待机。

模型 API 和三组任务继续在 Python，Godot 不需要直接集成供应商 SDK。

---

## 15. 前端开发顺序与交付物

### 阶段 A：可走动的咖啡馆白盒

完成地面、墙、柜台、设施、导航、第三人称镜头、玩家移动、固定身份切换与 CafeZone。

**交付物：**`cafe_world.tscn`、`test_player.tscn`、锚点表、可用导航网格。A 从门外走进再离开，能明确看到进入／离开事件。

### 阶段 B：NPC 身体与原子动作

导入／制作简单角色，完成行走、看向对象、招呼、制作、递放、整理。使用本地按钮或 Mock 命令驱动同一个 `WorldActionDriver`。

**交付物：**`cafe_owner.tscn`、动画绑定资源、动作驱动器。能够真正绕过柜台、拿放同一只杯子，并且制作期间继续显示对话。

### 阶段 C：Godot 与 Harness 双向连接

实现 `scene_ready`、快照、玩家事件、动作指令和结果反馈；Python 启用 `world_mode=godot`。先让规则 Planner 下发动作，验证没有双重坐标更新。

**交付物：**WebSocket 接口、GodotWorldAdapter、可见的反馈闭环。后台只在实际到达后进入制备。

### 阶段 D：真实模型、多人会话与记忆

接入 v0.3 的真实事件模型、战略模型和经历摘要，完成邀请、拒绝、暂停、续谈、记忆修正及“下次再来”。完善 UI 的玩家／目标／话题显示。

**交付物：**三次到店流程、两人交错提问、多人活动。不能只用 Mock 或固定台词代替最终验证。

### 阶段 E：调整角色表现

调整转身、注视、动画过滤、杯子插槽、字幕时长和站位。修复“眼睛看 A、台词答 B”“手里有杯子仍挥手”等表现冲突。

**交付物：**桌面导出的可试玩测试场和下节功能用例结果。语音和高精美术留待这个版本之后，不阻塞功能完成。

---

## 16. 只验收主线：功能测试清单

| 编号 | 操作 | 场景必须出现 | 后台必须对应 |
| --- | --- | --- | --- |
| F01 | A 从门外走进 | NPC 感知更新，在合适时机欢迎 | 新访问和欢迎候选 |
| F02 | Ctrl 切换到 B | A 保留在场，控制对象变为 B | A 的 visit 不结束 |
| F03 | A、B 向不同对象发言 | 消息准确显示说话者与对象 | 待答和会话不串人 |
| F04 | NPC 走向咖啡机 | 实际绕开柜台，到工作站位 | 到达回执后才推进 |
| F05 | 制作期间 B 提问 | 手部制作继续，回应和注视转向 B | A 的服务任务仍在 |
| F06 | A 说“等一下再拿” | 杯子留在柜台，不飞向玩家 | 交付阶段等待 |
| F07 | A 走近按 E 领取 | 同一个杯子进入 A 携带槽 | 服务完成，记入实际经历 |
| F08 | 接受话题邀请 | 接受前等待，接受后开始讲解 | 正确会话阶段转移 |
| F09 | 拒绝活动 | 不重复催促，恢复适当待机／店务 | 调整当次目标 |
| F10 | 讲解中暂停 | 只停相应话题和表现，不停无关店务 | 保存已显示进度 |
| F11 | A 正常离店并再次进入 | 稳定身份，能够接上旧话题 | 读取 SQLite 进度和约定 |
| F12 | A 修改口味 | 下一次建议使用新偏好 | profile 实际更新 |
| F13 | 测试“下次再来提醒” | 返回后出现续谈机会 | on_next_visit 被条件触发 |
| F14 | 角色背向本地发言者 | 在可听范围仍能收到发言 | 听觉不依赖视觉列表 |
| F15 | 启用遮挡，玩家走到墙后 | 可见列表变化，不把出视野当离店 | 最后观察与当前观察区分 |
| F16 | 把目标站位设在不可达区域 | 不瞬移，不报已到达 | 返回失败，允许重新安排 |
| F17 | 没有新问题 | NPC 可整理、巡视或静待 | 保持自己的目标，不高频调用模型 |
| F18 | 多人活动各自提交答案 | 显示参与者与轮次，逐人收集 | 真实活动进度和共同经历 |

### 16.1 核心用例：三次到店

**第一次：**A 听手冲介绍，NPC 走到 `demo_spot`、看向器具并指点。水温段完整显示后，A 选择“下次继续”，走出 CafeZone；后台保存已显示段落和未来约定。

**第二次：**A 与 B 一起进店。A 表示“今天先陪朋友”，NPC 暂缓续谈目标，转而接待 B。A 仍是熟客，但不被强行拉回原话题。

**第三次：**A 再次进店并说“接着上次那个”。NPC 读取真实进度，从注水部分继续，表现动作与本轮内容对应，不从头重讲。

要同时观察 3D 行动、目标变化、会话状态与后台 `memory_used`。这不是把所有历史塞进一次超长 Prompt 后表演出来。

### 16.2 核心用例：做事时被打断

A 确认饮品；NPC 移动、制作。B 在制作期间提问；NPC 不停手地简短回应。A 暂缓领取；NPC 放杯并继续其他工作。A 回到取餐点领取；场景发生物品转移，后台完成原服务。

这条流程检验的是多个通道、多个玩家和持续任务，并非动画越多越好。

---

## 17. 常见实现错误与直接修正

| 错误 | 会看到的现象 | 直接修正 |
| --- | --- | --- |
| Python 和 Godot 都计算位置 | 角色抖动、回拉、位置判断错误 | Godot 是唯一空间模拟器 |
| 后端高层技能和前端各自跑完整流程 | 一次点单做两杯，状态提前完成 | 后端编排，前端只跑原子动作 |
| `move_and_slide` 速度再乘 delta | 行走很慢或帧率相关 | 使用米／秒速度，物理步调用 |
| 工作站位放在设备内部 | 走不到，穿模，背对设备 | WorkPoint 与模型中心分离 |
| 只烘焙地板、不包含家具 | 路线穿过桌子，被碰撞卡住 | 调整导航源几何并重新烘焙 |
| 导航未同步立即寻路 | 第一条移动失败 | 等导航 iteration 非零 |
| 把 navigation_finished 当成功 | 不可达位置也报已到达 | 额外核验真实距离和目标高度 |
| 全身 talk 覆盖 brew | 回答时手里工作突然停止 | AnimationTree 轨道过滤和通道控制 |
| 当前相机决定 NPC 可见对象 | 切换镜头后 NPC 认知改变 | 感知用 NPC 自身朝向 |
| 换角色控制就结束访问 | 第二次聊天被当作新客 | visit 与控制身份独立 |
| 本地保存聊天冒充记忆系统 | 重启或换访问后无法正确续谈 | SQLite 仍在 Python，前端查实际结果 |
| 收到 npc_update 就执行一次动作 | 面板刷新使动作不停重启 | 状态更新只读，动作由 action_command 驱动 |
| UI 一收到模型文字就算讲完 | 停止显示后后台仍跳到下一段 | 以 speech_feedback 推进展示进度 |
| 用 sleep 假定模型已回复 | 演示时序错乱 | 脚本等待真实提示与状态条件 |

---

## 18. 最终交付清单

开发完成时应当得到一个可从 Godot 编辑器或桌面导出程序运行的工程，而不是仅有设计图。

**工程内容：**3D 咖啡馆、NPC 与玩家场景、导航与锚点、基础动作、WebSocket、聊天和交互 UI、测试身份与脚本配角、功能调试面板。

**后端配套：**GodotWorldAdapter、拆分后的表现消息、动作回执到 SkillRunner 的映射，复用原有战略、事件模型和 SQLite 记忆。

**运行说明：**固定 Godot 版本、项目入口、后端启动方式、连接地址、Input Map、模型资源绑定方式和三次到店演示步骤。具体启动命令以真实后端入口为准，不能把当前文档当作已经存在的服务。

**不作为本版交付：**真实多客户端网络同步、复杂账号、事件重放、崩溃恢复、安全平台、语音模型和美术精修。

**推荐第一项开发任务：搭好 `CafeWorld + CafeOwner + 两个 TestPlayer + 锚点和导航`，用同一个 WorldActionDriver 跑通“走向咖啡机—制作—放杯—玩家领取”。第二条立即加入“邀请—暂停—下次到店继续”，再把规则输入替换为真实 Harness 和模型。**

---

## 资料来源与核验边界

项目需求依据：本次讨论及《咖啡馆NPC_Harness与大模型接入_主线功能方案_v0.3.md》，尤其是第 3～8 节的感知、战略、事件、状态、记忆和动作要求，以及第 12、14 节的前端与验收。本方案替换其中的网页 2D 场景，不改变主线功能范围。

以下均为 Godot 官方资料，访问日期 2026-09-30。它们用于核验版本、节点与 API；场景尺寸、协议名称、模块划分和开发顺序是本文的设计，不是官方强制架构。

- [R01] [Godot 4.7 stable 归档](https://godotengine.org/download/archive/4.7-stable/)：版本发布日期。
- [R02] [Godot 4.7.2 stable 归档](https://godotengine.org/download/archive/4.7.2-stable/)：建议固定的维护版本。
- [R03] [WebSocketPeer](https://docs.godotengine.org/en/4.7/classes/class_websocketpeer.html)：连接、poll、文本帧。
- [R04] [CharacterBody3D](https://docs.godotengine.org/en/4.7/classes/class_characterbody3d.html)：velocity、物理步与 move_and_slide。
- [R05] [NavigationAgent3D](https://docs.godotengine.org/en/4.7/classes/class_navigationagent3d.html)：路径更新、到达、避让和半径含义。
- [R06] [Using navigation meshes](https://docs.godotengine.org/en/4.7/tutorials/navigation/navigation_using_navigationmeshes.html)：导航烘焙与几何边界。
- [R07] [NavigationServer3D](https://docs.godotengine.org/en/4.7/classes/class_navigationserver3d.html)：导航地图同步状态。
- [R08] [Area3D](https://docs.godotengine.org/en/4.7/classes/class_area3d.html)：区域进入、离开与检测层。
- [R09] [SpringArm3D](https://docs.godotengine.org/en/4.7/classes/class_springarm3d.html)：第三人称镜头碰撞。
- [R10] [Ray-casting](https://docs.godotengine.org/en/4.7/tutorials/physics/ray-casting.html)：物理空间射线查询。
- [R11] [Using AnimationTree](https://docs.godotengine.org/en/4.7/tutorials/animation/animation_tree.html)：动画来源、混合和轨道过滤。
- [R12] [AnimationNodeOneShot](https://docs.godotengine.org/en/4.7/classes/class_animationnodeoneshot.html)：单次动作控制。
- [R13] [LookAtModifier3D](https://docs.godotengine.org/en/4.7/classes/class_lookatmodifier3d.html)：骨骼注视与角度限制。
- [R14] [BoneAttachment3D](https://docs.godotengine.org/en/4.7/classes/class_boneattachment3d.html)：手部道具挂接。
- [R15] [Available 3D formats](https://docs.godotengine.org/en/4.7/tutorials/assets_pipeline/importing_3d_scenes/available_formats.html)：glTF／GLB 导入。
- [R16] [Retargeting 3D Skeletons](https://docs.godotengine.org/en/4.7/tutorials/assets_pipeline/retargeting_3d_skeletons.html)：骨架重定向。
- [R17] [High-level multiplayer](https://docs.godotengine.org/en/4.7/tutorials/networking/high_level_multiplayer.html)：Godot 高层网络和 RPC 的使用范围。
- [R18] [Overview of renderers](https://docs.godotengine.org/en/4.7/tutorials/rendering/renderers.html)：渲染器选择。
- [R19] [Node3D](https://docs.godotengine.org/en/4.7/classes/class_node3d.html)：3D 坐标和节点变换。
- [R20] [RichTextLabel](https://docs.godotengine.org/en/4.7/classes/class_richtextlabel.html)：完整文本和逐字显示。
