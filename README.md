# GPS 기반 공공 CCTV 자동 녹화 · 사고 증거 보존 도구

> 휴대폰(Traccar 앱)이 GPS만 보내면, 내가(또는 내 차가) 지나가는 공공 CCTV를 **알아서** 미리 녹화 대기하고, 실제로 지나간 구간 **+ 앞뒤 2초**를 자동으로 저장합니다. 블랙박스가 고장 났거나 영상이 안 남았을 때를 대비한 개인 증거 보존 도구입니다.

## 이렇게 동작합니다

```text
휴대폰 Traccar 앱 ──(1~5초마다 GPS)──▶ gps2cctv serve
                                         │
   1. 주변 CCTV 목록 자동 갱신 ◀──────────┤  국가교통정보센터(ITS) 오픈 API / 내 카메라 목록
   2. 진행 방향 앞쪽 60초 안에 지나갈 카메라 → 스트림 미리 녹화 대기(디스크 버퍼)
   3. GPS 경로가 카메라 촬영 반경을 지나감 → 진입·이탈 시각 계산
   4. [진입 − 2초, 이탈 + 2초] 구간만 잘라 저장 → 원본(.orig.ts) + 보기용(.mp4) + 근거(.json)
   5. 급정거·SOS → 앞 10분·뒤 5분 클립 보호 + 사건·보존 요청 후보 자동 생성
   6. 보호하지 않은 클립은 14일 뒤, 용량(20GB) 넘으면 오래된 것부터 자동 삭제
```

- 공공 CCTV 스트림은 실제보다 몇 초~수십 초 늦게 나오므로, 지나가기 직전에 녹화를 시작해도 앞부분을 놓치지 않습니다.
- 영상 속 차량·사람을 인식하지 않습니다. **"언제 어느 카메라 앞을 지나갔는가"는 GPS로만 판단**합니다.
- 클립 시각은 스트림의 시각 태그(`#EXT-X-PROGRAM-DATE-TIME`)가 있으면 그것으로(오차 ±1초), 없으면 수신 시각에서 지연을 빼서 추정합니다(오차 ±4~6초). 차가 잘리지 않도록 이 오차만큼 앞뒤를 더 저장합니다.

## 빠른 시작

### 1) 준비물

- 서버 한 대(집 PC·NAS·클라우드 VM). Docker 또는 Python 3.11+와 ffmpeg
- 국가교통정보센터 ITS 오픈 API 키(its.go.kr 공개 데이터에서 발급, CCTV 정보)
- 휴대폰에 Traccar Client 앱

### 2) 설정

```bash
cp config.example.toml config.toml
```

`config.toml` 에서 두 가지만 고치면 됩니다.

```toml
[[devices]]
id = "my-phone-7f3a9c2e51"   # Traccar 앱의 '기기 식별자'와 똑같이(10자 이상 임의 문자열 권장)

[its]
api_key = "발급받은 키"       # 또는 환경 변수 GPS2CCTV_ITS_API_KEY
```

### 3) 실행

Docker:

```bash
GPS2CCTV_ITS_API_KEY=발급받은키 docker compose up -d --build
docker compose logs -f                      # '녹화 대기 시작', '통과 감지', '클립 saved'
```

Docker 없이:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e .
gps2cctv --config config.toml serve
```

### 4) 휴대폰 설정

Traccar Client 앱에서 서버 URL을 `http://서버주소:5055`, 기기 식별자를 위 `id`와 같게, **전송 간격을 1~5초**로 맞춥니다(기본 300초로는 동작하지 않습니다). 자세한 내용은 [`docs/TRACCAR_SETUP.md`](docs/TRACCAR_SETUP.md).

### 5) 저장된 영상 보기

```bash
gps2cctv --config config.toml clips list              # 최근 클립
gps2cctv --config config.toml clips list --protected  # 사고 의심으로 보호된 클립
gps2cctv --config config.toml clips protect <클립ID>   # 자동 삭제되지 않게 보호
gps2cctv --config config.toml clips mark --device <기기ID> --time 2026-10-27T14:32:10+09:00  # 사고 시점 표시
```

클립 파일은 `data/clips/<기기>/<날짜>/` 아래에 있습니다(Docker는 `docker compose cp gps2cctv:/data/clips ./clips`).

