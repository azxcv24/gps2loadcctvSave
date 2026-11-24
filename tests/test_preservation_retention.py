"""보존 요청 준비·상태 관리와 보존 기간 만료·삭제 검증."""

from datetime import timedelta

import pytest

from gps2cctv import catalog, incidents, preservation, retention
from gps2cctv.candidates import find_candidates
from gps2cctv.errors import PolicyDeniedError
from gps2cctv.routeio import read_route

from .conftest import ACCIDENT, FIXTURES, NOW


@pytest.fixture
def run(conn, settings, city):
    inc = incidents.create_incident(
        conn, occurred_at=ACCIDENT, reason="합성 테스트 사건", now=NOW, settings=settings
    )
    incidents.import_route(conn, inc.id, read_route(FIXTURES / "route.csv"), NOW, settings)
    return find_candidates(conn, inc.id, NOW, settings)


def pick(run, *, needs_review: bool):
    return next(c for c in run.candidates if c.needs_manual_review == needs_review)


class TestPrepare:
    def test_draft_from_candidate(self, conn, settings, run):
        cand = pick(run, needs_review=False)
        req = preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        assert req.status == "draft"
        assert (req.requested_start, req.requested_end) == (
            cand.request_window_start,
            cand.request_window_end,
        )
        assert req.calculation_snapshot["calculation_method"] == cand.calculation_method

    def test_duplicate_active_request_is_refused(self, conn, settings, run):
        cand = pick(run, needs_review=False)
        first = preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        with pytest.raises(preservation.DuplicateRequestError) as info:
            preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        assert info.value.existing_id == first.id
        # 철회하면 다시 준비할 수 있다
        preservation.update_status(conn, first.id, "withdrawn", now=NOW, settings=settings)
        assert preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)

    def test_low_confidence_requires_review_flag(self, conn, settings, run):
        cand = pick(run, needs_review=True)
        with pytest.raises(preservation.PreservationError, match="--reviewed"):
            preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        req = preservation.prepare_request(conn, cand.id, now=NOW, settings=settings, reviewed=True)
        assert req.calculation_snapshot["manual_review_acknowledged"] is True

    def test_policy_denial_blocks_request(self, conn, settings, run):
        catalog.review_source(
            conn,
            "synthetic-city",
            actions=["catalog_sync", "catalog_lookup"],
            terms_ref="v2",
            valid_days=30,
            reviewer="tester",
            notes=None,
            now=NOW + timedelta(seconds=1),
            settings=settings,
        )
        cand = pick(run, needs_review=False)
        with pytest.raises(PolicyDeniedError) as info:
            preservation.prepare_request(
                conn, cand.id, now=NOW + timedelta(seconds=2), settings=settings
            )
        assert info.value.reason_code == "action_not_allowed"
        assert preservation.list_requests(conn, run.incident_id) == []

    def test_expired_policy_blocks_request(self, conn, settings, run):
        cand = pick(run, needs_review=False)
        with pytest.raises(PolicyDeniedError) as info:
            preservation.prepare_request(
                conn, cand.id, now=NOW + timedelta(days=31), settings=settings
            )
        assert info.value.reason_code == "policy_snapshot_expired"

    def test_request_survives_candidate_recalculation(self, conn, settings, run):
        cand = pick(run, needs_review=False)
        req = preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        find_candidates(conn, run.incident_id, NOW, settings)
        again = preservation.get_request(conn, req.id)
        assert again.candidate_id is None
        assert again.requested_start == req.requested_start
        assert "CAM-A" in preservation.render_draft(conn, again, settings)


class TestStatus:
    def test_lifecycle_and_incident_status(self, conn, settings, run):
        cand = pick(run, needs_review=False)
        req = preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        req = preservation.update_status(
            conn, req.id, "submitted", now=NOW, settings=settings, provider_request_id="2026-0001"
        )
        assert req.submitted_at == NOW and req.provider_request_id == "2026-0001"
        assert incidents.get_incident(conn, run.incident_id).status == "preservation_requested"
        req = preservation.update_status(
            conn, req.id, "fulfilled", now=NOW, settings=settings, response_reference="회신-77"
        )
        assert req.responded_at == NOW and req.response_reference == "회신-77"

    def test_invalid_transition(self, conn, settings, run):
        req = preservation.prepare_request(
            conn, pick(run, needs_review=False).id, now=NOW, settings=settings
        )
        with pytest.raises(preservation.PreservationError):
            preservation.update_status(conn, req.id, "fulfilled", now=NOW, settings=settings)

    def test_reference_format_is_validated(self, conn, settings, run):
        req = preservation.prepare_request(
            conn, pick(run, needs_review=False).id, now=NOW, settings=settings
        )
        with pytest.raises(preservation.PreservationError):
            preservation.update_status(
                conn,
                req.id,
                "submitted",
                now=NOW,
                settings=settings,
                provider_request_id="x" * 101,
            )


class TestDraftWording:
    def test_draft_contains_disclaimer_and_no_contact(self, conn, settings, run):
        req = preservation.prepare_request(
            conn, pick(run, needs_review=False).id, now=NOW, settings=settings
        )
        text = preservation.render_draft(conn, req, settings)
        assert "식별한 결과가 아니며" in text
        assert "CCTV 화면 표시 시각 기준" in text
        assert "(직접 기재)" in text


class TestRetention:
    def test_delete_incident_removes_everything(self, conn, settings, run):
        cand = pick(run, needs_review=False)
        preservation.prepare_request(conn, cand.id, now=NOW, settings=settings)
        report = retention.delete_incident(conn, run.incident_id, NOW, settings)
        assert report.verified
        assert report.route_points > 0 and report.candidates > 0
        assert report.preservation_requests == 1
        assert retention.verify_deleted(conn, [run.incident_id]) == {
            "incident": 0,
            "route_point": 0,
            "camera_candidate": 0,
            "preservation_request": 0,
            "live_event": 0,
        }

    def test_purge_respects_retention(self, conn, settings, run):
        inc = incidents.get_incident(conn, run.incident_id)
        before = retention.purge_expired(conn, inc.retention_until - timedelta(seconds=1), settings)
        assert before.incident_ids == []
        dry = retention.purge_expired(conn, inc.retention_until, settings, dry_run=True)
        assert dry.incident_ids == [inc.id]
        assert conn.execute("SELECT COUNT(*) FROM incident").fetchone()[0] == 1
        done = retention.purge_expired(conn, inc.retention_until, settings)
        assert done.incident_ids == [inc.id] and done.verified
        assert conn.execute("SELECT COUNT(*) FROM route_point").fetchone()[0] == 0
        purge_rows = conn.execute(
            "SELECT resource_id FROM audit_event WHERE action = 'incident.purge'"
        ).fetchall()
        assert [r[0] for r in purge_rows] == [inc.id]

    def test_audit_events_expire_too(self, conn, settings, run):
        later = NOW + timedelta(days=settings.audit_retention_days + 1)
        report = retention.purge_expired(conn, later, settings)
        assert report.audit_events > 0
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_event")]
        # 남는 것은 이번 삭제 작업 자체의 기록뿐
        assert set(actions) <= {"incident.purge", "retention.purge"}

    def test_every_incident_record_has_retention(self, conn, settings, run):
        assert (
            conn.execute("SELECT COUNT(*) FROM incident WHERE retention_until IS NULL").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_event WHERE retention_until IS NULL"
            ).fetchone()[0]
            == 0
        )
