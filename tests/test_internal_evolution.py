"""chat-evolution 幂等回执的失败语义。

历史 bug:回执先提交 'processing',真正的处理在事务外;中途失败回执永久停在
'processing',平台重试被当 duplicate 直接吞掉——"安全重试"实现成了"首错即黑洞"。
修复后:失败撤回回执让重试真的重做;硬崩溃遗留的 'processing' 行被接管重做。
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from anima_world.world.auth import issue_membership_claim

SERVICE_TOKEN = "svc-token"
CLAIM_SECRET = "claim-secret"


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from anima_world.__main__ import build_serve_scheduler
    from anima_world.world.app import create_app

    scheduler = build_serve_scheduler(db_path=tmp_path / "w.db", force_mock_llm=True)
    app = create_app(
        scheduler,
        run_loop=False,
        platform_service_credentials=(SERVICE_TOKEN,),
        membership_claim_secret=CLAIM_SECRET,
    )
    return scheduler, TestClient(app, raise_server_exceptions=False)


def _headers(membership_id="mem-1"):
    claim = issue_membership_claim(
        CLAIM_SECRET,
        membership_id=membership_id,
        world_id="legacy",
        role="player",
        instance_id="legacy",
    )
    return {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Cyberworld-Membership": claim,
    }


def _body(delivery_id, agent_id="夏"):
    return {
        "delivery_id": delivery_id,
        "agent_id": agent_id,
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ],
    }


def test_failed_delivery_is_retryable_not_a_black_hole(tmp_path, monkeypatch):
    scheduler, client = _client(tmp_path)
    try:
        from anima_world.chat_session import ChatSessionManager

        async def _boom(self, conversation_id):
            raise RuntimeError("close blew up")

        monkeypatch.setattr(ChatSessionManager, "close_conversation", _boom)
        first = client.post(
            "/internal/v1/chat-evolution", json=_body("d-1"), headers=_headers()
        )
        assert first.status_code == 500

        monkeypatch.undo()
        retry = client.post(
            "/internal/v1/chat-evolution", json=_body("d-1"), headers=_headers()
        )
        assert retry.status_code == 200
        assert retry.json()["status"] == "applied"
        assert retry.json()["duplicate"] is False, "失败后的重试必须真的重做,不是回显"

        again = client.post(
            "/internal/v1/chat-evolution", json=_body("d-1"), headers=_headers()
        )
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
    finally:
        scheduler.stop()


def test_crash_leftover_processing_receipt_is_taken_over(tmp_path):
    scheduler, client = _client(tmp_path)
    try:
        body = _body("d-crashed")
        canonical = json.dumps(
            {"agent_id": body["agent_id"], "messages": body["messages"]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
        conn = scheduler.event_log.conn
        with scheduler._lock:
            conn.execute(
                """INSERT INTO world_chat_evolution_receipts
                   (delivery_id, membership_id, world_id, instance_id, agent_id,
                    payload_hash, status, created_at)
                   VALUES (?, ?, 'legacy', 'legacy', ?, ?, 'processing', ?)""",
                ("d-crashed", "mem-1", body["agent_id"], payload_hash, int(time.time())),
            )
            conn.commit()

        resp = client.post(
            "/internal/v1/chat-evolution", json=body, headers=_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["duplicate"] is False
    finally:
        scheduler.stop()


def test_provenance_conflict_still_409s(tmp_path):
    scheduler, client = _client(tmp_path)
    try:
        ok = client.post(
            "/internal/v1/chat-evolution", json=_body("d-2"), headers=_headers()
        )
        assert ok.status_code == 200
        tampered = _body("d-2")
        tampered["messages"][0]["content"] = "换了内容"
        conflict = client.post(
            "/internal/v1/chat-evolution", json=tampered, headers=_headers()
        )
        assert conflict.status_code == 409
    finally:
        scheduler.stop()
