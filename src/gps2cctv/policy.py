"""정책·권한 검증(Policy & Permission Guard).

모든 판정은 실패 시 거부(fail closed)한다. 하나라도 확인되지 않으면 허용하지 않는다.

허용 조건:
1. 소스가 등록되어 있고 활성 상태
2. 행위가 알려진 행위이며, 소스가 해당 기능(capability)을 제공
3. 공식 엔드포인트·약관 URL이 HTTPS이고, 실제(production) 소스는 호스트 허용 목록에 포함
   (합성 소스는 실제 요청이 불가능한 예약 도메인 .invalid/.test/.example 만 허용)
4. 가장 최근 정책 검토 기록(policy snapshot)이 있고, 유효 기간 안이며, 해당 행위를 허용
5. 영상 녹화(media_capture)는 소스가 video 기능을 제공하고 정책 검토에서 명시적으로 허용한 경우만
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from . import audit
from .config import Settings
from .timeutil import from_db

# 행위 → 소스가 제공해야 하는 기능
ACTIONS: dict[str, str] = {
    "catalog_sync": "catalog",
    "catalog_lookup": "catalog",
    "preservation_request_prepare": "preservation_request",
    "media_capture": "video",
}
REVIEWABLE_ACTIONS = frozenset(ACTIONS)
CAPABILITIES = frozenset({"catalog", "metadata", "video", "preservation_request"})
SYNTHETIC_TLDS = (".invalid", ".test", ".example")

REASON_TEXT: dict[str, str] = {
    "policy_ok": "정책 검증 통과",
    "source_not_found": "등록되지 않은 소스",
    "source_disabled": "비활성화된 소스",
    "unknown_action": "알 수 없는 행위",
    "capability_missing": "소스가 해당 기능을 제공하지 않음",
    "endpoint_invalid": "공식 엔드포인트 주소가 올바르지 않음",
    "endpoint_not_https": "공식 엔드포인트가 HTTPS가 아님",
    "endpoint_not_allowlisted": "공식 엔드포인트 호스트가 허용 목록에 없음",
    "synthetic_endpoint_not_reserved": "합성 소스가 예약 도메인(.invalid/.test/.example)을 쓰지 않음",
    "terms_url_invalid": "약관 URL이 없거나 HTTPS가 아님",
    "policy_snapshot_missing": "정책 검토 기록 없음",
    "policy_snapshot_not_yet_valid": "정책 검토 시각이 현재보다 미래임",
    "policy_snapshot_outdated": "소스 정보(엔드포인트·약관·기능)가 바뀐 뒤 재검토하지 않음",
    "policy_snapshot_expired": "정책 검토 유효 기간 만료",
    "action_not_allowed": "정책 검토에서 허용하지 않은 행위",
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason_code: str
    source_id: str
    action: str
    snapshot_id: str | None = None
    valid_until: datetime | None = None

    @property
    def reason_text(self) -> str:
        return REASON_TEXT.get(self.reason_code, self.reason_code)


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """정확히 같은 호스트, 또는 허용 항목이 '.'으로 시작하면 그 하위 도메인만 허용."""
    host = host.lower().rstrip(".")
    for entry in allowed_hosts:
        entry = entry.lower().rstrip(".")
        if entry.startswith("."):
            if host.endswith(entry) and len(host) > len(entry):
                return True
        elif host == entry:
            return True
    return False


def endpoint_problem(environment: str, url: str, settings: Settings) -> str | None:
    """엔드포인트가 정책을 만족하지 않으면 사유 코드를, 만족하면 None을 반환."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "endpoint_invalid"
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password:
        return "endpoint_invalid"
    if parsed.scheme != "https":
        return "endpoint_not_https"
    if environment == "synthetic":
        if not host.endswith(SYNTHETIC_TLDS):
            return "synthetic_endpoint_not_reserved"
        return None
    if not host_allowed(host, settings.allowed_hosts):
        return "endpoint_not_allowlisted"
    return None


def terms_problem(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "terms_url_invalid"
    if parsed.scheme != "https" or not parsed.hostname:
        return "terms_url_invalid"
    return None


def latest_snapshot(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM policy_snapshot WHERE source_id = ?
        ORDER BY checked_at DESC, rowid DESC LIMIT 1
        """,
        (source_id,),
    ).fetchone()


def evaluate(
    conn: sqlite3.Connection, source_id: str, action: str, now: datetime, settings: Settings
) -> PolicyDecision:
    """감사 기록 없이 정책만 판정한다."""

    def deny(code: str, snapshot_id: str | None = None) -> PolicyDecision:
        return PolicyDecision(False, code, source_id, action, snapshot_id)

    source = conn.execute("SELECT * FROM cctv_source WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        return deny("source_not_found")
    if not source["enabled"]:
        return deny("source_disabled")
    if action not in ACTIONS:
        return deny("unknown_action")
    if ACTIONS[action] not in set(json.loads(source["capabilities"])):
        return deny("capability_missing")
    if source["environment"] != "owned":
        # 본인 소유 장비(owned)는 외부 제공처가 아니므로 엔드포인트·약관 검사를 하지 않는다.
        problem = endpoint_problem(source["environment"], source["official_endpoint"], settings)
        if problem:
            return deny(problem)
        problem = terms_problem(source["terms_url"])
        if problem:
            return deny(problem)
    snapshot = latest_snapshot(conn, source_id)
    if snapshot is None:
        return deny("policy_snapshot_missing")
    checked_at = from_db(snapshot["checked_at"])
    if checked_at > now:
        return deny("policy_snapshot_not_yet_valid", snapshot["id"])
    if checked_at < from_db(source["material_changed_at"]):
        return deny("policy_snapshot_outdated", snapshot["id"])
    valid_until = from_db(snapshot["valid_until"])
    if now > valid_until:
        return deny("policy_snapshot_expired", snapshot["id"])
    if action not in set(json.loads(snapshot["allowed_actions"])):
        return deny("action_not_allowed", snapshot["id"])
    return PolicyDecision(True, "policy_ok", source_id, action, snapshot["id"], valid_until)


def check_policy(
    conn: sqlite3.Connection, source_id: str, action: str, now: datetime, settings: Settings
) -> PolicyDecision:
    """정책을 판정하고 결과를 감사 로그에 남긴다."""
    decision = evaluate(conn, source_id, action, now, settings)
    audit.record(
        conn,
        action=f"policy.{action}" if action in ACTIONS else "policy.unknown",
        resource_type="cctv_source",
        resource_id=audit.safe_id(source_id),
        outcome="success" if decision.allowed else "denied",
        reason_code=decision.reason_code,
        now=now,
        settings=settings,
    )
    return decision
