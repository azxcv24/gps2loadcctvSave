# GPS 기반 사고 증거 보존 및 공공 CCTV 자료 연계 도구

> 블랙박스 고장·분실·저장 오류 등으로 사고 입증 자료가 부족해지는 상황에 대비하여, 사용자의 GPS 경로와 사고 시점을 기준으로 주변의 **공식 공개 CCTV 정보를 확인하고 허용된 자료의 보존 절차를 지원하는 연구용 프로젝트**입니다.

## 프로젝트를 시작한 이유

블랙박스가 고장 났거나 사고 당시 영상이 저장되지 않으면, 사고가 발생한 시간과 장소를 알고 있어도 주변 공공 CCTV·고속도로 CCTV 자료를 제때 확보하지 못해 사실관계를 입증하기 어려울 수 있습니다.

이 프로젝트는 이런 상황에서 사용자의 GPS 기록을 바탕으로 다음을 미리 준비하고 신속하게 처리하는 것을 목표로 합니다.

- 이동 경로 주변의 공식 CCTV 위치와 제공 여부 확인
- GPS와 CCTV 시각 차이를 고려한 예상 통과 시간 계산
- 사고 전후의 제한된 시간·공간 범위 산정
- 허용된 자료의 임시 보존 또는 공식 보존 요청 관리
- GPS 기록, 카메라 정보, 시간 오차, 요청 이력을 하나의 사건 자료로 정리

핵심은 CCTV 영상 속 사람이나 차량을 자동으로 찾아 추적하는 것이 아니라, **사고가 있었을 가능성이 있는 시간과 위치의 자료가 삭제되기 전에 보존되도록 돕는 것**입니다.

## 현재 구현 상태 (v0.1 — M1 MVP)

로컬에서 실행하는 명령행 도구(`gps2cctv`)로, **합성 데이터**로 동작을 검증한 단계입니다.

| 구분 | 상태 |
|---|---|
| 사건 생성, GPS 경로(CSV/GPX) 입력, 사건 확인 범위 밖 지점 버림, 좌표 정밀도 축소 | 구현 |
| 공식 소스 등록(허용 목록), 정책 검토 기록, 실패 시 거부하는 정책 검증 | 구현 |
| 로컬 JSON 카탈로그 동기화, 카메라 시각 오차 기록(사용자 관측·제공처 고지) | 구현 |
| 후보 카메라·표준시/CCTV 시계/보존 요청 시간 범위 계산, 신뢰도·사유 표시 | 구현 |
| GPS 공백 구간 도달 가능 범위 계산, 사고 위치만 있을 때 계산 | 구현 |
| 제공처 보관 기간 기준 예상 삭제 시점·임박 경고 | 구현 |
| 보존 요청 **초안** 생성, 제출·접수·응답 상태와 참조번호 기록, 중복 요청 방지 | 구현 |
| 사건 보고서(텍스트·JSON) 내보내기, 감사 로그, 보존 기간 만료 삭제·삭제 검증 | 구현 |
| 실제 공공 CCTV 제공처 연동(HTTP API) | **미구현** (M2, 제공처 약관 검토 필요) |
| 제공처에 보존 요청 자동 제출 | **미구현** — 사용자가 공식 절차로 직접 제출 |
| 영상·스틸 수집 | **미구현·비활성** (정책 검증에서 항상 거부) |
| 저장 시 암호화 | **미구현** (M3). DB 파일은 소유자 전용 권한(0600)으로만 보호 |
| 운행 중 상시 경로 기록(시나리오 B) | **미구현** — [`docs/PLAN.md`](docs/PLAN.md) 13절 결정 필요 |

## 중요 고지

이 저장소는 타인을 몰래 찾아내거나 지속적으로 추적·감시하기 위한 도구가 아닙니다. 얼굴 인식, 번호판 인식, 개인 식별, 여러 카메라 사이의 사람·차량 자동 추적, 비인가 접근, 인증 우회, 서비스 약관을 위반한 수집, 대량 스크래핑은 이 프로젝트의 목표와 허용 범위에 포함되지 않습니다.

개발·검증은 다음 환경으로 제한합니다.

