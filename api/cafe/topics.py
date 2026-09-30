from .schemas import TOPIC_LABELS, TOPIC_STAGES


class Topics:
    def __init__(self, runtime):
        self.r = runtime
        self.items: dict[str, dict] = {}

    def find(self, player_id: str, conversation_id: str = "") -> dict | None:
        if conversation_id:
            item = self.items.get(conversation_id)
            if not item or item["player_id"] != player_id:
                raise ValueError("会话不存在或不属于当前玩家")
            return item
        return next((c for c in reversed(list(self.items.values())) if c["player_id"] == player_id and c["status"] != "closed"), None)

    async def invite(self, player_id: str, topic_id: str = "pourover_intro") -> None:
        if topic_id != "pourover_intro":
            raise ValueError("当前可体验的讲解话题为 pourover_intro")
        player = self.r.players[player_id]
        if player.get("give_space"):
            return
        previous = self.find(player_id)
        if previous and previous["status"] in {"waiting_reply", "engaged"}:
            return
        saved = next((t for t in self.r.memory.view(player_id)["topics"] if t["topic_id"] == topic_id), {})
        stage = saved.get("stage", "beans")
        if stage == "finished":
            stage = "beans"
        conversation_id = self.r.new_id("conv")
        conv = {"conversation_id": conversation_id, "player_id": player_id, "topic_id": topic_id,
                "status": "waiting_reply", "stage": stage, "segment_id": "", "pending_segment": False,
                "displayed_segment_id": "",
                "visit_id": player["visit_id"], "summary": saved.get("summary", "")}
        self.items[conversation_id] = conv
        self.r.trace_link(conversation_id)
        prompt = await self.r.prompt(player_id, "topic_invitation", f"要从「{TOPIC_LABELS[stage]}」继续手冲话题吗？",
                                    [{"label": "接受", "kind": "accept_invitation", "value": True},
                                     {"label": "今天不讲", "kind": "decline_invitation", "value": False}],
                                    conversation_id=conversation_id, expires_in=90)
        conv["prompt_id"] = prompt["prompt_id"]
        self.r.strategy_dirty = True

    async def interact(self, player_id: str, kind: str, message: dict) -> None:
        conv = self.find(player_id, message.get("conversation_id", ""))
        if not conv:
            if kind == "resume_topic":
                self.r.players[player_id]["give_space"] = False
                await self.invite(player_id)
                return
            raise ValueError("当前没有可操作的会话")
        if kind in {"accept_invitation", "decline_invitation"}:
            if conv["status"] != "waiting_reply" or message.get("prompt_id") != conv["prompt_id"]:
                raise ValueError("邀请已经失效或 prompt_id 不匹配")
            self.r.dismiss_prompt(conv["prompt_id"])
        if kind == "accept_invitation":
            conv["status"] = "engaged"
            self.r.players[player_id]["give_space"] = False
            self.r.record(player_id, "topic_accepted", topic_id=conv["topic_id"])
            await self.segment(conv)
        elif kind == "decline_invitation":
            conv["status"] = "closed"
            self.r.players[player_id]["give_space"] = True
            self.save(conv, "paused")
            self.r.record(player_id, "topic_declined", topic_id=conv["topic_id"])
        elif kind in {"pause_topic", "next_visit", "end_topic"}:
            if conv["status"] == "closed" and kind == "pause_topic":
                raise ValueError("已结束会话不能暂停")
            await self.r.stop_speech(player_id, conv["conversation_id"])
            conv["pending_segment"] = False
            conv["status"] = "paused" if kind == "pause_topic" else "closed"
            if conv["status"] == "closed":
                self.r.dismiss_related_prompts("conversation_id", conv["conversation_id"])
            self.save(conv, "paused" if conv["stage"] != "finished" else "completed")
            if kind == "next_visit" and conv["stage"] != "finished":
                self.r.memory.promise(player_id, conv["topic_id"], self.r.players[player_id]["visit_id"])
                self.r.players[player_id]["give_space"] = True
            self.r.record(player_id, kind, topic_id=conv["topic_id"], next_stage=conv["stage"])
            if kind != "pause_topic":
                await self.r.summarize(player_id, conv["topic_id"])
        elif kind in {"resume_topic", "next_topic"}:
            if conv["stage"] == "finished":
                raise ValueError("本话题已讲完")
            if conv["status"] not in {"paused", "engaged"}:
                raise ValueError("请先接受有效邀请")
            self.r.players[player_id]["give_space"] = False
            conv["status"] = "engaged"
            await self.segment(conv)
        self.r.strategy_dirty = True
        await self.r.send(self.r.memory.view(player_id))

    async def segment(self, conv: dict) -> None:
        if conv["pending_segment"]:
            return
        conv["pending_segment"] = True
        conv["segment_id"] = self.r.new_id("segment")
        self.r.trace_link(conv["segment_id"])
        origin = self.r.trace_origin(conv["segment_id"])
        if not origin[0]:
            origin = self.r.trace_origin(conv["conversation_id"])
        # A new spoken continuation becomes the source for subsequent explicit buttons;
        # otherwise the stable conversation carries its existing invitation/segment source.
        self.r.trace_link(conv["segment_id"], origin)
        self.r.trace_link(conv["conversation_id"], origin)
        with self.r.trace_scope(*origin):
            self.r.enqueue(conv["player_id"], "topic_segment", {
                "conversation_id": conv["conversation_id"], "topic_id": conv["topic_id"],
                "stage": conv["stage"], "stage_label": TOPIC_LABELS[conv["stage"]], "segment_id": conv["segment_id"],
            })

    async def continue_requested(self, player_id: str, conversation_id: str = "") -> None:
        """模型已识别玩家主动要求续谈；这次要求本身是接受的依据。"""
        self.r.players[player_id]["give_space"] = False
        conv = self.find(player_id, conversation_id)
        if not conv or conv["status"] == "closed":
            await self.invite(player_id)
            conv = self.find(player_id)
        if not conv:
            raise ValueError("没有可继续的话题")
        if conv["status"] == "waiting_reply":
            await self.interact(player_id, "accept_invitation", {"conversation_id": conv["conversation_id"], "prompt_id": conv["prompt_id"]})
        else:
            await self.interact(player_id, "resume_topic", {"conversation_id": conv["conversation_id"]})

    def save(self, conv: dict, status: str) -> None:
        self.r.memory.save_topic(conv["player_id"], conv["topic_id"], conv["stage"], conv["summary"],
                                 TOPIC_LABELS[conv["stage"]], status)

    async def speech_displayed(self, speech: dict) -> None:
        """HUD 整段已画出才提交进度；显示时长结束与进度无关。"""
        conv = self.items.get(speech.get("conversation_id"))
        segment_id = speech.get("segment_id")
        if not conv or conv.get("forgotten") or not segment_id or conv["segment_id"] != segment_id or conv.get("displayed_segment_id") == segment_id:
            return
        conv["displayed_segment_id"] = segment_id
        conv["pending_segment"] = False
        old_stage = conv["stage"]
        conv["summary"] = f"已实际展示至{TOPIC_LABELS[old_stage]}。"
        conv["stage"] = TOPIC_STAGES[min(TOPIC_STAGES.index(old_stage) + 1, len(TOPIC_STAGES) - 1)]
        if conv["stage"] == "finished":
            conv["status"] = "closed"
            self.r.dismiss_related_prompts("conversation_id", conv["conversation_id"])
            self.r.memory.close_promises(conv["player_id"], conv["topic_id"])
            self.save(conv, "completed")
            await self.r.summarize(conv["player_id"], conv["topic_id"])
        else:
            self.save(conv, "engaged" if conv["status"] == "engaged" else "paused")
        if conv["status"] == "engaged":
            await self.r.prompt(conv["player_id"], "topic_progress", f"下一段是「{TOPIC_LABELS[conv['stage']]}」。",
                                [{"label": "继续下一段", "kind": "next_topic", "value": True},
                                 {"label": "稍等", "kind": "pause_topic", "value": True},
                                 {"label": "下次继续", "kind": "next_visit", "value": True}],
                                conversation_id=conv["conversation_id"])
        await self.r.send(self.r.memory.view(conv["player_id"]))
        self.r.strategy_dirty = True

    async def leave(self, player_id: str) -> None:
        for conv in self.items.values():
            if conv["player_id"] == player_id and conv["status"] != "closed":
                await self.r.stop_speech(player_id, conv["conversation_id"])
                conv["status"] = "closed"
                conv["pending_segment"] = False
                self.save(conv, "paused")
