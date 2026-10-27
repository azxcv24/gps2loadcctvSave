"""명령행 인터페이스.

    gps2cctv [--db 경로] [--config 설정.toml] <명령> ...

데이터베이스 기본 위치는 ``data/gps2cctv.sqlite3`` 이며 환경 변수 ``GPS2CCTV_DB`` 로 바꿀 수 있다.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__, audit, catalog, incidents, preservation, retention
from .candidates import find_candidates
from .config import Settings, load_settings
from .db import connect
from .errors import Gps2CctvError, PolicyDeniedError
from .policy import ACTIONS, REASON_TEXT, check_policy
from .report import INCIDENT_STATUS_TEXT, build_report, record_view, render_text
from .routeio import read_route
from .timeutil import fmt_local, parse_duration, parse_timestamp, utc_now

DEFAULT_DB = Path("data") / "gps2cctv.sqlite3"


def _write_private(path: Path, text: str) -> None:
    """소유자만 읽을 수 있는 파일(0600)로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # 이미 있던 파일도 소유자 전용으로
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _emit(text: str, out: str | None) -> None:
    if out:
        _write_private(Path(out), text)
        print(f"저장했습니다: {out} (권한 0600)")
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------------------
# 명령 구현
# ---------------------------------------------------------------------------


def cmd_init(args, conn: sqlite3.Connection, settings: Settings) -> int:
    print(f"데이터베이스 준비 완료: {args.db}")
    return 0


def cmd_source_register(args, conn, settings) -> int:
    ids = catalog.register_sources(conn, Path(args.file), utc_now(), settings)
    print(f"소스 {len(ids)}개를 등록했습니다: {', '.join(ids)}")
    print("※ 사용하려면 약관·절차를 검토한 뒤 'source review' 로 정책 검토 기록을 남기세요.")
    return 0


def cmd_source_list(args, conn, settings) -> int:
    tz = settings.display_timezone
    rows = catalog.list_sources(conn)
    if not rows:
        print("등록된 소스가 없습니다.")
        return 0
    for s in rows:
        state = "활성" if s.enabled else "비활성"
        review = (
            f"검토 유효 ~{fmt_local(s.snapshot_valid_until, tz)} [{', '.join(s.snapshot_actions)}]"
            if s.snapshot_valid_until
            else "정책 검토 기록 없음"
        )
        retention = f"{s.provider_retention_days}일" if s.provider_retention_days else "미상"
        print(f"{s.id} ({s.environment}, {state}) — {s.provider_name}")
        print(
            f"  기능: {', '.join(s.capabilities)} / 카메라 {s.camera_count}대 / 제공처 보관 {retention}"
        )
        print(f"  {review}")
    return 0


def cmd_source_review(args, conn, settings) -> int:
    actions = [a.strip() for a in args.actions.split(",") if a.strip()]
    snapshot_id = catalog.review_source(
        conn,
        args.source_id,
        actions=actions,
        terms_ref=args.terms_ref,
        valid_days=args.valid_days,
        reviewer=args.reviewer,
        notes=args.notes,
        now=utc_now(),
        settings=settings,
    )
    print(f"정책 검토 기록을 남겼습니다: {snapshot_id} (허용: {', '.join(sorted(set(actions)))})")
    return 0


def cmd_source_check(args, conn, settings) -> int:
    decision = check_policy(conn, args.source_id, args.action, utc_now(), settings)
    verdict = "허용" if decision.allowed else "거부"
    print(f"{verdict}: {decision.reason_text} ({decision.reason_code})")
    return 0 if decision.allowed else PolicyDeniedError.exit_code


def cmd_source_enable(args, conn, settings) -> int:
    catalog.set_source_enabled(conn, args.source_id, True, utc_now(), settings)
    print(f"소스를 활성화했습니다: {args.source_id}")
    return 0


def cmd_source_disable(args, conn, settings) -> int:
    catalog.set_source_enabled(conn, args.source_id, False, utc_now(), settings)
    print(f"소스를 비활성화했습니다(모든 조회·요청 중단): {args.source_id}")
    return 0