- 본인이 소유하거나 관리하는 CCTV 장비
- 운영 주체의 명시적인 서면 동의를 받은 테스트 환경
- 이용약관과 제공 정책이 허용하는 공식 API·공식 URL·공공데이터
- 관련 법률과 기관 정책을 사전에 검토한 환경

공개되어 있다는 이유만으로 영상을 자유롭게 저장·재배포할 수 있는 것은 아닙니다. 실제 연동 전에는 제공처별 이용약관, 저작권, 영상정보 및 개인정보 보호 규정, 위치정보 관련 규정, 접근 제한, 보관 기간, 삭제 요청·보존 요청 절차를 확인해야 합니다.

## 목표와 비목표

### 목표

- 사용자의 GPS 경로와 사건 시점을 안전하게 기록
- 공식 CCTV 카탈로그에서 주변 카메라와 제공 정책 조회
- 카메라별 시각 오차와 GPS 오차를 반영한 시간 범위 계산
- 영상보다 메타데이터·이벤트·보존 요청을 우선
- 사고 전후의 제한된 범위만 사건 단위로 보존
- 사용자가 직접 검토하고 선택한 자료를 정리
- 수집·열람·보존 요청·삭제 작업을 감사 로그로 남김

### 비목표

- CCTV 영상에서 사용자의 얼굴·차량·번호판을 자동 인식
- 여러 CCTV 사이에서 사람이나 차량의 이동 경로 자동 연결
- 타인의 이동 경로를 장기간 축적하거나 프로파일링
- 로그인 우회, 접근 제한 회피, 비공식 스트림 추출
- 무단 녹화, 무단 재배포, 데이터 브로커식 수집
- 사고와 무관한 구간의 CCTV를 상시·무기한 저장

## 허용되는 사용 사례

1. 본인의 사고 또는 분쟁에 대비해 GPS 경로와 주변 카메라 정보를 보존
2. 본인 소유 차량·장비의 사고 전후 자료를 사건 단위로 정리
3. 명시적 동의를 받은 실험실·교육용 CCTV 테스트
4. 공공기관이 공식적으로 제공하는 카메라 위치·상태·교통 이벤트 메타데이터 조회
5. 제공처가 허용한 범위에서 제한적인 영상 자료 보존 또는 보존 요청 상태 관리
6. 원본 영상 대신 시간, 위치 격자, 카메라 식별자, 상태, 요청 이력 분석

## 핵심 원칙

### 1. 영상 속 대상을 찾지 않고 시간·위치 범위를 산정
시스템은 GPS와 지도상 카메라 위치를 이용해 후보 카메라와 예상 통과 시각을 계산합니다. 최종 영상 확인은 사용자가 직접 수행하며, 자동 식별·추적을 기본 기능으로 두지 않습니다.

### 2. 합법성 우선
소스별 이용약관과 관할 법률을 확인하고, 불확실하면 수집하지 않습니다. 보존이 필요한 경우에도 프로그램이 직접 다운로드하는 것보다 제공처의 공식 보존 요청·제공 절차를 우선합니다.

### 3. 사건 중심·최소 수집
평상시에는 GPS와 CCTV 메타데이터를 최소한으로 기록하고, 사고·이상 상황이 발생했을 때만 전후의 제한된 시간 범위를 사건으로 보존합니다.

### 4. 개인 식별 금지
개인 식별, 얼굴·번호판 인식, 임의의 재식별 시도를 구현하거나 추가하지 않습니다. 테스트 데이터는 합성 데이터 또는 적절히 마스킹된 자료를 사용합니다.

### 5. 시각 오차를 명시적으로 기록
각 CCTV의 시계가 GPS·표준시각과 얼마나 차이 나는지 알 수 없을 수 있으므로, 오차의 출처·추정값·신뢰도와 검색 여유 범위를 함께 기록합니다.

### 6. 안전한 기본값
영상 저장은 기본 비활성화, 보존 기간은 짧게, 요청 빈도는 제한적으로, 허용 소스만 사용하고, 사건 확정과 자료 내보내기는 사용자 확인을 요구합니다.

## 권장 처리 흐름

