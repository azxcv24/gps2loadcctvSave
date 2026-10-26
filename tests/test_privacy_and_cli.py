"""개인정보·오해 방지 검증과 CLI 통합 테스트."""

import json
import os
import re
import stat

import pytest

from gps2cctv import audit, incidents
from gps2cctv.candidates import find_candidates
from gps2cctv.cli import main
from gps2cctv.report import build_report, render_text
from gps2cctv.routeio import RouteFileError, read_route

from .conftest import ACCIDENT, FIXTURES, NOW

COORD_RE = re.compile(r"\b3[67]\.\d{3,}|\b12[67]\.\d{3,}")
SECRET_REASON = "비공개-사유-문자열"


@pytest.fixture
def full_run(conn, settings, city):
    inc = incidents.create_incident(
        conn,
        occurred_at=ACCIDENT,
        reason=SECRET_REASON,
        now=NOW,
        settings=settings,
        latitude=37.04,
        longitude=127.06,
    )
    incidents.import_route(conn, inc.id, read_route(FIXTURES / "route.csv"), NOW, settings)
    find_candidates(conn, inc.id, NOW, settings)
    return inc


class TestPrivacy:
    def test_audit_log_has_no_coordinates_or_free_text(self, conn, full_run):
        dump = "\n".join(
            " ".join(str(v) for v in row) for row in conn.execute("SELECT * FROM audit_event")
        )
        assert not COORD_RE.search(dump)
        assert SECRET_REASON not in dump

    def test_audit_rejects_free_text(self, conn, settings):
        with pytest.raises(ValueError):
            audit.record(
                conn,
                action="x",
                resource_type="y",
                reason_code="free text!",
                now=NOW,
                settings=settings,
            )
        with pytest.raises(TypeError):
            audit.record(
                conn,
                action="x",
                resource_type="y",
                detail={"lat": 37.1234},
                now=NOW,
                settings=settings,
            )

    def test_report_has_no_route_coordinates(self, conn, settings, full_run):
        report = build_report(conn, full_run.id, NOW, settings)
        assert not COORD_RE.search(json.dumps(report, ensure_ascii=False))
        assert not COORD_RE.search(render_text(report, conn, settings))


class TestMisinterpretationGuard:
    def test_report_starts_with_disclaimer(self, conn, settings, full_run):
        report = build_report(conn, full_run.id, NOW, settings)
        text = render_text(report, conn, settings)
        assert text.startswith("[안내]")
        assert "식별한 결과가 아니며" in report["disclaimer"]
        # 신뢰도는 '시간 범위' 신뢰도로만 표현한다
        assert all("time_window_confidence" in c for c in report["candidates"])
        assert "시간 범위 신뢰도" in text

    def test_no_identification_claims(self, conn, settings, full_run):
        text = render_text(build_report(conn, full_run.id, NOW, settings), conn, settings)
        for phrase in ("식별됨", "확인됨", "동일 차량", "본인 차량", "추적 결과", "identified"):
            assert phrase not in text


class TestRouteFiles:
    def test_naive_csv_time_requires_timezone(self, tmp_path):
        path = tmp_path / "r.csv"
        path.write_text("recorded_at,latitude,longitude\n2026-10-26T14:00:00,37,127\n", "utf-8")
        with pytest.raises(RouteFileError, match="시간대"):
            read_route(path)
        assert read_route(path, "Asia/Seoul")[0].recorded_at.hour == 5

    def test_missing_columns(self, tmp_path):
        path = tmp_path / "r.csv"
        path.write_text("time,lat,lon\n", "utf-8")
        with pytest.raises(RouteFileError, match="필수 열"):
            read_route(path)

    def test_bad_coordinate_reports_line(self, tmp_path):
        path = tmp_path / "r.csv"
        path.write_text("recorded_at,latitude,longitude\n2026-10-26T05:00:00Z,99,127\n", "utf-8")
        with pytest.raises(RouteFileError, match=r"r\.csv:2"):
            read_route(path)

    def test_gpx(self, tmp_path):
        path = tmp_path / "r.gpx"
        path.write_text(
            '<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            "<trk><trkseg>"
            '<trkpt lat="37.0001" lon="127.0001"><time>2026-10-26T05:00:02Z</time>'
            "<hdop>1.2</hdop></trkpt>"
            '<trkpt lat="37.0000" lon="127.0000"><time>2026-10-26T05:00:00Z</time></trkpt>'
            "</trkseg></trk></gpx>",
            "utf-8",
        )
        points = read_route(path)
        assert [p.recorded_at.second for p in points] == [0, 2]
        assert points[1].accuracy_m == pytest.approx(6.0)
        assert points[0].accuracy_m is None