def cmd_camera_sync(args, conn, settings) -> int:
    result = catalog.sync_cameras(conn, args.source_id, utc_now(), settings)
    print(
        f"{result.source_id}: 카메라 {result.upserted}대 갱신, "
        f"카탈로그에서 사라진 카메라 {result.marked_missing}대, "
        f"제공처 시각 정보 {result.clock_declarations}건"
    )
    return 0


def cmd_camera_list(args, conn, settings) -> int:
    rows = catalog.list_cameras(conn, args.source, args.limit)
    if not rows:
        print("카메라가 없습니다.")
        return 0
    for r in rows:
        print(
            f"{r['id']}  {r['name'] or '-'}  상태: {r['status']}  "
            f"시각 오차 근거: {r['clock_models']}건"
        )
    return 0


def cmd_camera_time_observe(args, conn, settings) -> int:
    reference = parse_timestamp(args.reference_time, args.assume_tz)
    camera_time = parse_timestamp(args.camera_time, args.assume_tz)
    model_id, offset = catalog.add_time_observation(
        conn,
        args.camera_id,
        reference_time=reference,
        camera_time=camera_time,
        uncertainty_seconds=args.uncertainty,
        drift_seconds_per_day=args.drift,
        note=args.note,
        now=utc_now(),
        settings=settings,
    )
    direction = "빠름" if offset > 0 else "느림" if offset < 0 else "일치"
    print(f"시각 오차를 기록했습니다: {model_id} — CCTV 시계 {offset:+.1f}초 ({direction})")
    return 0


def cmd_incident_create(args, conn, settings) -> int:
    incident = incidents.create_incident(
        conn,
        occurred_at=parse_timestamp(args.time, args.assume_tz),
        reason=args.reason,
        now=utc_now(),
        settings=settings,
        time_uncertainty_seconds=(
            parse_duration(args.time_uncertainty) if args.time_uncertainty else None
        ),
        before_seconds=parse_duration(args.before) if args.before else None,
        after_seconds=parse_duration(args.after) if args.after else None,
        latitude=args.lat,
        longitude=args.lon,
        location_uncertainty_m=args.location_uncertainty,
        retention_days=args.retention_days,
    )
    tz = settings.display_timezone
    print(f"사건을 만들었습니다: {incident.id}")
    print(
        f"  확인 범위: {fmt_local(incident.started_at, tz)} ~ "
        f"{fmt_local(incident.ended_at, tz, with_date=False)}"
    )
    print(f"  자료 보존 종료: {fmt_local(incident.retention_until, tz)}")
    return 0


def cmd_incident_list(args, conn, settings) -> int:
    tz = settings.display_timezone
    rows = incidents.list_incidents(conn)
    if not rows:
        print("사건이 없습니다.")
        return 0
    for inc in rows:
        print(
            f"{inc.id}  사고 {fmt_local(inc.occurred_at, tz)}  "
            f"상태: {INCIDENT_STATUS_TEXT.get(inc.status, inc.status)}  "
            f"보존 종료 {fmt_local(inc.retention_until, tz, with_date=True)}"
        )
    return 0


def cmd_incident_import_route(args, conn, settings) -> int:
    points = read_route(Path(args.file), args.assume_tz)
    result = incidents.import_route(conn, args.incident_id, points, utc_now(), settings)
    print(
        f"경로 지점 {result.stored}개 저장 "
        f"(사건 확인 범위 밖 {result.outside_window}개 버림, 중복 {result.duplicates}개 무시)"
    )
    print(f"  좌표는 소수점 {settings.route_precision_decimals}자리로 낮춰 저장했습니다.")
    return 0


def cmd_incident_candidates(args, conn, settings) -> int:
    now = utc_now()
    run = find_candidates(conn, args.incident_id, now, settings)
    report = build_report(
        conn, args.incident_id, now, settings, excluded_sources=run.excluded_sources
    )
    sys.stdout.write(render_text(report, conn, settings))
    return 0


def cmd_incident_report(args, conn, settings) -> int:
    now = utc_now()
    report = build_report(conn, args.incident_id, now, settings)
    export = bool(args.json or args.out)
    if args.json:
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_text(report, conn, settings)
    record_view(conn, args.incident_id, now, settings, export=export)
    _emit(text, args.out)
    return 0