```text
GPS 기록 또는 사고 시점 입력
  -> 사건 생성 및 동의·권한 확인
  -> 이동 경로 주변 공식 CCTV 검색
  -> 카메라 위치·도로·방향·운영 시간 확인
  -> GPS 오차·이동 시간·CCTV 시각 오차를 반영한 시간 범위 계산
  -> 제공 정책과 보존 가능 여부 검증
  -> 메타데이터 기록 또는 공식 보존 요청
  -> 허용된 자료만 제한적으로 저장
  -> 사용자가 직접 검토하고 사건 자료 확정
  -> 감사 로그 기록 및 보존 기간 만료·삭제 처리
```

### 시간 범위 계산 예시

GPS 기준 예상 통과 시각이 `14:32:10`이고, CCTV 시계가 실제보다 약 18초 느린 것으로 추정되면, 시스템은 **시각 기준을 밝힌 세 가지 범위**를 계산합니다.

```text
예상 통과 시각(표준시):            14:32:10
GPS·이동·카메라 위치 오차 여유:     ±30초
① 확인 범위(표준시):               14:31:40 ~ 14:32:40
추정 CCTV 시계 오차:              -18초 (CCTV 시계가 18초 느림, 예시에서는 불확실성 0초)
② 확인 범위(CCTV 시계 기준):        14:31:22 ~ 14:32:22
③ 보존 요청 권장 범위(CCTV 화면 표시 시각): 14:31:20 ~ 14:32:40
   = ①과 ②를 모두 덮고 10초 단위로 바깥쪽 반올림
```

- 시각 오차 부호는 `CCTV 화면 표시 시각 − 표준 시각`입니다. 음수면 CCTV 시계가 느립니다.
- ③이 ①까지 덮는 이유는 시각 오차 추정이 틀려 실제로는 CCTV 시계가 정확한 경우에도 자료를 놓치지 않기 위해서입니다.
- 실제 프로그램에서 "±30초"에 해당하는 여유는 고정값이 아니라 촬영 반경, GPS 정확도, 좌표 반올림 오차, 카메라 위치 오차, 기록 간격으로 계산합니다. 시각 오차의 불확실성도 근거가 오래될수록 커집니다. 자세한 규칙은 [ADR-0002](docs/decisions/ADR-0002-time-basis-and-clock-offset.md)·[ADR-0004](docs/decisions/ADR-0004-location-precision-and-gps-gaps.md)에 있습니다.
- 위 예시는 `tests/test_candidate_math.py`의 `TestReadmeExample`이 그대로 검증합니다.

이 범위는 영상 속 대상을 자동으로 찾는 기준이 아니라, **어떤 시간대의 자료를 보존 요청하거나 사용자가 확인할지 결정하는 기준**입니다. 실제 계산은 카메라 위치, 도로 방향, 이동 속도, GPS 정확도, 교통 상황, 제공처의 시각 정보에 따라 달라집니다.

## 평상시 기록과 사고 시 보존

모든 주변 CCTV 영상을 상시·무기한 저장하는 방식은 법적·기술적·보안상 적절하지 않을 수 있습니다. 권장 방식은 다음과 같습니다.

### 평상시

- GPS 경로와 필요한 정확도의 시간 기록만 보존
- 주변 CCTV의 위치·상태·제공 정책 등 메타데이터 기록
- 영상 원본은 저장하지 않거나 제공처 정책이 허용하는 짧은 임시 보관만 사용
- 보존 기간과 삭제 시점을 사전에 설정

### 사고·이상 상황 발생 시

- 사용자가 직접 사고 버튼을 누르거나 승인된 센서 이벤트로 사건 생성
- 사고 전후 시간·공간 범위를 계산
- 해당 범위의 공식 CCTV와 보존 가능 여부 표시
- 제공처의 공식 보존 요청 또는 허용된 제한 수집 수행
- 사용자가 확인한 자료만 사건 자료로 확정
- 사건별 접근·내보내기·삭제 이력 관리

공공 CCTV 제공처는 영상 원본을 직접 내려받는 대신, 사고·민원·수사 등 정식 절차에 따라 보존·제공 요청을 받는 구조일 수 있습니다. 따라서 이 프로젝트는 **영상 자동 다운로드 도구가 아니라 보존 대상과 요청 절차를 관리하는 보조 도구**가 되는 방향을 우선합니다.

