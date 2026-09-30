"""由 Harness 唯一入口调用；完整服务流程只根据 Godot 完成回执推进。"""

from .schemas import RECIPES


class Orders:
    def __init__(self, runtime):
        self.r = runtime
        self.items: dict[str, dict] = {}
        self.active_id: str | None = None

    async def propose(self, player_id: str, recipe_id: str) -> None:
        if recipe_id not in RECIPES:
            raise ValueError("饮品必须为 americano/latte/pourover/decaf")
        if any(o["player_id"] == player_id and o["status"] not in {"completed", "cancelled", "failed"} for o in self.items.values()):
            raise ValueError("该玩家已有未完成订单，请先处理现有订单")
        order_id = self.r.new_id("order")
        order = {"order_id": order_id, "player_id": player_id, "recipe_id": recipe_id,
                 "cup_id": "cup_" + order_id, "status": "confirming", "stage": "CONFIRM_ORDER",
                 "request_id": None, "last_result": {}, "reason": "", "cup_exists": False,
                 "cleanup_pending": False, "cleanup_failed": False, "holder_id": "", "socket_id": "",
                 "preferences": self.r.memory.view(player_id)["profile"]["preferences"]}
        details = "，".join(f"{key}: {value}" for key, value in order["preferences"].items() if key in {"sweetness", "milk"})
        confirmation = f"确认点一杯{RECIPES[recipe_id]}" + (f"（{details}）" if details else "") + "？"
        self.items[order_id] = order
        self.r.trace_link(order_id)
        prompt = await self.r.prompt(player_id, "order_confirmation", confirmation,
                                   [{"label": "确认点单", "kind": "confirm_order", "value": True},
                                    {"label": "取消", "kind": "cancel_order", "value": False}], order_id=order_id)
        order["prompt_id"] = prompt["prompt_id"]

    def owned(self, player_id: str, order_id: str) -> dict:
        order = self.items.get(order_id)
        if not order or order["player_id"] != player_id:
            raise ValueError("订单不存在或不属于当前玩家")
        return order

    async def interact(self, player_id: str, kind: str, message: dict) -> None:
        order = self.owned(player_id, message.get("order_id", ""))
        if kind == "confirm_order":
            if order["status"] != "confirming" or message.get("prompt_id") != order["prompt_id"]:
                raise ValueError("点单确认已过期或缺少正确 prompt_id")
            self.r.dismiss_prompt(order["prompt_id"])
            order["preferences"] = self.r.memory.view(player_id)["profile"]["preferences"]
            order["status"] = "queued"
            order["stage"] = "MOVE_TO_MACHINE"
            self.r.record(player_id, "confirmed_order", recipe_id=order["recipe_id"], order_id=order["order_id"])
            self.r.strategy_dirty = True
            await self.pump()
        elif kind == "cancel_order":
            if order["status"] in {"completed", "cancelled"}:
                raise ValueError("订单已经结束")
            order["status"] = "cancelled"
            order["stage"] = "CANCELLED"
            order["cleanup_pending"] = order["cup_exists"]
            self.r.dismiss_related_prompts("order_id", order["order_id"])
            if order["request_id"]:
                await self.r.control(order["request_id"], "cancel")
            if self.active_id == order["order_id"]:
                # Body is not released until the real cancellation feedback.
                if not order["request_id"]:
                    self.active_id = None
            self.r.record(player_id, "cancelled_order", order_id=order["order_id"])
            self.r.strategy_dirty = True
            await self.pump()
        elif kind == "pause_order":
            if order["status"] not in {"running", "queued", "ready"}:
                raise ValueError("当前订单不能暂停")
            order["resume_status"] = order["status"]
            order["status"] = "paused"
            self.r.strategy_dirty = True
            if order["request_id"]:
                await self.r.control(order["request_id"], "pause")
            else:
                self.r.record(player_id, "order_paused", order_id=order["order_id"], stage=order["stage"],
                              from_status=order["resume_status"], source="harness_state")
        elif kind == "resume_order":
            if order["status"] != "paused":
                raise ValueError("当前订单没有暂停")
            order["status"] = order.pop("resume_status", "queued")
            self.r.strategy_dirty = True
            if order["request_id"]:
                await self.r.control(order["request_id"], "resume")
            else:
                self.r.record(player_id, "order_resumed", order_id=order["order_id"], stage=order["stage"],
                              to_status=order["status"], source="harness_state")
                await self.pump()
        elif kind == "collect_cup":
            if order["status"] != "ready":
                raise ValueError("饮品尚未实际放到取餐柜台")
            order["stage"] = "COLLECT"
            order["status"] = "queued"
            await self.pump()
        elif kind == "retry_cleanup":
            if not order["cleanup_failed"]:
                raise ValueError("此订单没有失败的回收动作")
            order["cleanup_failed"] = False
            order["cleanup_pending"] = True
            await self.pump()

    async def pump(self) -> None:
        if self.active_id or self.r.body_action:
            return
        for order in self.items.values():
            if order["cleanup_pending"] and not order["cleanup_failed"]:
                self.active_id = order["order_id"]
                order["stage"] = "RECYCLE_MOVE" if order["holder_id"] == "npc_owner" else "RECYCLE_PICKUP_MOVE"
                await self.start_stage(order)
                return
        # There is one real pickup socket and one NPC hand socket; never overwrite either.
        occupied = any(o["cup_exists"] and o["status"] != "completed" for o in self.items.values())
        for order in self.items.values():
            if order["status"] != "queued" or order["player_id"] not in self.r.players:
                continue
            if occupied and order["stage"] == "MOVE_TO_MACHINE":
                continue
            self.active_id = order["order_id"]
            order["status"] = "running"
            await self.start_stage(order)
            return

    async def start_stage(self, order: dict) -> None:
        # Pump may choose a different order from the one that triggered it.
        with self.r.trace_scope(*self.r.trace_origin(order["order_id"])):
            await self._start_stage(order)

    async def _start_stage(self, order: dict) -> None:
        stage = order["stage"]
        commands = {
            "MOVE_TO_MACHINE": ("move_to_anchor", {"anchor_id": "coffee_machine"}),
            "BREW": ("brew", {"order_id": order["order_id"], "cup_id": order["cup_id"], "station_id": "machine_01", "recipe_id": order["recipe_id"], "preferences": order["preferences"]}),
            "MOVE_TO_COUNTER": ("move_to_anchor", {"anchor_id": "counter"}),
            "PLACE": ("place_cup", {"cup_id": order["cup_id"], "socket_id": "pickup_socket"}),
            "COLLECT": ("collect_cup", {"cup_id": order["cup_id"], "player_id": order["player_id"]}),
            "RECYCLE_PICKUP_MOVE": ("move_to_anchor", {"anchor_id": "counter"}),
            "RECYCLE_RETRIEVE": ("retrieve_cup", {"cup_id": order["cup_id"]}),
            "RECYCLE_MOVE": ("move_to_anchor", {"anchor_id": "recycle_spot"}),
            "RECYCLE_PLACE": ("place_cup", {"cup_id": order["cup_id"], "socket_id": "recycle_socket"}),
        }
        command, args = commands[stage]
        try:
            order["request_id"] = await self.r.action(command, args, order["order_id"], "order")
            self.r.flow_step("apply", "订单按当前阶段下发单步动作，等待实际回执", "waiting_feedback",
                             {"order_id": order["order_id"], "player_id": order["player_id"], "order_stage": stage,
                              "order_status": order["status"], "request_id": order["request_id"], "command": command, "args": args})
        except ValueError as exc:
            if order["cleanup_pending"]:
                order.update(cleanup_failed=True, reason=str(exc), request_id=None)
            else:
                order.update(status="failed", reason=str(exc), request_id=None)
            self.active_id = None
            self.r.last_error = str(exc)
            self.r.strategy_dirty = True
            self.r.flow_step("validation", "订单当前阶段无法下发动作", "rejected",
                             {"order_id": order["order_id"], "order_stage": stage, "reason": str(exc), "command": command})

    async def feedback(self, action: dict, message: dict) -> None:
        order = self.items.get(action["parent_skill_id"])
        if not order or order["request_id"] != message["request_id"]:
            return
        status = message["status"]
        if status in {"paused", "resumed"}:
            receipt = (message["request_id"], status)
            if order.get("last_control_feedback") != receipt:
                order["last_control_feedback"] = receipt
                self.r.record(order["player_id"], "action_" + status, order_id=order["order_id"],
                              request_id=message["request_id"], command=action["command"],
                              result=message.get("result", {}), source=message.get("source", "godot_feedback"))
            return
        if status not in {"completed", "failed", "cancelled"}:
            return
        order["request_id"] = None
        order["last_result"] = message.get("result", {})
        self.active_id = None
        if message.get("result", {}).get("cup_id") == order["cup_id"]:
            order["cup_exists"] = True
            order["holder_id"] = message["result"].get("holder_id", order["holder_id"])
            order["socket_id"] = message["result"].get("socket_id", order["socket_id"])
        if order["status"] == "cancelled":
            recycle_steps = {"RECYCLE_PICKUP_MOVE": "RECYCLE_RETRIEVE", "RECYCLE_RETRIEVE": "RECYCLE_MOVE", "RECYCLE_MOVE": "RECYCLE_PLACE"}
            if order["stage"] in recycle_steps and status == "completed":
                order["stage"] = recycle_steps[order["stage"]]
                self.active_id = order["order_id"]
                await self.start_stage(order)
                return
            if order["stage"] == "RECYCLE_PLACE" and status == "completed":
                order.update(cleanup_pending=False, cup_exists=False, stage="CANCELLED")
                self.r.record(order["player_id"], "cup_recycled", order_id=order["order_id"], cup_id=order["cup_id"])
            elif order["stage"].startswith("RECYCLE_"):
                order["cleanup_failed"] = True
                self.r.last_error = "杯子回收未完成：" + message.get("reason", status)
            else:
                order["cleanup_pending"] = order["cup_exists"]
            await self.pump()
            return
        if status != "completed":
            # A failed pickup leaves the physical cup at the counter, allowing a genuine retry.
            order["status"] = "ready" if order["stage"] == "COLLECT" else "failed"
            order["stage"] = "WAIT_PICKUP" if order["stage"] == "COLLECT" else order["stage"]
            order["reason"] = message.get("reason", status)
            self.r.last_error = f"{order['order_id']}: {order['reason']}"
            self.r.strategy_dirty = True
            await self.pump()
            return
        stage = order["stage"]
        self.r.record(order["player_id"], "action_completed", order_id=order["order_id"], command=action["command"], result=message.get("result", {}))
        next_stages = {"MOVE_TO_MACHINE": "BREW", "BREW": "MOVE_TO_COUNTER", "MOVE_TO_COUNTER": "PLACE"}
        paused = order["status"] == "paused"
        if stage in next_stages:
            order["stage"] = next_stages[stage]
            order["status"] = "paused" if paused else "queued"
            if paused:
                order["resume_status"] = "queued"
        elif stage == "PLACE":
            order.update(stage="WAIT_PICKUP", status="paused" if paused else "ready")
            self.r.strategy_dirty = True
            if paused:
                order["resume_status"] = "ready"
            await self.r.prompt(order["player_id"], "collect_cup", "饮品已放在柜台，走近后领取。",
                                [{"label": "领取饮品", "kind": "collect_cup", "value": True}], order_id=order["order_id"])
            self.r.enqueue(order["player_id"], "order_ready", {"order_id": order["order_id"], "recipe_id": order["recipe_id"]})
        elif stage == "COLLECT":
            order.update(stage="DONE", status="completed")
            self.r.dismiss_related_prompts("order_id", order["order_id"])
            self.r.strategy_dirty = True
            await self.r.summarize(order["player_id"], "service")
        await self.pump()