def cmd_incident_delete(args, conn, settings) -> int:
    if not args.yes:
        print("삭제는 되돌릴 수 없습니다. 확인했다면 --yes 를 붙여 다시 실행하세요.")
        return 2
    report = retention.delete_incident(conn, args.incident_id, utc_now(), settings)
    print(
        f"사건 {args.incident_id} 삭제: 경로 지점 {report.route_points}개, "
        f"후보 {report.candidates}건, 보존 요청 {report.preservation_requests}건"
    )
    print("  삭제 검증: " + ("통과" if report.verified else f"실패 {report.leftovers}"))
    return 0 if report.verified else 1


def cmd_preservation_prepare(args, conn, settings) -> int:
    request = preservation.prepare_request(
        conn, args.candidate_id, now=utc_now(), settings=settings, reviewed=args.reviewed
    )
    _emit(preservation.render_draft(conn, request, settings), args.out)
    print(f"\n보존 요청 초안을 만들었습니다: {request.id}", file=sys.stderr)
    print(
        "제출한 뒤에는 'preservation update <요청ID> --status submitted' 로 기록하세요.",
        file=sys.stderr,
    )
    return 0


def cmd_preservation_show(args, conn, settings) -> int:
    request = preservation.get_request(conn, args.request_id)
    audit.record(
        conn,
        action="preservation.view",
        resource_type="preservation_request",
        resource_id=request.id,
        now=utc_now(),
        settings=settings,
    )
    _emit(preservation.render_draft(conn, request, settings), args.out)
    return 0


def cmd_preservation_list(args, conn, settings) -> int:
    tz = settings.display_timezone
    incidents.get_incident(conn, args.incident_id)
    rows = preservation.list_requests(conn, args.incident_id)
    if not rows:
        print("보존 요청이 없습니다.")
        return 0
    for r in rows:
        ref = f"  접수번호 {r.provider_request_id}" if r.provider_request_id else ""
        print(
            f"{r.id}  카메라 {r.provider_camera_id}  "
            f"{fmt_local(r.requested_start, tz)} ~ {fmt_local(r.requested_end, tz, with_date=False)}"
            f"  {preservation.STATUS_TEXT[r.status]}{ref}"
        )
    return 0


def cmd_preservation_update(args, conn, settings) -> int:
    at = parse_timestamp(args.at, args.assume_tz) if args.at else None
    request = preservation.update_status(
        conn,
        args.request_id,
        args.status,
        now=utc_now(),
        settings=settings,
        at=at,
        provider_request_id=args.provider_ref,
        response_reference=args.response_ref,
    )
    print(f"{request.id} 상태: {preservation.STATUS_TEXT[request.status]}")
    return 0


def cmd_purge(args, conn, settings) -> int:
    from .live.clips import purge_clips

    now = utc_now()
    report = retention.purge_expired(conn, now, settings, dry_run=args.dry_run)
    clip_report = purge_clips(conn, now, settings, dry_run=args.dry_run)
    prefix = "[미리보기] 삭제 예정" if args.dry_run else "삭제 완료"
    print(
        f"{prefix}: 사건 {len(report.incident_ids)}건, 경로 지점 {report.route_points}개, "
        f"후보 {report.candidates}건, 보존 요청 {report.preservation_requests}건, "
        f"감사 로그 {report.audit_events}건, 자동 저장 클립 {clip_report.expired_clips}건, "
        f"수신 위치 {clip_report.positions}개"
    )
    if not args.dry_run:
        print("  삭제 검증: " + ("통과" if report.verified else f"실패 {report.leftovers}"))
    return 0 if report.verified else 1


def cmd_serve(args, conn, settings) -> int:
    import logging

    from .live.serve import run_serve

    if not args.config:
        raise Gps2CctvError(
            "serve 에는 --config 설정 파일이 필요합니다(예: config.example.toml 참고)."
        )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return run_serve(Path(args.config), Path(args.db), settings)


def _recording_config(args):
    from .live.config import RecordingConfig, load_live_config

    if args.config:
        try:
            return load_live_config(Path(args.config)).recording
        except Gps2CctvError:
            pass
    return RecordingConfig()


