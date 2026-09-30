"""Harness 因果追踪回归源码；需经批准的 MCP Python 入口执行，未由 CLI 运行。"""

import asyncio
import copy
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from cafe.config import Settings
from cafe.debug_console import DebugSession, debug_input
from cafe.memory import MemoryStore
from cafe.runtime import NpcRuntime
from cafe.tracing import TraceJournal, emit_model_trace


SCENE = {
    "type": "scene_ready", "protocol": "cafe3d.v1", "room_id": "cafe_demo",
    "npc_id": "npc_owner", "character_id": "owner_01",
    "entities": [{"entity_id": "machine_01", "available": True}],
    "anchors": [{"anchor_id": value, "available": True} for value in ("coffee_machine", "counter", "recycle_spot")],
    "sockets": [{"socket_id": value} for value in ("pickup_socket", "recycle_socket")],
    "supported_commands": ["move_to_anchor", "brew", "place_cup", "collect_cup", "retrieve_cup"],
}


class GatedGateway:
    """Explicit model fixture: pause event proposals while other inputs and receipts arrive."""

    configured = True

    def __init__(self):
        self.requests = asyncio.Queue()

    async def generate(self, task, context):
        emit_model_trace("model_request", "测试替身请求（不发送 HTTP）", {"request": context}, status="sent")
        if task == "event":
            result_future = asyncio.get_running_loop().create_future()
            await self.requests.put((copy.deepcopy(context), result_future))
            result = await result_future
        elif task == "strategy":
            ids = [goal["goal_id"] for goal in context["candidate_goals"]][:3]
            result = {"active_goal_ids": ids, "primary_goal_id": next(iter(ids), None),
                      "interaction_mode": "fixture", "next_step": "fixture", "review_on": []}
        else:
            result = {"summary": "测试事实摘要", "topic_id": context["topic_id"],
                      "participant_ids": [context["player_id"]], "resume_hint": ""}
        emit_model_trace("model_response", "测试替身响应", {"response": result}, status="received")
        emit_model_trace("model_validation", "测试替身返回固定结构", {"result": result}, status="validated")
        return result


def answer(player_id, text="测试回复", *, reaction="REACT", action=None, speech=True):
    return {"reaction": reaction, "goal_id": None, "action": action, "memory_updates": [],
            "speech": {"target_player_ids": [player_id], "text": text, "emotion": "warm"} if speech else None}


class FlowTracingCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.trace = TraceJournal("test", "flow_case", log_dir=self.directory.name)
        self.memory = MemoryStore(":memory:")
        self.gateway = GatedGateway()
        self.output = []

        async def send(message):
            self.output.append(copy.deepcopy(message))

        self.runtime = NpcRuntime(Settings(mode="model"), self.memory, self.gateway, send,
                                  trace=self.trace, world_mode="debug_manual")
        await self.runtime.handle(copy.deepcopy(SCENE))
        for player_id in ("p_A", "p_B"):
            await self.runtime.handle({"type": "player_entered", "player_id": player_id})
        # Fixture starts after both arrivals; no greeting generation is under test here.
        self.runtime.agenda.clear()
        self.worker = asyncio.create_task(self.runtime.run())

    async def asyncTearDown(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        await self.runtime.close()
        self.memory.close()
        self.directory.cleanup()

    def records(self):
        return self.trace.events()["events"]

    def flow_for(self, text):
        return next(row["flow_id"] for row in self.records()
                    if row["kind"] == "input" and row["data"].get("type") == "utterance" and row["data"].get("text") == text)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                if self.worker.done():
                    await self.worker
                    self.fail("runtime worker ended before the expected observation")
                await asyncio.sleep(0)

    async def utterance(self, player_id, text, **extra):
        await self.runtime.inbox.put({"type": "utterance", "player_id": player_id, "text": text,
                                      "target_id": "npc_owner", "perceived_by": ["npc_owner"], **extra})
        await self.until(lambda: any(row["kind"] == "input" and row["data"].get("text") == text for row in self.records()))

    async def next_request(self):
        return await asyncio.wait_for(self.gateway.requests.get(), timeout=3)

    async def finish_speech(self, speech):
        for status in ("displayed", "finished"):
            await self.runtime.inbox.put({"type": "speech_feedback", "speech_id": speech["speech_id"],
                                          "status": status, "source": "test_fixture"})
        await self.until(lambda: self.runtime.current_speech is None or self.runtime.current_speech["speech_id"] != speech["speech_id"])

    async def test_two_players_keep_distinct_model_output_and_feedback_chains(self):
        await self.utterance("p_A", "A 的问题")
        context_a, reply_a = await self.next_request()
        await self.utterance("p_B", "B 的问题")
        flow_a, flow_b = self.flow_for("A 的问题"), self.flow_for("B 的问题")
        self.assertNotEqual(flow_a, flow_b)
        self.assertFalse(any(row["kind"] == "flow_step" and row["flow_id"] in {flow_a, flow_b}
                             and row["data"]["stage"] == "apply" for row in self.records()))
        reply_a.set_result(answer("p_A", "A 的回复"))
        context_b, reply_b = await self.next_request()
        self.assertEqual((context_a["event"]["player_id"], context_b["event"]["player_id"]), ("p_A", "p_B"))
        reply_b.set_result(answer("p_B", "B 的回复"))
        await self.until(lambda: self.runtime.current_speech and len(self.runtime.speech_queue) == 1)
        speech_a = copy.deepcopy(self.runtime.current_speech)
        await self.finish_speech(speech_a)
        await self.until(lambda: self.runtime.current_speech is not None)
        speech_b = copy.deepcopy(self.runtime.current_speech)
        await self.finish_speech(speech_b)

        rows = self.records()
        event_calls = [row for row in rows if row["kind"] == "model_call" and row["lane"] == "event"]
        self.assertEqual(len(event_calls), 2)
        for call, flow, speech in zip(event_calls, (flow_a, flow_b), (speech_a, speech_b)):
            self.assertEqual(call["flow_id"], flow)
            related = [row for row in rows if row["call_id"] == call["call_id"]]
            self.assertTrue(all(row["flow_id"] == flow for row in related))
            self.assertTrue({"model_request", "model_response", "model_validation", "model_result"}.issubset({row["kind"] for row in related}))
            receipt_rows = [row for row in rows if row["kind"] == "input" and row["data"].get("speech_id") == speech["speech_id"]]
            self.assertEqual({row["flow_id"] for row in receipt_rows}, {flow})
            steps = {row["data"]["stage"] for row in rows if row["flow_id"] == flow and row["kind"] == "flow_step"}
            self.assertEqual(steps, {"input", "perception", "queue", "context", "decision", "validation", "apply", "output", "feedback"})
        self.assertTrue(all(not row["flow_id"] for row in rows if row["kind"] == "model_call" and row["lane"] == "strategy"))
        self.assertTrue(any(flow_b in row["data"].get("related_flow_ids", []) for row in rows if row["kind"] == "model_call" and row["lane"] == "strategy"))

    async def test_order_progress_uses_order_origin_while_other_player_model_is_waiting(self):
        await self.utterance("p_A", "A 点拿铁")
        _, reply_a = await self.next_request()
        reply_a.set_result(answer("p_A", action={"type": "order_drink", "args": {"recipe_id": "latte"}}, speech=False))
        await self.until(lambda: bool(self.runtime.orders.items))
        order = next(iter(self.runtime.orders.items.values()))
        await self.runtime.inbox.put({"type": "interaction", "player_id": "p_A", "kind": "confirm_order",
                                      "order_id": order["order_id"], "prompt_id": order["prompt_id"]})
        await self.until(lambda: bool(order["request_id"]))
        first_action = order["request_id"]
        await self.utterance("p_B", "B 问问题")
        _, reply_b = await self.next_request()
        await self.runtime.inbox.put({"type": "action_feedback", "request_id": first_action, "status": "completed",
                                      "result": {"anchor_id": "coffee_machine", "position": [0, 0, 0]}, "source": "test_fixture"})
        await self.until(lambda: order["stage"] == "BREW" and order["request_id"] != first_action)
        flow_a, flow_b = self.flow_for("A 点拿铁"), self.flow_for("B 问问题")
        second_action = order["request_id"]
        commands = [row for row in self.records() if row["kind"] == "output" and row["data"].get("request_id") in {first_action, second_action}]
        self.assertEqual({row["flow_id"] for row in commands}, {flow_a})
        self.assertNotIn(flow_b, {row["flow_id"] for row in commands})
        self.assertTrue(any(row["flow_id"] == flow_a and row["status"] == "waiting_feedback" for row in self.records()))
        reply_b.set_result(answer("p_B"))

    async def test_unheard_invalid_and_other_target_inputs_end_with_evidence(self):
        await self.utterance("p_A", "听不到", perceived_by=[], flow_id="caller_supplied")
        await self.utterance("p_A", "对别人说", target_id="p_B")
        await self.utterance({"invalid": True}, "无效身份")
        await self.utterance("p_A", "")
        await self.until(lambda: len([row for row in self.records() if row["kind"] == "flow_step" and row["status"] == "rejected"]) >= 3)
        self.assertNotEqual(self.flow_for("听不到"), "caller_supplied")
        self.assertFalse(any(row["kind"] == "model_call" and row["lane"] == "event" for row in self.records()))
        unheard = [row for row in self.records() if row["flow_id"] == self.flow_for("听不到") and row["kind"] == "flow_step"]
        self.assertTrue(any(row["data"].get("reason") == "out_of_hearing_range" and row["status"] == "rejected" for row in unheard))
        other = [row for row in self.records() if row["flow_id"] == self.flow_for("对别人说") and row["kind"] == "flow_step"]
        self.assertTrue(any(row["data"].get("reason") == "target_is_other" for row in other))

    async def test_deferred_retry_and_stale_result_keep_original_flow(self):
        await self.utterance("p_A", "A 稍后回答")
        _, first_a = await self.next_request()
        await self.utterance("p_B", "B 先回答")
        first_a.set_result(answer("p_A", reaction="DEFER", speech=False))
        _, reply_b = await self.next_request()
        reply_b.set_result(answer("p_B"))
        await self.until(lambda: self.runtime.current_speech is not None)
        await self.finish_speech(copy.deepcopy(self.runtime.current_speech))
        context_a, second_a = await self.next_request()
        self.assertEqual(context_a["event"]["player_id"], "p_A")
        flow_a = self.flow_for("A 稍后回答")
        calls_a = [row for row in self.records() if row["kind"] == "model_call" and row["lane"] == "event" and row["data"]["player_id"] == "p_A"]
        self.assertEqual([row["flow_id"] for row in calls_a], [flow_a, flow_a])
        await self.runtime.inbox.put({"type": "interaction", "player_id": "p_A", "kind": "give_space"})
        await self.until(lambda: self.runtime.players["p_A"]["give_space"])
        second_a.set_result(answer("p_A", "过期回复"))
        await self.until(lambda: any(row["kind"] == "model_result" and row["call_id"] == calls_a[-1]["call_id"] for row in self.records()))
        self.assertTrue(any(row["flow_id"] == flow_a and row["status"] == "deferred" for row in self.records()))
        rejected = next(row for row in self.records() if row["kind"] == "model_result" and row["call_id"] == calls_a[-1]["call_id"])
        self.assertEqual((rejected["flow_id"], rejected["status"]), (flow_a, "discarded"))
        self.assertTrue(rejected["data"]["reason"])
        self.assertFalse(any(row["kind"] == "output" and row["data"].get("text") == "过期回复" for row in self.records()))

    async def test_web_rejection_keeps_raw_input_root_and_does_not_enqueue(self):
        session = DebugSession("lab", "case", self.trace, runtime=self.runtime, worker=self.worker)
        message = {"type": "utterance", "player_id": "p_UNKNOWN", "text": "入口拒绝"}
        with patch("cafe.debug_console.service") as manager:
            manager.return_value.session.return_value = session
            with self.assertRaises(HTTPException) as rejected:
                await debug_input(message, object())
        self.assertEqual(rejected.exception.status_code, 422)
        root = next(row for row in self.records() if row["kind"] == "input" and row["data"] == message)
        self.assertTrue(root["flow_id"])
        steps = [row for row in self.records() if row["flow_id"] == root["flow_id"] and row["kind"] == "flow_step"]
        self.assertEqual([row["data"]["stage"] for row in steps], ["input", "validation"])
        self.assertEqual(steps[-1]["status"], "rejected")
        self.assertFalse(any(item["payload"].get("text") == "入口拒绝" for item in self.runtime.agenda))

    async def test_topic_buttons_follow_invitation_then_new_spoken_continuation(self):
        await self.utterance("p_A", "想听手冲讲解")
        _, invitation_reply = await self.next_request()
        invitation_reply.set_result(answer("p_A", action={"type": "invite_topic", "args": {"topic_id": "pourover_intro"}}, speech=False))
        await self.until(lambda: bool(self.runtime.topics.items))
        conv = next(iter(self.runtime.topics.items.values()))
        invite_flow = self.flow_for("想听手冲讲解")
        accept = {"type": "interaction", "player_id": "p_A", "kind": "accept_invitation",
                  "conversation_id": conv["conversation_id"], "prompt_id": conv["prompt_id"]}
        # The same prompt cannot attribute another player's unrelated input.
        self.assertEqual(self.runtime._input_origin({**accept, "player_id": "p_B"}), ("", ""))
        await self.runtime.inbox.put(accept)
        first_context, first_segment = await self.next_request()
        self.assertEqual(first_context["event"]["type"], "topic_segment")
        first_segment.set_result(answer("p_A", "第一段讲解"))
        await self.until(lambda: self.runtime.current_speech is not None)
        first_speech = copy.deepcopy(self.runtime.current_speech)
        await self.finish_speech(first_speech)
        self.assertEqual(conv["stage"], "water_temperature")
        self.assertTrue(any(row["kind"] == "input" and row["data"] == accept and row["flow_id"] == invite_flow for row in self.records()))

        await self.utterance("p_A", "现在自然继续")
        _, continuation_reply = await self.next_request()
        continuation_reply.set_result(answer("p_A", action={"type": "continue_topic", "args": {"conversation_id": conv["conversation_id"]}}, speech=False))
        second_context, second_segment = await self.next_request()
        self.assertEqual(second_context["event"]["stage"], "water_temperature")
        second_segment.set_result(answer("p_A", "第二段讲解"))
        await self.until(lambda: self.runtime.current_speech is not None)
        await self.finish_speech(copy.deepcopy(self.runtime.current_speech))
        continuation_flow = self.flow_for("现在自然继续")
        self.assertNotEqual(invite_flow, continuation_flow)
        self.assertEqual(self.runtime.trace_origin(conv["conversation_id"])[0], continuation_flow)

        progress = next(prompt for prompt in self.runtime.prompts.values() if prompt.get("conversation_id") == conv["conversation_id"])
        await self.runtime.inbox.put({"type": "interaction", "player_id": "p_A", "kind": "next_topic",
                                      "conversation_id": conv["conversation_id"], "prompt_id": progress["prompt_id"]})
        third_context, third_segment = await self.next_request()
        self.assertEqual(third_context["event"]["stage"], "pouring")
        third_segment.set_result(answer("p_A", "第三段讲解"))
        await self.until(lambda: self.runtime.current_speech is not None)
        await self.finish_speech(copy.deepcopy(self.runtime.current_speech))
        segment_calls = [row for row in self.records() if row["kind"] == "model_call" and row["lane"] == "event"
                         and row["data"]["context"]["event"]["type"] == "topic_segment"]
        self.assertEqual([row["flow_id"] for row in segment_calls], [invite_flow, continuation_flow, continuation_flow])
        self.assertEqual(conv["stage"], "finished")