## 권장 아키텍처

- **GPS/사건 입력**: 사용자 동의 아래 GPS 경로, 사고 시점, 사고 전후 범위 입력
- **경로·시간 추정**: 위치 오차, 이동 속도, 카메라 위치, 시각 오차를 이용해 후보 범위 산정
- **CCTV Catalog Adapter**: 공공데이터 포털·기관 API 등 공식 제공처별 어댑터
- **Nearby Search**: 경로 주변 카메라, 도로 방향, 운영 상태, 제공 정책 검색
- **Policy & Permission Guard**: 허용 목록, 이용약관, 인증 상태, 요청 제한, 보존 정책 검증
- **Preservation Request Service**: 공식 보존 요청에 필요한 카메라·시간·사유·연락처·상태 관리
- **Capture/Metadata Service**: 초기 단계에서는 메타데이터와 이벤트만 저장. 영상 저장은 명시적으로 허용된 소스에서만 활성화
- **Storage**: 사건 단위 암호화 저장소와 접근 제어
- **Retention & Deletion**: 보존 기한, 삭제 요청, 계정 삭제, 소스 철회 처리
- **Audit Log**: 조회·요청·열람·내보내기·삭제·정책 변경 기록

v0.1의 구현 위치는 다음과 같습니다(`src/gps2cctv/`).

| 구성 요소 | 모듈 | 비고 |
|---|---|---|
| GPS/사건 입력 | `incidents.py`, `routeio.py` | CSV·GPX |
| 경로·시간 추정 | `candidates.py`, `geo.py`, `timeutil.py` | |
| CCTV Catalog Adapter | `catalog.py` | 로컬 JSON 어댑터만. HTTP 어댑터는 M2 |
| Nearby Search | `candidates.py` | |
| Policy & Permission Guard | `policy.py` | 실패 시 거부 |
| Preservation Request Service | `preservation.py` | 초안·상태 관리만, 자동 제출 없음 |
| Capture/Metadata Service | — | 미구현(영상 수집 비활성) |
| Storage | `db.py` | SQLite, 0600, secure_delete, 암호화 없음 |
| Retention & Deletion | `retention.py` | 만료 삭제·삭제 검증 |
| Audit Log | `audit.py` | 자유 텍스트·좌표 기록 불가 |

## 데이터 모델 (v0.1 구현 기준)

아래는 `src/gps2cctv/db.py`의 실제 스키마를 요약한 것입니다. 초안 대비 바뀐 점은 [`docs/PLAN.md`](docs/PLAN.md) 13절에 정리했습니다.

