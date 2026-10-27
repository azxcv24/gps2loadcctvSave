"""정책 검증: 불명확·만료·미허용 상태에서 반드시 거부(fail closed)."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from gps2cctv import catalog, policy
from gps2cctv.errors import PolicyDeniedError

from .conftest import ALL_ACTIONS, FIXTURES, NOW


def _register(conn, settings, tmp_path, now=NOW, **overrides):
    data = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    source = data["sources"][0] | overrides
    source["adapter"] = {"type": "json_file", "path": str(FIXTURES / "cameras_city.json")}
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    return catalog.register_sources(conn, path, now, settings)[0]


def _review(conn, settings, source_id, actions=ALL_ACTIONS, now=NOW, valid_days=30):
    return catalog.review_source(
        conn,
        source_id,
        actions=actions,
        terms_ref="v1",
        valid_days=valid_days,
        reviewer="tester",
        notes=None,
        now=now,
        settings=settings,
    )


def decide(conn, settings, action="catalog_lookup", now=NOW, source_id="synthetic-city"):
    return policy.evaluate(conn, source_id, action, now, settings)


class TestRegistration:
    def test_production_source_outside_allowlist_is_rejected(self, conn, settings, tmp_path):
        with pytest.raises(catalog.CatalogError, match="허용 목록"):
            _register(
                conn,
                settings,
                tmp_path,
                environment="production",
                official_endpoint="https://cctv.example.com/api",
            )

    def test_production_source_in_allowlist_is_accepted(self, conn, settings, tmp_path):
        allowed = replace(settings, allowed_hosts=(".example.com",))
        assert (
            _register(
                conn,
                allowed,
                tmp_path,
                environment="production",
                official_endpoint="https://cctv.example.com/api",
            )
            == "synthetic-city"
        )

    def test_http_endpoint_is_rejected(self, conn, settings, tmp_path):
        with pytest.raises(catalog.CatalogError, match="HTTPS"):
            _register(
                conn, settings, tmp_path, official_endpoint="http://city-cctv.example.invalid/api"
            )

    def test_synthetic_source_must_use_reserved_domain(self, conn, settings, tmp_path):
        with pytest.raises(catalog.CatalogError, match="예약 도메인"):
            _register(conn, settings, tmp_path, official_endpoint="https://real-site.com/api")

    def test_missing_terms_is_rejected(self, conn, settings, tmp_path):
        with pytest.raises(catalog.CatalogError):
            _register(conn, settings, tmp_path, terms_url="")

    def test_unknown_action_cannot_be_reviewed(self, conn, settings, tmp_path):
        _register(conn, settings, tmp_path)
        with pytest.raises(catalog.CatalogError, match="알 수 없는 행위"):
            _review(conn, settings, "synthetic-city", actions=["face_recognition"])


class TestDecisions:
    @pytest.fixture
    def source(self, conn, settings, tmp_path):
        return _register(conn, settings, tmp_path)

    def test_unknown_source(self, conn, settings):
        assert decide(conn, settings, source_id="nope").reason_code == "source_not_found"

    def test_no_review_is_denied(self, conn, settings, source):
        assert decide(conn, settings).reason_code == "policy_snapshot_missing"

    def test_reviewed_action_is_allowed(self, conn, settings, source):
        _review(conn, settings, source)
        result = decide(conn, settings)
        assert result.allowed and result.snapshot_id

    def test_action_not_in_review_is_denied(self, conn, settings, source):
        _review(conn, settings, source, actions=["catalog_sync"])
        assert decide(conn, settings).reason_code == "action_not_allowed"

    def test_expired_review_is_denied(self, conn, settings, source):
        _review(conn, settings, source, valid_days=1)
        later = NOW + timedelta(days=2)
        assert decide(conn, settings, now=later).reason_code == "policy_snapshot_expired"

    def test_future_review_is_denied(self, conn, settings, source):
        _review(conn, settings, source, now=NOW + timedelta(days=1))
        assert decide(conn, settings).reason_code == "policy_snapshot_not_yet_valid"

    def test_disabled_source_is_denied(self, conn, settings, source):
        _review(conn, settings, source)
        catalog.set_source_enabled(conn, source, False, NOW, settings)
        assert decide(conn, settings).reason_code == "source_disabled"

    def test_media_capture_needs_video_capability_and_explicit_review(
        self, conn, settings, tmp_path
    ):
        # 기본 합성 소스는 video 기능이 없음
        source = _register(conn, settings, tmp_path)
        _review(conn, settings, source, actions=[*ALL_ACTIONS, "media_capture"])
        assert decide(conn, settings, "media_capture").reason_code == "capability_missing"
        # video 기능이 있어도 검토에서 허용하지 않으면 거부
        later = NOW + timedelta(seconds=1)
        _register(conn, settings, tmp_path, now=later, capabilities=["catalog", "video"])
        _review(conn, settings, source, now=later + timedelta(seconds=1))
        result = decide(conn, settings, "media_capture", now=later + timedelta(seconds=2))
        assert result.reason_code == "action_not_allowed"
        _review(conn, settings, source, actions=["media_capture"], now=later + timedelta(seconds=3))
        assert decide(conn, settings, "media_capture", now=later + timedelta(seconds=4)).allowed

    def test_capability_missing(self, conn, settings, tmp_path):
        source = _register(conn, settings, tmp_path, capabilities=["catalog"])
        _review(conn, settings, source)
        result = decide(conn, settings, "preservation_request_prepare")
        assert result.reason_code == "capability_missing"

    def test_material_change_requires_new_review(self, conn, settings, tmp_path, source):
        _review(conn, settings, source)
        later = NOW + timedelta(seconds=1)
        _register(
            conn,
            settings,
            tmp_path,
            now=later,
            terms_url="https://city-cctv.example.invalid/terms-v2",
        )
        assert decide(conn, settings, now=later).reason_code == "policy_snapshot_outdated"
        _review(conn, settings, source, now=NOW + timedelta(seconds=2))
        assert decide(conn, settings, now=NOW + timedelta(seconds=3)).allowed

    def test_allowlist_removal_blocks_existing_production_source(self, conn, settings, tmp_path):
        allowed = replace(settings, allowed_hosts=("cctv.example.com",))
        source = _register(
            conn,
            allowed,
            tmp_path,
            environment="production",
            official_endpoint="https://cctv.example.com/api",
        )
        _review(conn, allowed, source)
        assert policy.evaluate(conn, source, "catalog_lookup", NOW, allowed).allowed
        result = policy.evaluate(conn, source, "catalog_lookup", NOW, settings)
        assert result.reason_code == "endpoint_not_allowlisted"


def test_sync_is_refused_without_review_and_audited(conn, settings):
    catalog.register_sources(conn, FIXTURES / "sources.json", NOW, settings)
    with pytest.raises(PolicyDeniedError) as info:
        catalog.sync_cameras(conn, "synthetic-city", NOW, settings)
    assert info.value.reason_code == "policy_snapshot_missing"
    assert conn.execute("SELECT COUNT(*) FROM camera").fetchone()[0] == 0
    row = conn.execute(
        "SELECT outcome, reason_code FROM audit_event WHERE action = 'policy.catalog_sync'"
    ).fetchone()
    assert tuple(row) == ("denied", "policy_snapshot_missing")


@pytest.mark.parametrize(
    ("host", "allowed", "expected"),
    [
        ("data.example.go.kr", ("data.example.go.kr",), True),
        ("evil-data.example.go.kr", ("data.example.go.kr",), False),
        ("a.example.go.kr", (".example.go.kr",), True),
        ("example.go.kr", (".example.go.kr",), False),
        ("example.go.kr.evil.com", (".example.go.kr",), False),
    ],
)
def test_host_allowlist_matching(host, allowed, expected):
    assert policy.host_allowed(host, allowed) is expected