| 파일 | 내용 |
|---|---|
| `HHMMSS_카메라_xxxxxx.mp4` | 보기용. 통과 구간 + 앞뒤 여유만 정확히 자른 영상 |
| `HHMMSS_카메라_xxxxxx.orig.ts` | 원본. 스트림에서 받은 세그먼트를 가공 없이 이어 붙인 것(SHA-256 기록, 증거용) |
| `HHMMSS_카메라_xxxxxx.json` | 카메라, 진입·이탈 시각, 시각 추정 방법·오차, 세그먼트별 시각, 해시 |

## 현재 구현 상태 (v0.2)

| 구분 | 상태 |
|---|---|
| Traccar GPS 수신(OsmAnd 쿼리·JSON·Traccar 서버 전달 형식), 등록 기기만 허용 | 구현 |
| ITS CCTV 오픈 API 연동(기기 주변 타일만 갱신, 스트림 주소 만료 대비 재요청) | 구현 — 실제 키로는 아직 미검증(아래 주의) |
| 본인 소유 카메라 목록(RTSP/HLS) | 구현 |
| 앞쪽 카메라 미리 녹화 대기, HLS 녹화(시각 태그·수신 시각 추정), RTSP 등은 ffmpeg 녹화 | 구현 |
| 통과 감지(GPS 공백 구간 포함), 클립 자르기·원본 해시·보기용 mp4 | 구현 |
| 기기 2대 이상(휴대폰 + 차량 추적기) 동시 처리 | 구현 |
| 카메라 앞 정차 시 최대 길이(180초)로 한 번만 저장 | 구현 |
| 급정거·SOS·수동 표시 → 클립 보호, 사건·보존 요청 후보 자동 생성 | 구현 |
| 클립·위치 자동 삭제(기간·용량), 감사 로그 | 구현 |
| 수동 사건 처리(경로 파일 → 후보 시간 범위 → 보존 요청 초안) | 구현(v0.1) |
| Docker / docker compose | 구현 |
| 저장 시 암호화 | 미구현. 파일·DB는 소유자 전용 권한(0600)으로만 보호 |
| 영상 속 차량·번호판·얼굴 인식 | 하지 않음(설계상 제외) |

검증 방법: 테스트 154개. 그중 통합 테스트는 ffmpeg로 실제 실시간 HLS 스트림을 띄우고, Traccar 형식 HTTP로 GPS를 실시간 전송해 카메라 앞을 지나간 뒤 저장된 mp4 길이가 "통과 구간 + 앞뒤 2초 + 시각 오차"와 맞는지 ffprobe로 확인합니다.

### 주의

- **ITS API 형식**: 파라미터(`apiKey`, `type`, `cctvType`, `minX`…`maxY`, `getType`)와 응답 필드(`cctvurl`, `coordx`, `coordy`, `cctvname`)는 공개 자료 검색으로 확인했고, 공식 문서를 직접 열어 보거나 실제 키로 호출해 보지는 못했습니다. JSON·XML 응답을 모두 처리하도록 했습니다. 처음 실행할 때 로그에 `ITS 타일 … 갱신: 카메라 N대`가 나오는지 확인하세요. 0대면 `cctv_type = 1`로 바꿔 보세요.
- **ITS 스트림의 실제 지연·시각 태그 여부**는 아직 확인하지 못했습니다. 시각 태그가 없으면 앞뒤 여유가 2초보다 길어집니다(기본 각 6초 안팎). 카메라 화면의 시계와 비교해 `stream_delay_seconds`를 맞추면 줄일 수 있습니다.
- 공공데이터 이용 조건(공공누리 유형 등)은 ITS 누리집에서 확인하세요. 저장한 클립은 본인 사고·분쟁 증거 보관 용도로만 쓰고, 다른 사람이 함께 찍힌 영상을 공개·배포하지 마세요.
- 클립은 법적 증거 효력을 보증하지 않습니다. 원본이 필요하면 도로 관리 기관에 정식으로 보존·제공을 요청하세요(`incident`·`preservation` 명령이 요청 초안을 만들어 줍니다).

## 프로젝트를 시작한 이유

블랙박스가 고장 났거나 사고 당시 영상이 저장되지 않으면, 사고가 발생한 시간과 장소를 알고 있어도 주변 공공 CCTV·고속도로 CCTV 자료를 제때 확보하지 못해 사실관계를 입증하기 어려울 수 있습니다. 사고가 난 뒤에 보존을 요청하면 이미 지워졌거나 절차가 늦을 수 있습니다.

그래서 GPS만 보내 두면 **지나가는 순간의 공공 CCTV 영상을 그때그때 자동으로 남겨 두는** 것을 목표로 합니다. 사고가 나면 그 앞뒤 클립을 보호하고, 공식 보존 요청에 필요한 카메라·시간 범위도 정리해 둡니다.