```text
cctv_source          제공처 소스
- id, provider_name
- environment: synthetic | production
- capabilities: [catalog, metadata, video, preservation_request]
- official_endpoint, terms_url            (HTTPS, production은 허용 목록 호스트만)
- preservation_procedure                  공식 보존 요청 절차 안내
- provider_retention_days                 제공처 영상 보관 기간(알려진 경우)
- adapter, enabled, last_verified_at
- material_changed_at                     엔드포인트·약관·기능 변경 시각(이전 정책 검토 무효)

policy_snapshot      사람(또는 승인된 자동화)의 약관·절차 검토 기록
- id, source_id, terms_version_or_hash
- checked_at, valid_until
- allowed_actions: [catalog_sync, catalog_lookup, preservation_request_prepare]
- reviewer_or_automation, notes

camera               카탈로그 카메라(공공 시설 위치라 좌표는 원본 유지)
- id(<source_id>:<provider_camera_id>), source_id, provider_camera_id, name
- latitude, longitude, location_uncertainty_m, coverage_radius_m
- road_name, road_direction
- status: active | inactive | unknown | missing_from_catalog
- observed_at, synced_at

camera_time_model    CCTV 시계 오차 근거 (offset = CCTV 표시 시각 − 표준 시각)
- id, camera_id, reference_time
- estimated_offset_seconds, uncertainty_seconds, drift_seconds_per_day
- evidence: provider_declared | manual_observation, evidence_note, calculated_at

incident             사건
- id, owner_id, occurred_at, occurred_at_uncertainty_seconds
- window_before_seconds, window_after_seconds, started_at, ended_at
- latitude_rounded, longitude_rounded, location_uncertainty_m, location_precision_decimals
- reason
- status: draft | preservation_requested | reviewing | closed
- retention_until, created_at

route_point          사건 확인 범위 안의 경로 지점만 저장(사건 삭제 시 함께 삭제)
- id, incident_id, recorded_at
- latitude_rounded, longitude_rounded, precision_decimals, accuracy_m, speed_mps

camera_candidate     후보 카메라와 시간 범위(재계산 시 교체)
- id, incident_id, camera_id, source_id, provider_camera_id, pass_index
- estimated_passage_at, closest_distance_m
- review_window_start/end                 표준시
- camera_clock_offset_seconds, uncertainty_seconds
- camera_clock_window_start/end           CCTV 시계 기준
- request_window_start/end                CCTV 화면 표시 시각 기준(보존 요청용)
- calculation_method, calculation_details(JSON 근거)
- confidence: high | medium | low (시간 범위 신뢰도, 식별 신뢰도 아님)
- needs_manual_review, reasons
- provider_deletion_estimate, policy_snapshot_id, created_at

preservation_request 보존 요청(초안·상태만, 연락처 저장 안 함)
- id, incident_id, candidate_id(재계산 시 NULL), camera_id, source_id, provider_camera_id
- requested_start/end, estimated_passage_at
- camera_clock_offset_seconds, uncertainty_seconds, provider_deletion_estimate
- calculation_snapshot(요청 당시 계산 근거 사본), policy_snapshot_id
- status: draft | submitted | acknowledged | fulfilled | rejected | expired | withdrawn
- provider_request_id, submitted_at, responded_at, response_reference

audit_event          감사 로그(자유 텍스트 불가, detail은 정수·참거짓만)
- id, created_at, actor_id, action, resource_type, resource_id
- outcome: success | denied | error, reason_code, detail
- retention_until

observation          (미구현 — M3에서 허용된 소스의 이벤트·메타데이터 기록용)
```

개인 식별에 사용될 수 있는 원본 영상, 정밀 좌표, 이동 경로는 특히 민감할 수 있으므로 암호화, 접근 통제, 보존 기간, 삭제 검증을 함께 설계해야 합니다.

<details>
<summary>초기 데이터 모델 초안(참고용)</summary>

```text
incident
- id
- owner_id
- started_at
- ended_at
- route_reference
- incident_location_rounded
- reason
- status: draft | preservation_requested | reviewing | closed
- retention_until

route_point
- id
- incident_id
- recorded_at
- latitude_rounded
- longitude_rounded
- accuracy_m
- speed_mps

cctv_source
- id
- provider_name
- source_type: catalog | metadata | video | preservation_request
- official_endpoint
- terms_url
- access_policy
- last_verified_at
- enabled

camera
- id
- source_id
- provider_camera_id
- latitude_rounded
- longitude_rounded
- road_direction
- status
- observed_at

camera_time_model
- id
- camera_id
- reference_time
- estimated_offset_seconds
- uncertainty_seconds
- evidence
- calculated_at

camera_candidate
- id
- incident_id
- camera_id
- estimated_passage_at
- review_window_start
- review_window_end
- confidence
- reason

preservation_request
- id
- incident_id
- camera_id
- requested_start
- requested_end
- provider_request_id
- submitted_at
- status
- response_reference

observation
- id
- incident_id
- camera_id
- observed_at
- event_type
- metadata_uri
- media_uri (optional)
- policy_snapshot_id
- retention_until

policy_snapshot
- id
- source_id
- terms_version_or_hash
- checked_at
- allowed_actions
- reviewer_or_automation

audit_event
- id
- actor_id
- action
- resource_type
- resource_id
- created_at
- outcome
- reason_code
```

</details>

## API 및 어댑터 방향