class TestCli:
    def run(self, db, *args):
        return main(["--db", str(db), *args])

    def test_end_to_end(self, tmp_path, capsys):
        db = tmp_path / "data" / "t.sqlite3"
        assert self.run(db, "init") == 0
        assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
        assert self.run(db, "source", "register", str(FIXTURES / "sources.json")) == 0
        # 정책 검토 전에는 동기화 거부(종료 코드 3)
        assert self.run(db, "camera", "sync", "synthetic-city") == 3
        assert (
            self.run(
                db,
                "source",
                "review",
                "synthetic-city",
                "--actions",
                "catalog_sync,catalog_lookup,preservation_request_prepare",
                "--terms-ref",
                "v1",
                "--reviewer",
                "tester",
            )
            == 0
        )
        assert self.run(db, "camera", "sync", "synthetic-city") == 0
        assert self.run(db, "source", "check", "synthetic-city", "media_capture") == 3
        capsys.readouterr()

        assert (
            self.run(
                db,
                "incident",
                "create",
                "--time",
                "2026-10-26T14:32:10+09:00",
                "--reason",
                "합성 테스트",
                "--before",
                "10m",
                "--after",
                "5m",
            )
            == 0
        )
        incident_id = re.search(r"inc_[0-9a-f]{12}", capsys.readouterr().out).group(0)
        assert (
            self.run(db, "incident", "import-route", incident_id, str(FIXTURES / "route.csv")) == 0
        )
        assert self.run(db, "incident", "candidates", incident_id) == 0
        out = capsys.readouterr().out
        assert "후보 카메라 5건" in out and "보존 요청 권장 범위" in out

        export = tmp_path / "exports" / "report.json"
        assert self.run(db, "incident", "report", incident_id, "--json", "--out", str(export)) == 0
        assert stat.S_IMODE(os.stat(export).st_mode) == 0o600
        report = json.loads(export.read_text(encoding="utf-8"))
        candidate = next(c for c in report["candidates"] if not c["needs_manual_review"])
        capsys.readouterr()

        assert self.run(db, "preservation", "prepare", candidate["candidate_id"]) == 0
        assert "보존 요청 초안" in capsys.readouterr().out
        assert self.run(db, "incident", "delete", incident_id) == 2  # --yes 없이 거부
        assert self.run(db, "incident", "delete", incident_id, "--yes") == 0
        assert "삭제 검증: 통과" in capsys.readouterr().out
        assert self.run(db, "audit", "list", "--resource", incident_id) == 0
        assert "incident.delete" in capsys.readouterr().out

    def test_naive_incident_time_is_rejected(self, tmp_path, capsys):
        db = tmp_path / "t.sqlite3"
        code = self.run(db, "incident", "create", "--time", "2026-10-26T14:32:10", "--reason", "r")
        assert code == 2
        assert "시간대" in capsys.readouterr().err


def test_existing_files_are_restricted_to_owner(tmp_path):
    db = tmp_path / "t.sqlite3"
    db.touch(mode=0o644)
    out = tmp_path / "report.json"
    out.touch(mode=0o644)
    assert main(["--db", str(db), "init"]) == 0
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    from gps2cctv.cli import _write_private

    _write_private(out, "{}")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
