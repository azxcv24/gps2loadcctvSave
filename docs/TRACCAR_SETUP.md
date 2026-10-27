# Traccar 앱 설정 가이드

휴대폰(또는 차량용 GPS 추적기)이 `gps2cctv serve` 서버로 위치를 보내도록 설정하는 방법입니다.
서버는 Traccar 서버와 같은 **OsmAnd 프로토콜**을 받으므로 공식 Traccar Client 앱을 그대로 쓸 수 있습니다.

## 1. 앱 설치

- Android: Google Play에서 "Traccar Client"
- iOS: App Store에서 "Traccar Client"

## 2. 앱 설정

앱 버전에 따라 메뉴 이름이 조금 다를 수 있습니다.

| 항목 | 설정값 | 이유 |
|---|---|---|
| 기기 식별자 (Device identifier) | `config.toml` 의 `[[devices]] id` 와 **똑같이** | 등록되지 않은 식별자는 서버가 거부합니다. 남이 추측하기 어렵게 10자 이상 임의 문자열 권장 |
| 서버 URL (Server URL) | `http://서버주소:5055` | 서버의 `[server] port` |
| 위치 정확도 (Location accuracy) | 높음 (High) | 카메라 통과 시각 계산 정확도 |
| 빈도/간격 (Frequency, Interval) | **1~5초** | 기본값(300초)이면 카메라를 미리 녹화 대기할 수 없습니다 |
| 거리 (Distance) | 0 | 시간 간격으로만 보내기 |
| 각도 (Angle) | 0 | |
| 오프라인 버퍼링 (Offline buffering) | 켬 | 통신이 끊겼다 이어져도 위치 기록 유지 |
| 배터리 최적화 | 앱을 예외로 지정 (Android) | 화면이 꺼져도 계속 전송 |

설정 후 서비스를 켜면(Service status ON) 위치 전송이 시작됩니다.

## 3. 동작 확인

서버에서:

```bash
curl http://서버주소:5055/health
```

`"accepted"` 값이 늘어나면 위치를 받고 있는 것입니다. `"unknown_device"` 가 늘어나면 기기 식별자가
`config.toml` 과 다릅니다.

서버 로그에 `녹화 대기 시작`, `통과 감지`, `클립 saved` 가 보이면 정상 동작입니다.

```bash
gps2cctv --config config.toml clips list
```

## 4. 서버를 어디에 둘까

- 휴대폰이 서버에 접속할 수 있어야 합니다. 집 서버라면 공유기 포트 포워딩(5055), 또는
  Tailscale 같은 VPN으로 휴대폰과 서버를 같은 사설망에 두는 방법이 간단하고 안전합니다.
- 인터넷에 직접 열 때는 위치가 암호화되지 않은 HTTP로 오가므로, nginx·Caddy 같은 리버스 프록시로
  HTTPS를 붙이고 앱의 서버 URL을 `https://...` 로 지정하는 것을 권장합니다.
- 녹화는 서버가 하므로 휴대폰 데이터는 위치 전송(아주 적음)만 씁니다. 서버는 카메라 스트림을
  동시에 최대 `max_concurrent_streams`개(기본 6) 받습니다.

## 5. 사고가 났을 때

- 급정거(3초 안에 약 25km/h 이상 감속 후 정지)나 앱의 SOS 알람이 오면 자동으로 앞 10분·뒤 5분 클립을
  **보호**(자동 삭제 안 함)하고, 5분 뒤 사건 기록과 보존 요청 후보까지 만들어 둡니다.
- 직접 표시하려면: `gps2cctv --config config.toml clips mark --device 기기식별자 --time 2026-10-27T14:32:10+09:00`
- 보호된 클립만 보기: `gps2cctv --config config.toml clips list --protected`
