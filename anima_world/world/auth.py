"""Small, dependency-free trust contract between the client and a world runtime.

This module deliberately knows nothing about platform persistence or services.  It
only protects and validates the membership facts a platform sends over HTTP.

**此线格式已永久冻结(2026-07)。**

claim 是身份断言,不是行李箱:它只把一个 membership_id 可信地绑定到一次请求上,
任何新的数据需求一律走请求体等其他通道,因此它永远不需要长新字段。禁止增删字段、
禁止更换序列化方式或签名算法;若密码学层面出现必须更换的黑天鹅,规则是并行加一个
新 header(如 X-Cyberworld-Membership-V2),本格式原样保留——契约从不修改,只会有
新契约在旁边出生。冻结由两道机器强制:验签对未知字段直接拒收(见下),以及
tests/test_claim_freeze.py 的黄金向量(与网站仓库共享同一组向量,逐字节钉死输出)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


MAX_CLAIM_LIFETIME_SECONDS = 300

_ALLOWED_CLAIM_KEYS = frozenset({"membership_id", "world_id", "role", "instance_id", "iat", "exp"})


class MembershipClaimError(ValueError):
    """A membership claim is malformed, untrusted, expired, or misaddressed."""


@dataclass(frozen=True)
class MembershipClaim:
    membership_id: str
    world_id: str
    role: str
    instance_id: str
    issued_at: int
    expires_at: int


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_membership_claim(
    secret: str,
    *,
    membership_id: str,
    world_id: str,
    role: str,
    instance_id: str,
    ttl_seconds: int = 60,
    expires_at: int | None = None,
    now: int | None = None,
) -> str:
    """Return a compact HMAC-protected membership claim for one runtime audience."""
    if not secret:
        raise ValueError("membership claim secret cannot be empty")
    issued_at = int(time.time()) if now is None else int(now)
    expiry = issued_at + int(ttl_seconds) if expires_at is None else int(expires_at)
    payload = {
        "membership_id": membership_id,
        "world_id": world_id,
        "role": role,
        "instance_id": instance_id,
        "iat": issued_at,
        "exp": expiry,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    encoded = _encode(raw)
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_encode(signature)}"


def verify_membership_claim(
    token: str,
    secret: str,
    *,
    world_id: str,
    instance_id: str,
    now: int | None = None,
) -> MembershipClaim:
    """Verify signature, lifetime, required identity facts, and runtime audience."""
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        decoded_signature = _decode(supplied_signature)
        if (
            not hmac.compare_digest(_encode(decoded_signature), supplied_signature)
            or not hmac.compare_digest(decoded_signature, expected)
        ):
            raise MembershipClaimError("invalid membership claim signature")
        payload: dict[str, Any] = json.loads(_decode(encoded))
    except MembershipClaimError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise MembershipClaimError("malformed membership claim") from exc

    unexpected = set(payload.keys()) - _ALLOWED_CLAIM_KEYS
    if unexpected:
        raise MembershipClaimError("legacy claim rejected: contains removed fields")
    text_fields = ("membership_id", "world_id", "role", "instance_id")
    if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in text_fields):
        raise MembershipClaimError("membership claim is missing identity fields")
    if payload["world_id"] != world_id or payload["instance_id"] != instance_id:
        raise MembershipClaimError("membership claim audience mismatch")
    try:
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MembershipClaimError("membership claim is missing lifetime") from exc
    current = int(time.time()) if now is None else int(now)
    if expires_at <= current:
        raise MembershipClaimError("membership claim expired")
    if expires_at <= issued_at or expires_at - issued_at > MAX_CLAIM_LIFETIME_SECONDS:
        raise MembershipClaimError("membership claim lifetime is not short-lived")
    if issued_at > current + 30:
        raise MembershipClaimError("membership claim issued in the future")
    return MembershipClaim(
        membership_id=payload["membership_id"],
        world_id=payload["world_id"],
        role=payload["role"],
        instance_id=payload["instance_id"],
        issued_at=issued_at,
        expires_at=expires_at,
    )


def service_credential_matches(supplied: str, accepted: tuple[str, ...]) -> bool:
    """Compare credentials without leaking which configured credential matched."""
    return bool(supplied) and any(
        hmac.compare_digest(supplied.encode(), candidate.encode()) for candidate in accepted
    )