| 초안 API | v0.1 구현 (Python 함수 / CLI) |
|---|---|
| `create_incident(time, location, reason)` | `incidents.create_incident` / `incident create` |
| `append_route_point(incident_id, point)` | `incidents.import_route` / `incident import-route` (파일 단위) |
| `list_sources()` | `catalog.list_sources` / `source list` |
| `sync_cameras(source_id)` | `catalog.sync_cameras` / `camera sync` |
| `find_candidates(incident_id)` | `candidates.find_candidates` / `incident candidates` |
| `estimate_camera_time(camera_id, route)` | `candidates.resolve_clock` + `catalog.add_time_observation` / `camera time-observe` |
| `check_policy(source_id, action)` | `policy.check_policy` / `source check` |
| `create_preservation_request(candidate_id)` | `preservation.prepare_request` / `preservation prepare` (초안만) |
| `record_observation(incident_id, observation)` | 미구현 (M3) |
| `purge_expired(now)` | `retention.purge_expired` / `purge` |

실제 HTTP 요청, 인증 방식, 갱신 주기, 재시도 정책은 각 제공처의 공식 문서와 이용약관을 기준으로 구현해야 합니다. 비공식 스트림 주소를 추측하거나 접근 제한을 회피하는 기능은 추가하지 않습니다.

## 로드맵

### 0단계 — 동기·법적 범위 확정

- [x] 사고 증거 보존 목적과 허용 사용 사례를 문서화
- [ ] 대상 제공처별 공식 문서·약관·보존 요청 절차 목록화
- [x] 개인 식별·자동 추적을 비목표로 고정
- [ ] 데이터 분류, 보존 기간, 삭제 절차 정의 — 기본값은 구현됨([`config.example.toml`](config.example.toml)), 소유자 승인 필요
- [x] 합성 GPS 경로와 CCTV 카탈로그 작성 (`fixtures/synthetic/`, `scripts/make_synthetic_fixtures.py`)

### 1단계 — 메타데이터·후보 검색 MVP

- [x] GPS 경로와 사건 시점 입력
- [x] 카메라 위치 기반 후보 검색
- [x] 이동 시간·GPS 오차·시각 오차를 반영한 시간 범위 계산
- [x] 영상 수집 없이 카메라 후보와 보존 대상 목록 생성
- [x] 정책 거부·불확실한 시각·삭제 만료 테스트

### 2단계 — 공식 보존 요청 연동

- [ ] 공식 제공처 1곳을 선정하고 약관 검토 기록 작성
- [x] 보존 요청 양식·상태·응답 참조번호 관리 — 공통 초안 양식 기준, 제공처별 양식은 미정
- [ ] 요청 빈도·타임아웃·오류·중단 장치 추가 — 소스 비활성화(비상 중단)만 구현
- [ ] 정책 변경과 보존 가능 기간 감지 — 소스 정보 변경 시 재검토 요구, 보관 기간 기준 삭제 임박 경고는 구현

### 3단계 — 제한적 자료 보존

- [ ] 영상보다 이벤트·스틸·메타데이터 우선
- [ ] 허용된 소스에만 명시적 수집 기능 제공
- [ ] 암호화 저장, 권한 분리, 보존 만료 자동 삭제 구현
- [ ] 사건 자료 내보내기와 접근 감사 구현

### 4단계 — 운영 준비

- [ ] 위협 모델과 독립 보안 검토
- [ ] 제공처 철회·정책 변경·비상 중단 절차
- [ ] 백업·캐시를 포함한 삭제 검증
- [ ] 배포 전 개인정보 영향 및 법률 검토

## 테스트 전략과 성공 기준

### 테스트 전략

- 합성 GPS·카메라 목록을 이용한 후보 검색 단위 테스트
- GPS 정확도와 카메라 시각 오차에 따른 시간 범위 테스트
- 도로 방향·이동 속도·카메라 간격을 반영한 도착 시각 테스트
- 정책이 불명확하거나 권한이 없는 경우 보존 요청이 거부되는 테스트
- 보존 만료·삭제 요청·중복 요청 테스트
- 제공처 장애·응답 지연·약관 변경 테스트
- 인증 정보·정밀 위치·원본 영상이 로그에 노출되지 않는 테스트
- 실제 사람을 포함하지 않는 통합 테스트

### 성공 기준

- 공식 소스만 허용 목록에 등록할 수 있음
- 시스템이 영상 속 사람·차량을 자동 식별하지 않음
- GPS·카메라 위치·시각 오차로 합리적인 후보 시간 범위를 제시함
- 정책 검증 실패 시 보존 요청·자료 수집이 수행되지 않음
- 기본 동작이 메타데이터와 공식 보존 요청 중심임
- 모든 사건 자료에 보존 종료 시점이 있음
- 만료 자료와 삭제 요청 자료가 검증 가능하게 삭제됨
- 접근·요청·열람·삭제 이력이 감사 로그에 남음

