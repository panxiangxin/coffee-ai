"""后端回归用例源码。须通过 MCP Python 测试入口配置正确的路径、解释器和依赖后执行。"""

import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cafe.config import Settings
from cafe.memory import MemoryStore
from cafe.model import ModelGateway
from cafe.runtime import NpcRuntime


SCENE = {
    "type": "scene_ready", "protocol": "cafe3d.v1", "room_id": "cafe_demo", "npc_id": "npc_owner", "character_id": "owner_01",
    "entities": [{"entity_id": "machine_01", "available": True}, {"entity_id": "counter_01", "available": True}],
    "anchors": [{"anchor_id": anchor, "available": True} for anchor in ["coffee_machine", "counter", "recycle_spot"]],
    "sockets": [{"socket_id": socket} for socket in ["pickup_socket", "recycle_socket"]],
    "supported_commands": ["move_to_anchor", "brew", "place_cup", "collect_cup", "retrieve_cup", "tidy", "look_at_entity", "gesture"],
}


class RuntimeCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = []
        async def send(message):
            self.output.append(copy.deepcopy(message))
        self.memory = MemoryStore(":memory:")
        self.gateway = ModelGateway(Settings(mode="rule"))
        self.runtime = NpcRuntime(Settings(mode="rule"), self.memory, self.gateway, send)
        await self.runtime.handle(copy.deepcopy(SCENE))
        for player_id in ("p_A", "p_B"):
            await self.runtime.handle({"type": "player_entered", "player_id": player_id})

    async def asyncTearDown(self):
        await self.runtime.close()
        await self.gateway.close()
        self.memory.close()

    async def order(self, player_id="p_A"):
        await self.runtime.interact({"player_id": player_id, "kind": "order_drink", "value": "latte"})
        order = next(o for o in self.runtime.orders.items.values() if o["player_id"] == player_id)
        await self.runtime.interact({"player_id": player_id, "kind": "confirm_order", "order_id": order["order_id"], "prompt_id": order["prompt_id"]})
        return order

    async def complete(self, order, **result):
        request_id = order["request_id"]
        action = self.runtime.actions[request_id]
        payload = {**action["args"], "position": [0, 0, 0], **result}
        await self.runtime.feedback({"request_id": request_id, "npc_id": "npc_owner", "status": "completed", "result": payload})

    async def test_wrong_completion_cannot_advance_order(self):
        order = await self.order()
        with self.assertRaises(ValueError):
            await self.runtime.feedback({"request_id": order["request_id"], "status": "completed", "result": {"anchor_id": "wrong", "position": [0, 0, 0]}})
        self.assertEqual(order["stage"], "MOVE_TO_MACHINE")
        await self.complete(order)
        self.assertEqual(order["stage"], "BREW")

    async def test_pause_and_late_completion_never_start_next_stage(self):
        order = await self.order()
        await self.runtime.interact({"player_id": "p_A", "kind": "pause_order", "order_id": order["order_id"]})
        await self.complete(order)
        self.assertEqual((order["stage"], order["status"]), ("BREW", "paused"))
        self.assertIsNone(order["request_id"])
        await self.runtime.interact({"player_id": "p_A", "kind": "resume_order", "order_id": order["order_id"]})
        self.assertEqual(self.runtime.actions[order["request_id"]]["command"], "brew")

    async def test_other_player_cannot_cancel_order(self):
        order = await self.order()
        with self.assertRaises(ValueError):
            await self.runtime.interact({"player_id": "p_B", "kind": "cancel_order", "order_id": order["order_id"]})
        self.assertEqual(order["status"], "running")

    async def test_pause_resume_refresh_strategy_without_snapshot_polling_churn(self):
        self.runtime.agenda.clear()
        order = await self.order()
        with patch.object(self.runtime, "launch_model") as launch:
            await self.runtime.schedule()
            self.assertEqual([c.args[0] for c in launch.call_args_list], ["strategy"])
            await self.runtime.interact({"player_id": "p_A", "kind": "pause_order", "order_id": order["order_id"]})
            self.assertTrue(self.runtime.strategy_dirty)
            await self.runtime.schedule()
            self.assertEqual(len(launch.call_args_list), 2)
            self.assertEqual(launch.call_args.args[1]["candidate_goals"][0]["status"], "suspended")
            for snapshot_id in range(4):
                self.runtime.observe({"room_id": "cafe_demo", "snapshot_id": snapshot_id, "entities": [], "perception": {}})
                await self.runtime.schedule()
            self.assertEqual(len(launch.call_args_list), 2)
            await self.runtime.interact({"player_id": "p_A", "kind": "resume_order", "order_id": order["order_id"]})
            self.assertTrue(self.runtime.strategy_dirty)
            await self.runtime.schedule()
            self.assertEqual(len(launch.call_args_list), 3)
            self.assertEqual(launch.call_args.args[1]["candidate_goals"][0]["status"], "active")
            await self.runtime.schedule()
            self.assertEqual(len(launch.call_args_list), 3)

    async def test_answer_goal_refreshes_on_enqueue_and_finished_not_internal_stages(self):
        self.runtime.agenda.clear()
        with patch.object(self.runtime, "launch_model") as launch:
            await self.runtime.schedule()
            strategy_count = lambda: sum(c.args[0] == "strategy" for c in launch.call_args_list)
            self.assertEqual(strategy_count(), 1)
            self.runtime.enqueue("p_A", "menu_question", {})
            item = self.runtime.agenda[-1]
            await self.runtime.schedule()
            self.assertEqual(strategy_count(), 2)
            await self.runtime.apply_plan(item, self.runtime.rule_result("event", self.runtime.context(item)))
            await self.runtime.schedule()
            self.assertEqual(strategy_count(), 2)
            self.assertIn("answer_" + item["item_id"], [g["goal_id"] for g in self.runtime.goals()])
            speech_id = self.runtime.current_speech["speech_id"]
            await self.runtime.speech_feedback({"speech_id": speech_id, "status": "displayed"})
            await self.runtime.schedule()
            self.assertEqual(strategy_count(), 2)
            await self.runtime.speech_feedback({"speech_id": speech_id, "status": "finished"})
            await self.runtime.schedule()
            self.assertEqual(strategy_count(), 3)
            self.assertNotIn("answer_" + item["item_id"], [g["goal_id"] for g in self.runtime.goals()])
            for _ in range(4):
                await self.runtime.schedule()
            self.assertEqual(strategy_count(), 3)
            self.runtime.enqueue("p_A", "utterance", {"text": "不用回答"})
            quiet_item = self.runtime.agenda[-1]
            await self.runtime.schedule()
            self.assertEqual(strategy_count(), 4)
            await self.runtime.apply_plan(quiet_item, {"reaction": "CONTINUE", "speech": None, "action": None, "memory_updates": []})
            await self.runtime.schedule()
            self.assertEqual(strategy_count(), 5)

    async def test_pause_resume_history_uses_actual_godot_feedback(self):
        order = await self.order()
        request_id = order["request_id"]
        await self.runtime.interact({"player_id": "p_A", "kind": "pause_order", "order_id": order["order_id"]})
        self.assertFalse(any(h["type"] == "action_paused" for h in self.runtime.history["p_A"]))
        pause_receipt = {"request_id": request_id, "status": "paused", "result": {"body": {"movement": "paused"}}}
        await self.runtime.feedback(pause_receipt)
        await self.runtime.feedback(pause_receipt)
        self.assertEqual(sum(h["type"] == "action_paused" for h in self.runtime.history["p_A"]), 1)
        await self.runtime.interact({"player_id": "p_A", "kind": "resume_order", "order_id": order["order_id"]})
        self.assertFalse(any(h["type"] == "action_resumed" for h in self.runtime.history["p_A"]))
        await self.runtime.feedback({"request_id": request_id, "status": "resumed", "result": {"body": {"movement": "moving"}}})
        facts = [h for h in self.runtime.history["p_A"] if h["type"] in {"action_paused", "action_resumed"}]
        self.assertEqual([h["type"] for h in facts], ["action_paused", "action_resumed"])
        self.assertTrue(all(h["source"] == "godot_feedback" and h["request_id"] == request_id for h in facts))

    async def test_waiting_order_history_records_harness_state_not_animation(self):
        await self.order("p_A")
        queued = await self.order("p_B")
        self.assertIsNone(queued["request_id"])
        await self.runtime.interact({"player_id": "p_B", "kind": "pause_order", "order_id": queued["order_id"]})
        await self.runtime.interact({"player_id": "p_B", "kind": "resume_order", "order_id": queued["order_id"]})
        facts = [h for h in self.runtime.history["p_B"] if h["type"] in {"order_paused", "order_resumed"}]
        self.assertEqual([h["type"] for h in facts], ["order_paused", "order_resumed"])
        self.assertTrue(all(h["source"] == "harness_state" for h in facts))
        self.assertFalse(any(h["type"] in {"action_paused", "action_resumed"} for h in self.runtime.history["p_B"]))

    async def test_single_pickup_socket_waits_for_real_collection(self):
        order_a = await self.order()
        await self.complete(order_a)
        await self.complete(order_a, holder_id="npc_owner")
        await self.complete(order_a)
        await self.complete(order_a, holder_id="counter_01")
        order_b = await self.order("p_B")
        self.assertEqual(order_a["status"], "ready")
        self.assertEqual(order_b["status"], "queued")
        await self.runtime.interact({"player_id": "p_A", "kind": "collect_cup", "order_id": order_a["order_id"]})
        await self.complete(order_a, holder_id="p_A")
        self.assertEqual(order_a["status"], "completed")
        self.assertEqual(order_b["status"], "running")

    async def test_stale_model_result_cannot_restart_cancelled_order(self):
        order = await self.order()
        self.runtime.enqueue("p_A", "utterance", {"text": "来杯拿铁"})
        item = self.runtime.agenda[-1]
        item["status"] = "generating"
        self.runtime.lanes["event"] = "old_decision"
        revision = item["revision"]
        await self.runtime.interact({"player_id": "p_A", "kind": "cancel_order", "order_id": order["order_id"]})
        await self.runtime.model_result({"lane": "event", "decision_id": "old_decision", "item_id": item["item_id"], "revision": revision,
                                         "result": {"reaction": "REACT", "action": {"type": "order_drink", "args": {"recipe_id": "latte"}}}})
        self.assertEqual(len(self.runtime.orders.items), 1)
        self.assertEqual(order["status"], "cancelled")

    async def test_event_success_counts_only_accepted_plans_and_validation_has_no_side_effects(self):
        self.runtime.settings = Settings(mode="model")
        self.runtime.calls["event"] = 3

        async def deliver(plan, event_type="utterance", payload=None):
            self.runtime.enqueue("p_A", event_type, payload or {"text": "来杯拿铁，不加糖"})
            item = self.runtime.agenda[-1]
            item["status"] = "generating"
            decision_id = self.runtime.new_id("decision")
            self.runtime.lanes["event"] = decision_id
            await self.runtime.model_result({"lane": "event", "decision_id": decision_id,
                                             "item_id": item["item_id"], "revision": item["revision"], "result": plan})
            return item

        rejected = await deliver({"reaction": "REACT", "speech": {"target_player_ids": ["p_C"], "text": "错误对象"},
                                  "action": {"type": "order_drink", "args": {"recipe_id": "latte"}}})
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(self.runtime.orders.items, {})
        self.assertEqual(self.runtime.successes["event"], 0)
        stale = await deliver({"reaction": "REACT", "action": {"type": "order_drink", "args": {"recipe_id": "latte"}}},
                              "topic_segment", {"conversation_id": "missing", "segment_id": "old"})
        self.assertEqual(stale["status"], "superseded")
        self.assertEqual(self.runtime.orders.items, {})
        self.assertEqual(self.runtime.successes["event"], 0)
        accepted = await deliver({"reaction": "REACT", "speech": {"target_player_ids": ["p_A"], "text": "请确认饮品。"}})
        self.assertEqual(accepted["status"], "presenting")
        self.assertEqual(self.runtime.successes["event"], 1)
        self.assertEqual(self.runtime.calls["event"], 3)

    async def test_all_memory_evidence_is_validated_before_any_write(self):
        self.runtime.enqueue("p_A", "utterance", {"text": "不加糖"})
        item = self.runtime.agenda[-1]
        plan = {"reaction": "REACT", "speech": None, "action": None, "memory_updates": [
            {"kind": "preference", "key": "sweetness", "value": "不加糖", "evidence_event_id": item["event_id"]},
            {"kind": "preference", "key": "milk", "value": "燕麦奶", "evidence_event_id": "wrong_event"},
        ]}
        with self.assertRaises(ValueError):
            await self.runtime.apply_plan(item, plan)
        self.assertEqual(self.memory.view("p_A")["profile"]["preferences"], {})

    async def test_strategy_and_episode_success_counts_follow_acceptance(self):
        self.runtime.settings = Settings(mode="model")
        self.runtime.strategy_dirty = False
        self.runtime.strategy_revision = 2
        original_strategy = copy.deepcopy(self.runtime.strategy)
        proposal = {"active_goal_ids": [], "primary_goal_id": None, "interaction_mode": "quiet", "next_step": "等待", "review_on": []}
        self.runtime.lanes["strategy"] = "stale_strategy"
        await self.runtime.model_result({"lane": "strategy", "decision_id": "stale_strategy", "revision": 1, "result": proposal})
        self.assertEqual(self.runtime.strategy, original_strategy)
        self.assertEqual(self.runtime.successes["strategy"], 0)
        self.runtime.lanes["strategy"] = "accepted_strategy"
        await self.runtime.model_result({"lane": "strategy", "decision_id": "accepted_strategy", "revision": 2, "result": proposal})
        self.assertEqual(self.runtime.successes["strategy"], 1)
        episode = {"lane": "episode", "player_id": "p_A", "memory_revision": 0,
                   "topic_id": "visit", "visit_id": self.runtime.players["p_A"]["visit_id"],
                   "result": {"participant_ids": ["p_B"], "summary": "一次访问。", "resume_hint": ""}}
        await self.runtime.model_result(episode)
        self.assertEqual(self.runtime.successes["episode"], 0)
        episode["result"]["participant_ids"] = ["p_A"]
        episode["memory_revision"] = 1
        await self.runtime.model_result(episode)
        self.assertEqual(self.runtime.successes["episode"], 0)
        self.assertEqual(self.memory.view("p_A")["episodes"], [])
        episode["memory_revision"] = 0
        await self.runtime.model_result(episode)
        self.assertEqual(self.runtime.successes["episode"], 1)
        self.assertEqual(len(self.memory.view("p_A")["episodes"]), 1)

    async def start_topic_segment(self):
        await self.runtime.topics.invite("p_A")
        conv = self.runtime.topics.find("p_A")
        await self.runtime.interact({"player_id": "p_A", "kind": "accept_invitation", "conversation_id": conv["conversation_id"], "prompt_id": conv["prompt_id"]})
        item = self.runtime.agenda[-1]
        await self.runtime.apply_plan(item, self.runtime.rule_result("event", self.runtime.context(item)))
        await self.runtime.pump_speech()
        return conv, item, self.runtime.current_speech["speech_id"]

    async def test_displayed_commits_once_while_finished_only_releases_speech(self):
        conv, item, speech_id = await self.start_topic_segment()
        await self.runtime.speech_feedback({"speech_id": speech_id, "status": "started"})
        self.assertEqual(conv["stage"], "beans")
        await self.runtime.speech_feedback({"speech_id": speech_id, "status": "displayed"})
        self.assertEqual(conv["stage"], "water_temperature")
        self.assertEqual(self.memory.view("p_A")["topics"][0]["stage"], "water_temperature")
        self.assertEqual(item["status"], "presenting")
        self.assertIsNotNone(self.runtime.current_speech)
        await self.runtime.speech_feedback({"speech_id": speech_id, "status": "displayed"})
        self.assertEqual(conv["stage"], "water_temperature")
        await self.runtime.speech_feedback({"speech_id": speech_id, "status": "finished"})
        self.assertEqual(conv["stage"], "water_temperature")
        self.assertIsNone(self.runtime.current_speech)
        self.assertEqual(item["status"], "completed")

    async def test_unshown_finished_or_interrupted_speech_cannot_advance_topic(self):
        conv, _, speech_id = await self.start_topic_segment()
        await self.runtime.speech_feedback({"speech_id": speech_id, "status": "finished"})
        self.assertEqual(conv["stage"], "beans")
        self.assertFalse(conv["pending_segment"])
        self.assertFalse(any(h["type"] == "npc_speech_displayed" for h in self.runtime.history["p_A"]))
        await self.runtime.topics.interact("p_A", "next_topic", {"conversation_id": conv["conversation_id"]})
        item = self.runtime.agenda[-1]
        await self.runtime.apply_plan(item, self.runtime.rule_result("event", self.runtime.context(item)))
        await self.runtime.pump_speech()
        await self.runtime.speech_feedback({"speech_id": self.runtime.current_speech["speech_id"], "status": "interrupted"})
        self.assertEqual(conv["stage"], "beans")

    async def test_inaudible_utterance_is_not_a_model_agenda(self):
        before = len(self.runtime.agenda)
        await self.runtime.utterance({"player_id": "p_A", "text": "你好", "channel": "local", "perceived_by": []})
        self.assertEqual(len(self.runtime.agenda), before)

    async def test_room_target_is_public_but_unheard_or_absent_players_are_filtered(self):
        before = len(self.runtime.agenda)
        await self.runtime.utterance({"player_id": "p_A", "text": "大家喜欢哪种咖啡？", "target_id": "room", "channel": "room", "perceived_by": ["npc_owner"]})
        self.assertEqual(len(self.runtime.agenda), before + 1)
        self.assertEqual(self.runtime.agenda[-1]["payload"]["target_id"], "room")
        await self.runtime.utterance({"player_id": "p_A", "text": "听不见的公共发言", "target_id": "room", "channel": "room", "perceived_by": []})
        self.assertEqual(len(self.runtime.agenda), before + 1)
        with self.assertRaises(ValueError):
            await self.runtime.utterance({"player_id": "p_C", "text": "不在场", "target_id": "room", "channel": "room", "perceived_by": ["npc_owner"]})

    async def test_event_hearing_wins_over_older_snapshot_and_missing_field_falls_back(self):
        before = len(self.runtime.agenda)
        await self.runtime.utterance({"player_id": "p_A", "text": "刚走近", "target_id": "npc_owner", "channel": "local", "perceived_by": ["npc_owner"]})
        self.assertEqual(len(self.runtime.agenda), before + 1)
        await self.runtime.utterance({"player_id": "p_A", "text": "缺少事件感知", "channel": "local"})
        self.assertEqual(len(self.runtime.agenda), before + 1)
        self.runtime.world_view["audible_player_ids"] = ["p_A"]
        await self.runtime.utterance({"player_id": "p_A", "text": "回退快照", "channel": "local"})
        self.assertEqual(len(self.runtime.agenda), before + 2)
        await self.runtime.utterance({"player_id": "p_A", "text": "事件明确未听见", "channel": "local", "perceived_by": []})
        self.assertEqual(len(self.runtime.agenda), before + 2)

    async def test_invitation_expires_while_world_snapshots_keep_queue_busy(self):
        await self.runtime.topics.invite("p_A")
        conv = self.runtime.topics.find("p_A")
        expired_at = self.runtime.prompts[conv["prompt_id"]]["expires_at"] + 1
        observed = asyncio.Event()
        original_send = self.runtime.send
        async def send(message):
            await original_send(message)
            if message.get("type") == "runtime_update" and conv["status"] == "closed":
                observed.set()
        self.runtime.send = send
        for snapshot_id in range(20):
            await self.runtime.inbox.put({"type": "world_snapshot", "room_id": "cafe_demo", "snapshot_id": snapshot_id, "entities": [], "perception": {}})
        with patch("cafe.runtime.time.time", return_value=expired_at):
            worker = asyncio.create_task(self.runtime.run())
            try:
                # The old idle-only tick takes at least one second and cannot satisfy this.
                await asyncio.wait_for(observed.wait(), timeout=0.5)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertNotIn(conv["prompt_id"], self.runtime.prompts)

    async def test_three_visits_resume_only_after_accepting_and_preserve_decline(self):
        await self.runtime.topics.invite("p_A")
        conv = self.runtime.topics.find("p_A")
        await self.runtime.interact({"player_id": "p_A", "kind": "accept_invitation", "conversation_id": conv["conversation_id"], "prompt_id": conv["prompt_id"]})
        for stage in ("beans", "water_temperature"):
            self.assertEqual(conv["stage"], stage)
            item = self.runtime.agenda[-1]
            await self.runtime.apply_plan(item, self.runtime.rule_result("event", self.runtime.context(item)))
            await self.runtime.pump_speech()
            speech_id = self.runtime.current_speech["speech_id"]
            await self.runtime.speech_feedback({"speech_id": speech_id, "status": "displayed"})
            if stage == "beans":
                await self.runtime.speech_feedback({"speech_id": speech_id, "status": "finished"})
                await self.runtime.interact({"player_id": "p_A", "kind": "next_topic", "conversation_id": conv["conversation_id"]})
        await self.runtime.interact({"player_id": "p_A", "kind": "next_visit", "conversation_id": conv["conversation_id"]})
        self.assertEqual(self.memory.view("p_A")["topics"][0]["stage"], "pouring")
        await self.runtime.speech_feedback({"speech_id": speech_id, "status": "interrupted"})
        self.assertEqual(self.memory.view("p_A")["topics"][0]["stage"], "pouring")
        await self.runtime.leave("p_A")
        await self.runtime.enter({"player_id": "p_A"})
        second = self.runtime.topics.find("p_A")
        self.assertEqual((second["stage"], second["status"]), ("pouring", "waiting_reply"))
        await self.runtime.interact({"player_id": "p_A", "kind": "decline_invitation", "conversation_id": second["conversation_id"], "prompt_id": second["prompt_id"]})
        prompt_count = len(self.runtime.prompts)
        await self.runtime.topics.invite("p_A")
        self.assertEqual(len(self.runtime.prompts), prompt_count)
        await self.runtime.leave("p_A")
        await self.runtime.enter({"player_id": "p_A"})
        third = self.runtime.topics.find("p_A")
        self.assertEqual((third["stage"], third["status"]), ("pouring", "waiting_reply"))
        self.assertEqual(self.memory.view("p_A")["profile"]["visit_count"], 3)
        self.assertEqual(self.memory.view("p_B")["topics"], [])


class PersistentMemoryCases(unittest.TestCase):
    def test_correction_and_topic_survive_reopen_without_crossing_players(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "memory.sqlite3")
            memory = MemoryStore(path)
            memory.preference("p_A", "sweetness", "不加糖")
            memory.save_topic("p_A", "pourover_intro", "pouring", "已讲水温", "注水", "paused")
            memory.promise("p_A", "pourover_intro", "visit_1")
            memory.close()
            memory = MemoryStore(path)
            self.assertEqual(memory.view("p_A")["profile"]["preferences"]["sweetness"], "不加糖")
            self.assertEqual(memory.view("p_A")["topics"][0]["stage"], "pouring")
            self.assertEqual(memory.view("p_B")["topics"], [])
            memory.preference("p_A", "sweetness", "少糖")
            self.assertEqual(memory.view("p_A")["profile"]["preferences"]["sweetness"], "少糖")
            memory.close()
