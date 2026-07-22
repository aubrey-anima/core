# membership claim 线格式的永冻测试。
#
# 这里的 token 是黄金向量:固定密钥 + 固定 payload + 固定时间戳 → 逐字节钉死的输出。
# 网站仓库(backend/app/services/claims.py)持有同一组向量。任何让这些断言变红的改动
# ——字段增删、序列化空格、base64 padding、换哈希——都是在破坏已冻结的跨仓库契约,
# 正确做法永远是并行加一个新 header(如 X-Cyberworld-Membership-V2),而不是修改本格式。

import base64
import hashlib
import hmac
import json

import pytest

from anima_world.world.auth import (
    MAX_CLAIM_LIFETIME_SECONDS,
    MembershipClaimError,
    issue_membership_claim,
    verify_membership_claim,
)

SECRET = "golden-vector-secret"
NOW = 1_700_000_000

# 向量 1:ASCII 字段,ttl=60
GOLDEN_TOKEN = (
    "eyJleHAiOjE3MDAwMDAwNjAsImlhdCI6MTcwMDAwMDAwMCwiaW5zdGFuY2VfaWQiOiJpbnN0LTAwMDEiLCJtZW1iZXJzaGlwX2lkIjoibWVtLTAwMDEiLCJyb2xlIjoicGxheWVyIiwid29ybGRfaWQiOiJ3LWRlbW8ifQ"
    ".ta37aDdY4Kz1T_8pueD1IgNDKLoiSC8kiO6hs2QOZsQ"
)
GOLDEN_PAYLOAD = (
    '{"exp":1700000060,"iat":1700000000,"instance_id":"inst-0001",'
    '"membership_id":"mem-0001","role":"player","world_id":"w-demo"}'
)

# 向量 2:role 含中文(钉死 ensure_ascii=False),ttl 顶到上限 300
GOLDEN_TOKEN_CJK = (
    "eyJleHAiOjE3MDAwMDAzMDAsImlhdCI6MTcwMDAwMDAwMCwiaW5zdGFuY2VfaWQiOiJpbnN0LTAwMDEiLCJtZW1iZXJzaGlwX2lkIjoibWVtLTAwMDIiLCJyb2xlIjoi6K6_5a6iIiwid29ybGRfaWQiOiJ3LWRlbW8ifQ"
    ".nkMDM16pYSsavCETl2dwah-5cHnZFd8_VDdqgRu76y0"
)


def _issue_golden() -> str:
    return issue_membership_claim(
        SECRET,
        membership_id="mem-0001",
        world_id="w-demo",
        role="player",
        instance_id="inst-0001",
        ttl_seconds=60,
        now=NOW,
    )


def test_golden_vector_is_byte_identical():
    assert _issue_golden() == GOLDEN_TOKEN


def test_golden_vector_cjk_is_byte_identical():
    token = issue_membership_claim(
        SECRET,
        membership_id="mem-0002",
        world_id="w-demo",
        role="访客",
        instance_id="inst-0001",
        ttl_seconds=300,
        now=NOW,
    )
    assert token == GOLDEN_TOKEN_CJK


def test_payload_canonicalization_is_frozen():
    """payload 必须是 sort_keys + 紧凑分隔符 + 无 padding base64url——逐字符钉死。"""
    encoded = GOLDEN_TOKEN.split(".", 1)[0]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    assert raw.decode("utf-8") == GOLDEN_PAYLOAD
    assert "=" not in GOLDEN_TOKEN  # 两段都不许带 padding


def test_signature_is_hmac_sha256_over_encoded_text():
    """用测试自己的实现独立复算签名——与被测代码零共享路径。"""
    encoded, signature = GOLDEN_TOKEN.split(".", 1)
    expected = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    recomputed = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
    assert recomputed == signature


def test_golden_vector_round_trips_through_verify():
    claim = verify_membership_claim(
        GOLDEN_TOKEN, SECRET, world_id="w-demo", instance_id="inst-0001", now=NOW + 30
    )
    assert claim.membership_id == "mem-0001"
    assert claim.role == "player"
    assert claim.issued_at == NOW
    assert claim.expires_at == NOW + 60


def test_claim_key_set_is_frozen():
    """字段集是契约本体:恰好这 6 个,增删任何一个都会让本测试变红。"""
    encoded = GOLDEN_TOKEN.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert set(payload) == {"membership_id", "world_id", "role", "instance_id", "iat", "exp"}


def test_max_lifetime_constant_is_frozen():
    assert MAX_CLAIM_LIFETIME_SECONDS == 300


def test_extra_field_is_rejected_even_with_valid_signature():
    """扩展格式的唯一后果是被拒收——冻结由验签方机器强制,不靠自觉。"""
    payload = json.loads(GOLDEN_PAYLOAD)
    payload["display_name"] = "smuggled"
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    forged = f"{encoded}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"
    with pytest.raises(MembershipClaimError, match="removed fields"):
        verify_membership_claim(
            forged, SECRET, world_id="w-demo", instance_id="inst-0001", now=NOW + 30
        )


def test_tampered_payload_is_rejected():
    encoded, signature = GOLDEN_TOKEN.split(".", 1)
    tampered_payload = GOLDEN_PAYLOAD.replace("mem-0001", "mem-9999").encode()
    tampered = base64.urlsafe_b64encode(tampered_payload).rstrip(b"=").decode("ascii")
    with pytest.raises(MembershipClaimError, match="signature"):
        verify_membership_claim(
            f"{tampered}.{signature}", SECRET, world_id="w-demo", instance_id="inst-0001", now=NOW + 30
        )