## 설치와 실행

Python 3.11 이상이 필요하며, 실행에 필요한 외부 패키지는 없습니다.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"      # gps2cctv 명령과 테스트 도구(pytest) 설치
pytest                       # 테스트 실행
```

설치하지 않고 `PYTHONPATH=src python -m gps2cctv ...` 로 실행할 수도 있습니다.

### 합성 데이터로 따라 해 보기

아래 데이터는 모두 가상의 경로·카메라·제공처입니다(`fixtures/synthetic/`). 사건 ID·후보 ID·요청 ID는 실행할 때마다 달라지므로 출력에 나온 값을 넣으세요.

```bash
# 1) 데이터베이스 준비 (기본 위치: data/gps2cctv.sqlite3, 권한 0600)
gps2cctv init

# 2) 소스 등록 → 약관·절차 검토 기록 → 카메라 동기화
#    검토 기록 없이 동기화하면 거부됩니다(종료 코드 3).
gps2cctv source register fixtures/synthetic/sources.json
gps2cctv source review synthetic-city \
  --actions catalog_sync,catalog_lookup,preservation_request_prepare \
  --terms-ref synthetic-terms-v1 --reviewer "검토자 이름" --valid-days 30
gps2cctv camera sync synthetic-city

# 3) (선택) CCTV 화면 시각과 표준 시각을 같은 순간에 비교해 시각 오차 근거 기록
gps2cctv camera time-observe synthetic-city:CAM-A-001 \
  --reference-time 2026-10-24T12:00:00+09:00 \
  --camera-time   2026-10-24T11:59:42+09:00 --uncertainty 1

# 4) 사건 생성 → 경로 입력(사건 확인 범위 밖 지점은 저장하지 않음) → 후보 계산
gps2cctv incident create --time 2026-10-26T14:32:10+09:00 \
  --before 10m --after 5m --reason "합성 테스트: 블랙박스 미저장 가정"
gps2cctv incident import-route <사건ID> fixtures/synthetic/route.csv
gps2cctv incident candidates <사건ID>

# 5) 보존 요청 초안 → 사용자가 공식 절차로 제출 → 상태 기록
gps2cctv preservation prepare <후보ID> --out exports/request.txt
#    신뢰도 '낮음' 후보는 근거를 직접 확인한 뒤 --reviewed 를 붙여야 합니다.
gps2cctv preservation update <요청ID> --status submitted --provider-ref <제공처 접수번호>

# 6) 보고서·감사 로그·삭제
gps2cctv incident report <사건ID> --json --out exports/report.json
gps2cctv audit list
gps2cctv purge --dry-run                 # 보존 기간이 지난 자료 미리 보기
gps2cctv incident delete <사건ID> --yes   # 즉시 삭제(삭제 검증 결과 출력)
```

후보 계산 결과 예시(일부):

```text
[안내] 이 결과는 보존 요청이나 직접 확인이 필요한 '시간대 후보'입니다. 영상 속 사람·차량을 식별한 결과가 아니며, ...

1. [cand_…] 합성 2교차로 — 합성 도시 교통 CCTV (테스트용) (카메라 ID CAM-A-001)
   시간 범위 신뢰도: 높음
   예상 통과(표준시): 14:24:00   최근접 거리: 25m
   확인 범위(표준시): 14:23:48 ~ 14:24:10
   CCTV 시각 오차: -18.0초 ± 5.2초 (근거: 사용자 직접 관측)
   확인 범위(CCTV 시계): 14:23:25 ~ 14:23:57
   ▶ 보존 요청 권장 범위(CCTV 표시 시각): 2026-10-26 14:23:20 KST ~ 14:24:10
   제공처 보관 기간 기준 예상 삭제: 2026-11-25 14:23:20 KST (여유 있음)