def cmd_clips_list(args, conn, settings) -> int:
    from .live.clips import list_clips
    from .timeutil import from_db

    tz = settings.display_timezone
    rows = list_clips(conn, device_id=args.device, limit=args.limit, protected_only=args.protected)
    if not rows:
        print("저장된 클립이 없습니다.")
        return 0
    status_text = {"saved": "저장", "partial": "일부만", "failed": "실패"}
    for r in rows:
        start, end = from_db(r["window_start"]), from_db(r["window_end"])
        mark = " [보호]" if r["protected"] else ""
        path = r["viewing_path"] or r["original_path"] or f"({r['failure_reason']})"
        print(
            f"{r['id']}  {fmt_local(from_db(r['estimated_passage_at']), tz)}  {r['device_id']}  "
            f"{r['camera_name'] or r['camera_id']}  {status_text.get(r['status'], r['status'])}"
            f"{mark}  {(end - start).total_seconds():.0f}초"
        )
        print(f"    {path}")
    return 0


def cmd_clips_protect(args, conn, settings) -> int:
    from .live.clips import set_protected

    protect = args.cmd == "protect"
    if not set_protected(conn, args.clip_id, protect, utc_now(), settings):
        raise Gps2CctvError(f"클립을 찾을 수 없습니다: {args.clip_id}")
    print(f"{args.clip_id}: {'보호(자동 삭제 안 함)' if protect else '보호 해제'}")
    return 0


def cmd_clips_mark(args, conn, settings) -> int:
    from .live.clips import mark_event

    rec = _recording_config(args)
    at = parse_timestamp(args.time, args.assume_tz) if args.time else utc_now()
    event_id, protected = mark_event(
        conn, device_id=args.device, kind="manual", at=at, latitude=None, longitude=None,
        detail=None, protect_before_seconds=rec.event_protect_before_seconds,
        protect_after_seconds=rec.event_protect_after_seconds, now=utc_now(), settings=settings,
    )  # fmt: skip
    print(
        f"{event_id}: {fmt_local(at, settings.display_timezone)} 전후 클립을 보호합니다 "
        f"(지금 보호한 클립 {protected}건, 이후 저장되는 해당 구간 클립도 보호)"
    )
    return 0


def cmd_audit_list(args, conn, settings) -> int:
    tz = settings.display_timezone
    for e in audit.list_events(conn, limit=args.limit, resource_id=args.resource):
        detail = f" {json.dumps(e.detail, ensure_ascii=False)}" if e.detail else ""
        reason = f" ({e.reason_code})" if e.reason_code else ""
        print(
            f"{fmt_local(e.created_at, tz)}  {e.action}  {e.resource_type}:{e.resource_id or '-'}"
            f"  {e.outcome}{reason}{detail}"
        )
    return 0