## 하는 것과 하지 않는 것

### 하는 것

- 등록한 내 기기(휴대폰·차량 추적기)의 GPS로 지나가는 공공 CCTV를 판단해 통과 구간만 자동 저장
- 카메라별 시각 오차·GPS 오차를 반영해 차가 잘리지 않는 범위로 저장
- 사고 의심 시 클립 보호, 사건 기록·보존 요청 후보 자동 생성
- 저장·조회·보호·삭제를 감사 로그로 기록하고, 기간·용량에 따라 자동 삭제

### 하지 않는 것

- 영상 속 얼굴·차량·번호판 인식, 영상으로 사람·차량 찾기
- 등록하지 않은 기기·다른 사람의 위치 추적
- 로그인 우회, 접근 제한 회피, 비공식 스트림 주소 추측(공식 API가 준 주소와 내 카메라만 사용)
- 내가 지나가지 않은 구간의 CCTV 상시 녹화(녹화 대기는 내가 다가가는 카메라만, 버퍼는 5분 뒤 삭제)

## 핵심 원칙

1. **GPS로 판단, 영상은 해석하지 않음** — 언제 어느 카메라 앞을 지났는지는 GPS와 카메라 위치로만 계산합니다.
2. **놓치지 않게, 그러나 필요한 만큼만** — 통과 구간 + 앞뒤 여유만 저장하고, 시각 오차만큼만 넓힙니다.
3. **원본 보존** — 받은 세그먼트를 가공 없이 저장하고 해시를 남깁니다. 보기용 mp4는 따로 만듭니다.
4. **시각 근거 기록** — 시각을 어떻게 추정했는지(태그/수신 시각), 오차가 얼마인지 클립마다 남깁니다.
5. **자동 정리** — 보호하지 않은 클립·위치는 기간이 지나면 지웁니다. 사고 의심 구간은 자동 보호합니다.
6. **설정이 곧 승인** — 설정 파일에 적은 카메라 소스만 씁니다. 소스를 끄면(`source disable`) 즉시 녹화가 멈춥니다.

---

아래는 v0.1에서 만든 **수동 사건 처리**(경로 파일로 후보 시간 범위 계산 → 보존 요청 초안) 설명입니다. 자동 녹화가 놓친 카메라(스트림이 없는 지자체 CCTV 등)의 원본을 공식 절차로 요청할 때 씁니다.

## 수동 사건 처리 흐름

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

## 평상시 기록과 사고 시 보존 (v0.2)

### 평상시 (자동)

- 등록한 기기의 GPS를 받아 위치 기록(기본 14일 보관)
- 내가 다가가는 카메라만 녹화 대기, 지나간 구간 + 앞뒤 여유만 클립으로 저장(기본 14일 보관)
- 녹화 대기 버퍼는 지나간 뒤 5분 안에 삭제, 통과하지 않은 카메라 영상은 남기지 않음
- 전체 용량 상한(기본 20GB)을 넘으면 보호하지 않은 오래된 클립부터 삭제

### 사고·이상 상황 발생 시

- 급정거·SOS 알람 자동 감지, 또는 `clips mark` 로 직접 표시
- 앞 10분·뒤 5분 클립을 보호(자동 삭제 안 함). 이후 저장되는 그 구간 클립도 보호
- 5분 뒤 사건을 자동으로 만들고 경로를 넣어 주변 카메라·보존 요청 시간 범위까지 계산
- 스트림이 없던 카메라는 `preservation prepare` 로 공식 보존 요청 초안을 만들어 제출

## 권장 아키텍처

- **GPS/사건 입력**: 사용자 동의 아래 GPS 경로, 사고 시점, 사고 전후 범위 입력
- **경로·시간 추정**: 위치 오차, 이동 속도, 카메라 위치, 시각 오차를 이용해 후보 범위 산정
- **CCTV Catalog Adapter**: 공공데이터 포털·기관 API 등 공식 제공처별 어댑터
- **Nearby Search**: 경로 주변 카메라, 도로 방향, 운영 상태, 제공 정책 검색
- **Policy & Permission Guard**: 허용 목록, 이용약관, 인증 상태, 요청 제한, 보존 정책 검증
- **Preservation Request Service**: 공식 보존 요청에 필요한 카메라·시간·사유·연락처·상태 관리
- **Capture Service (v0.2)**: 다가가는 카메라 스트림 녹화 대기, 통과 구간 클립 저장. `video` 기능이 있고 정책에서 녹화를 허용한 소스만
- **Storage**: 사건 단위 암호화 저장소와 접근 제어
- **Retention & Deletion**: 보존 기한, 삭제 요청, 계정 삭제, 소스 철회 처리
- **Audit Log**: 조회·요청·열람·내보내기·삭제·정책 변경 기록