```

### 설정

[`config.example.toml`](config.example.toml)을 `config.toml`로 복사해 `--config config.toml`(또는 환경 변수 `GPS2CCTV_CONFIG`)로 지정합니다. 모든 값의 기본값이 들어 있습니다. 실제 제공처를 쓰려면 약관·보존 절차를 검토한 뒤 `[policy] allowed_hosts`에 공식 엔드포인트 호스트를 추가해야 합니다. 비어 있으면 실제 소스는 등록 자체가 거부됩니다.

### 데이터 보관 위치와 삭제 범위

- 모든 자료는 SQLite 파일 하나(`data/` 아래, `.gitignore`에 포함)에 저장됩니다. 파일 권한은 소유자 전용(0600)이며 **아직 암호화하지 않습니다**.
- 삭제 시 SQLite `secure_delete`로 지운 내용을 파일 안에서 덮어씁니다. 프로그램은 자체 백업·캐시를 만들지 않지만, 사용자가 따로 복사한 DB 파일이나 내보낸 보고서(`exports/`)는 직접 지워야 합니다.

비밀키·토큰·개인 위치 이력은 `.env`, 로그, 커밋에 포함하지 않습니다.

## 프로젝트 구조

```text
src/gps2cctv/         애플리케이션 코드 (모듈 설명은 "권장 아키텍처" 표 참고)
tests/                단위·통합 테스트 (합성 데이터만 사용)
fixtures/synthetic/   합성 GPS 경로·소스·카메라 카탈로그
scripts/              합성 fixture 생성기
docs/PLAN.md          실행 계획과 문서 검토 결과
docs/decisions/       설계 결정 기록(ADR)
config.example.toml   설정 예시(기본값)
```

## 개인정보·보안·운영 체크리스트

- [ ] 사고 증거 보존이라는 사용 목적과 법적 근거를 문서화했는가?
- [ ] 본인 소유 또는 명시적 동의가 있는 환경인가?
- [ ] 제공처의 최신 약관과 공식 보존 절차를 확인했는가?
- [ ] 사람·차량 자동 식별 기능이 없는가?
- [ ] 사고 전후의 최소 시간·공간 범위만 다루는가?
- [ ] 위치 정밀도와 보존 기간을 최소화했는가?
- [ ] CCTV 시각 오차와 계산 근거를 기록하는가?
- [ ] 접근 권한과 암호화 키를 분리했는가?
- [ ] 삭제 요청과 만료 삭제를 검증할 수 있는가?
- [ ] 정책 변경·접근 거부·철회 시 요청과 수집을 중단하는가?
- [ ] 감사 로그에 불필요한 원본 개인정보가 남지 않는가?
- [ ] 테스트가 실제 사람·차량의 식별을 전제로 하지 않는가?
- [ ] 배포 전에 법률·보안 검토를 완료했는가?

## 기여 방법

1. 먼저 이 README와 [`docs/PLAN.md`](docs/PLAN.md)의 안전 범위를 확인합니다.
2. 새로운 제공처를 추가할 때 공식 문서·약관·보존 요청 절차를 함께 제시합니다.
3. 실제 개인 영상, 번호판, 얼굴, 인증정보, 정밀 이동 이력을 이슈·PR에 첨부하지 않습니다.
4. 자동 식별·추적, 정책 우회, 대량 수집 기능을 제안하지 않습니다.
5. 테스트와 문서 변경을 포함하고, 변경 목적과 데이터 처리 범위를 설명합니다.

## 라이선스·법적 고지·책임 제한

현재 별도의 오픈소스 라이선스가 확정되지 않았다면 코드를 재배포하거나 상업적으로 이용하기 전에 저장소 소유자의 허가를 받아야 합니다. 이 프로젝트는 법률 자문, CCTV 제공처의 이용 허가, 개인정보 보호 적합성, 사고 입증 가능성을 보증하지 않습니다.

사용자는 자신이 이용하는 국가·지역의 법률, 제공처 약관, 기관 정책을 직접 확인하고 모든 책임을 부담해야 합니다. 이 프로젝트의 작성자와 기여자는 무단 수집, 사생활 침해, 불법 감시, 약관 위반 또는 그에 따른 손해에 대해 책임을 지지 않습니다.

자세한 요구사항과 마일스톤은 [`docs/PLAN.md`](docs/PLAN.md)를 참고하세요.