# ---------------------------------------------------------------------------
# 인자 구성
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gps2cctv",
        description=(
            "GPS 경로와 사고 시점을 기준으로 주변 공식 CCTV의 확인·보존 요청 시간 범위를 "
            "계산합니다. 영상 수집·식별·추적 기능은 없습니다."
        ),
    )
    parser.add_argument("--version", action="version", version=f"gps2cctv {__version__}")
    parser.add_argument(
        "--db",
        default=os.environ.get("GPS2CCTV_DB", str(DEFAULT_DB)),
        help="데이터베이스 파일 (기본: data/gps2cctv.sqlite3, 환경 변수 GPS2CCTV_DB)",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("GPS2CCTV_CONFIG"),
        help="설정 파일(TOML). 예시는 config.example.toml",
    )
    groups = parser.add_subparsers(dest="group", required=True, metavar="<명령>")

    def add(sub, name: str, func: Callable, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    add(groups, "init", cmd_init, "데이터베이스 준비")

    src = groups.add_parser("source", help="공식 CCTV 소스·정책 검토 관리")
    src_sub = src.add_subparsers(dest="cmd", required=True, metavar="<하위 명령>")
    p = add(src_sub, "register", cmd_source_register, "소스 정의 파일(JSON) 등록")
    p.add_argument("file")
    add(src_sub, "list", cmd_source_list, "등록된 소스와 정책 검토 상태")
    p = add(src_sub, "review", cmd_source_review, "약관·절차 검토 결과를 정책 검토 기록으로 남김")
    p.add_argument("source_id")
    p.add_argument(
        "--actions",
        required=True,
        help="허용 행위(쉼표 구분): catalog_sync,catalog_lookup,preservation_request_prepare",
    )
    p.add_argument("--terms-ref", required=True, help="검토한 약관 버전 또는 해시")
    p.add_argument("--valid-days", type=int, default=90, help="검토 유효 기간(일, 기본 90)")
    p.add_argument("--reviewer", required=True, help="검토자 또는 승인된 자동화 이름")
    p.add_argument("--notes")
    p = add(src_sub, "check", cmd_source_check, "특정 행위가 허용되는지 확인")
    p.add_argument("source_id")
    p.add_argument("action", choices=sorted(ACTIONS))
    p = add(src_sub, "enable", cmd_source_enable, "소스 활성화")
    p.add_argument("source_id")
    p = add(src_sub, "disable", cmd_source_disable, "소스 비활성화(비상 중단·철회)")
    p.add_argument("source_id")

    cam = groups.add_parser("camera", help="카메라 카탈로그와 시각 오차")
    cam_sub = cam.add_subparsers(dest="cmd", required=True, metavar="<하위 명령>")
    p = add(cam_sub, "sync", cmd_camera_sync, "소스 어댑터로 카메라 메타데이터 갱신")
    p.add_argument("source_id")
    p = add(cam_sub, "list", cmd_camera_list, "카메라 목록")
    p.add_argument("--source")
    p.add_argument("--limit", type=int, default=100)
    p = add(
        cam_sub,
        "time-observe",
        cmd_camera_time_observe,
        "같은 순간의 표준 시각과 CCTV 화면 표시 시각을 기록해 시각 오차 근거로 저장",
    )
    p.add_argument("camera_id", help="<소스ID>:<제공처 카메라ID>")
    p.add_argument(
        "--reference-time", required=True, help="표준 시각(예: 2026-10-24T12:00:00+09:00)"
    )
    p.add_argument("--camera-time", required=True, help="같은 순간 CCTV 화면에 표시된 시각")
    p.add_argument("--assume-tz", help="시간대 없는 입력에 적용할 시간대(예: Asia/Seoul)")
    p.add_argument("--uncertainty", type=float, default=2.0, help="관측 불확실성(초, 기본 2)")
    p.add_argument("--drift", type=float, help="하루당 시계 드리프트(초). 기본은 설정값")
    p.add_argument("--note")

    inc = groups.add_parser("incident", help="사건 생성·경로 입력·후보 계산·보고서")
    inc_sub = inc.add_subparsers(dest="cmd", required=True, metavar="<하위 명령>")
    p = add(inc_sub, "create", cmd_incident_create, "사건 생성")
    p.add_argument("--time", required=True, help="사고 시점(ISO 8601, 시간대 포함)")
    p.add_argument("--assume-tz", help="시간대 없는 입력에 적용할 시간대")
    p.add_argument("--time-uncertainty", help="사고 시점 불확실성(예: 60s, 2m)")
    p.add_argument("--before", help="사고 전 확인 범위(예: 10m)")
    p.add_argument("--after", help="사고 후 확인 범위(예: 5m)")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--location-uncertainty", type=float, help="사고 위치 불확실성(m)")
    p.add_argument("--reason", required=True, help="사건 사유(500자 이하)")
    p.add_argument("--retention-days", type=int, help="자료 보존 기간(일)")
    add(inc_sub, "list", cmd_incident_list, "사건 목록")
    p = add(
        inc_sub,
        "import-route",
        cmd_incident_import_route,
        "GPS 경로(CSV/GPX) 입력 — 사건 확인 범위 안의 지점만 저장",
    )
    p.add_argument("incident_id")
    p.add_argument("file")
    p.add_argument("--assume-tz", help="시간대 없는 시각에 적용할 시간대")
    p = add(inc_sub, "candidates", cmd_incident_candidates, "주변 카메라와 시간 범위 계산")
    p.add_argument("incident_id")
    p = add(inc_sub, "report", cmd_incident_report, "사건 보고서 보기·내보내기")
    p.add_argument("incident_id")
    p.add_argument("--json", action="store_true", help="JSON으로 출력")
    p.add_argument("--out", help="파일로 저장(권한 0600)")
    p = add(inc_sub, "delete", cmd_incident_delete, "사건과 관련 자료 즉시 삭제")
    p.add_argument("incident_id")
    p.add_argument("--yes", action="store_true", help="삭제 확인")

    pres = groups.add_parser("preservation", help="공식 보존 요청 초안·상태 관리")
    pres_sub = pres.add_subparsers(dest="cmd", required=True, metavar="<하위 명령>")
    p = add(pres_sub, "prepare", cmd_preservation_prepare, "후보로 보존 요청 초안 만들기")
    p.add_argument("candidate_id")
    p.add_argument(
        "--reviewed", action="store_true", help="신뢰도 낮음·범위 과대 후보를 직접 검토했음을 확인"
    )
    p.add_argument("--out", help="초안을 파일로 저장(권한 0600)")
    p = add(pres_sub, "show", cmd_preservation_show, "보존 요청 초안 다시 보기")
    p.add_argument("request_id")
    p.add_argument("--out")
    p = add(pres_sub, "list", cmd_preservation_list, "사건의 보존 요청 목록")
    p.add_argument("incident_id")
    p = add(pres_sub, "update", cmd_preservation_update, "제출·접수·응답 상태 기록")
    p.add_argument("request_id")
    p.add_argument("--status", required=True, choices=sorted(preservation.STATUS_TEXT))
    p.add_argument("--provider-ref", help="제공처 접수번호")
    p.add_argument("--response-ref", help="응답 문서 참조번호")
    p.add_argument("--at", help="실제 제출·응답 시각(기본: 지금)")
    p.add_argument("--assume-tz")

    p = add(
        groups,
        "serve",
        cmd_serve,
        "실시간 자동 녹화: GPS 수신 → 앞 CCTV 녹화 대기 → 지나간 구간 자동 저장",
    )
    p.add_argument("--verbose", action="store_true", help="자세한 로그")
    p.set_defaults(no_conn=True)

    clips = groups.add_parser("clips", help="자동 저장된 CCTV 클립")
    clips_sub = clips.add_subparsers(dest="cmd", required=True, metavar="<하위 명령>")
    p = add(clips_sub, "list", cmd_clips_list, "저장된 클립 목록")
    p.add_argument("--device")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--protected", action="store_true", help="보호된 클립만")
    p = add(clips_sub, "protect", cmd_clips_protect, "클립을 보호(보존 기간이 지나도 삭제 안 함)")
    p.add_argument("clip_id")
    p = add(clips_sub, "unprotect", cmd_clips_protect, "클립 보호 해제")
    p.add_argument("clip_id")
    p = add(
        clips_sub,
        "mark",
        cmd_clips_mark,
        "사고 등 중요한 시점을 표시해 앞뒤 클립을 보호(이후 저장되는 클립 포함)",
    )
    p.add_argument("--device", required=True)
    p.add_argument("--time", help="표시할 시각(기본: 지금)")
    p.add_argument("--assume-tz")

    p = add(groups, "purge", cmd_purge, "보존 기간이 지난 사건·감사 로그·클립 삭제")
    p.add_argument("--dry-run", action="store_true", help="삭제하지 않고 대상만 보기")

    aud = groups.add_parser("audit", help="감사 로그")
    aud_sub = aud.add_subparsers(dest="cmd", required=True, metavar="<하위 명령>")
    p = add(aud_sub, "list", cmd_audit_list, "최근 감사 로그")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--resource", help="자원 ID로 거르기")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(Path(args.config) if args.config else None)
        if getattr(args, "no_conn", False):
            return args.func(args, None, settings)
        conn = connect(Path(args.db))
        try:
            return args.func(args, conn, settings)
        finally:
            conn.close()
    except PolicyDeniedError as exc:
        print(
            f"거부: {exc} [{exc.reason_code}: {REASON_TEXT.get(exc.reason_code, '')}]",
            file=sys.stderr,
        )
        return exc.exit_code
    except Gps2CctvError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