구현 위치는 다음과 같습니다(`src/gps2cctv/`).

| 구성 요소 | 모듈 | 비고 |
|---|---|---|
| GPS/사건 입력 | `incidents.py`, `routeio.py` | CSV·GPX |
| 경로·시간 추정 | `candidates.py`, `geo.py`, `timeutil.py` | |
| CCTV Catalog Adapter | `catalog.py`, `live/its.py` | 로컬 JSON, ITS 오픈 API(주변 타일) |
| Nearby Search | `candidates.py` | |
| Policy & Permission Guard | `policy.py` | 실패 시 거부 |
| Preservation Request Service | `preservation.py` | 초안·상태 관리만, 자동 제출 없음 |
| GPS 수신(Traccar) | `live/ingest.py` | OsmAnd 쿼리·JSON·서버 전달 형식 |
| Capture Service | `live/engine.py`, `live/recorder.py`, `live/hls.py`, `live/clips.py` | 녹화 대기·통과 감지·클립 저장 |
| 실행 | `live/serve.py`, `live/config.py` | `gps2cctv serve` |
| Storage | `db.py` | SQLite, 0600, secure_delete, 암호화 없음 |
| Retention & Deletion | `retention.py` | 만료 삭제·삭제 검증 |
| Audit Log | `audit.py` | 자유 텍스트·좌표 기록 불가 |

## 데이터 모델 (스키마 v2)

아래는 `src/gps2cctv/db.py`의 실제 스키마를 요약한 것입니다. 초안 대비 바뀐 점은 [`docs/PLAN.md`](docs/PLAN.md) 13절에 정리했습니다. v0.1(스키마 1) DB는 처음 열 때 자료를 잃지 않고 v2로 바뀝니다.

v0.2에서 추가된 표:

```text
position             수신한 GPS(기기별, 기본 14일 보관)
- device_id, recorded_at, received_at, latitude, longitude, accuracy_m, speed_mps, heading_deg
- retention_until

clip                 자동 저장한 통과 구간
- id, device_id, camera_id, source_id, provider_camera_id, camera_name
- entry_at, exit_at, estimated_passage_at, closest_distance_m
- window_start/end(통과 + 앞뒤 여유), covered_start/end(실제 저장된 범위)
- time_mapping: program_date_time | receive_time | ffmpeg_segment | mixed, time_uncertainty_seconds
- status: saved | partial | failed, failure_reason
- original_path/sha256/bytes, viewing_path/sha256, metadata_path, flags
- protected, retention_until, created_at

live_event           사고 의심 이벤트(급정거·알람·수동 표시)
- device_id, kind, occurred_at, detail, latitude, longitude
- protect_from/until(이 구간 클립 보호), incident_id(자동 생성 사건), processed

camera (+열)          stream_url, stream_url_fetched_at, stream_delay_seconds
cctv_source.environment  synthetic | production | owned(본인 소유 장비)
```

v0.1부터 있던 표:

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

## 수동 사건 처리: 설치와 실행

Python 3.11 이상이 필요하며, 실행에 필요한 외부 패키지는 없습니다(자동 녹화의 mp4 사본과 RTSP 녹화에는 ffmpeg 필요).

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"      # gps2cctv 명령과 테스트 도구(pytest) 설치
pytest                       # 테스트 실행(ffmpeg가 있으면 실제 스트림 통합 테스트 포함, 약 30초)
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
src/gps2cctv/live/    실시간 자동 녹화(GPS 수신, ITS, 녹화기, 클립, serve)
tests/                단위·시뮬레이션·통합 테스트 (합성 데이터·테스트 패턴 영상만 사용)
fixtures/synthetic/   합성 GPS 경로·소스·카메라 카탈로그
scripts/              합성 fixture 생성기
docs/PLAN.md          실행 계획과 문서 검토 결과
docs/decisions/       설계 결정 기록(ADR)
docs/TRACCAR_SETUP.md Traccar 앱 설정 가이드
config.example.toml   설정 예시(기본값)
Dockerfile, docker-compose.yml  서버 실행
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
