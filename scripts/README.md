## Overview

Mininet 기반 트래픽 생성 에뮬레이터. Default OVS(OpenFlow)를 사용하는 2-stage Clos 네트워크 토폴로지에서 멀티패스 트래픽을 생성합니다.

```
serverHost --- Hedges --- Edges --- core --- Edgec --- Hedgec --- clientHost
               (20.0.0.0/24)                           (20.0.1.0/24)
```

## 파일 구성

| 파일 | 설명 |
|------|------|
| `main.py` | 진입점. 토폴로지 구성 → 룰 설치 → 트래픽 생성 순으로 실행 |
| `topoBuilder_lb.py` | Mininet 토폴로지 빌더 (default OVSController 사용) |
| `ruleInstaller_lb.py` | OpenFlow 룰 설치기 (ECMP: standard select group) |
| `flowGenerator_parll.py` | TrafficGenerator를 이용한 병렬 트래픽 생성 |

## 설치

1. **Mininet 설치**
   ```bash
   git clone https://github.com/mininet/mininet.git && cd mininet
   sudo ./util/install.sh -a
   ```

2. **TrafficGenerator 설치**
   ```bash
   git clone https://github.com/HKUST-SING/TrafficGenerator.git
   ```

## 실행 방법

```bash
sudo python3 main.py <server#> <client#> <serverEdge#> <clientEdge#> <paths#> <port#> <flowsPerPair#>
```

### 인자 설명

| 인자 | 설명 |
|------|------|
| `server#` | 서버 호스트 수 |
| `client#` | 클라이언트 호스트 수 |
| `serverEdge#` | 서버 쪽 엣지 스위치 수 |
| `clientEdge#` | 클라이언트 쪽 엣지 스위치 수 |
| `paths#` | 코어 스위치 수 (멀티패스 경로 수) |
| `port#` | TrafficGenerator 서버 포트 |
| `flowsPerPair#` | 클라이언트-서버 쌍당 플로우 수 |

TrafficGenerator 원본 방식대로 `DCTCP_CDF.txt`에서 흐름 크기를 뽑고, 모든 흐름을 연속 bulk 전송합니다. 요청 메타데이터는 16바이트이며 앱 패턴이나 `app_mix`는 사용하지 않습니다.

### 예시

```bash
# 서버 2대, 클라이언트 2대, 엣지 1대씩, 코어(경로) 2개, 포트 5001, 플로우 100개
sudo python3 main.py 2 2 1 1 2 5001 100
```

## OVS 동작 방식

- Default OVS + `OVSController` 사용 (커스텀 OVS 불필요)
- 멀티패스는 OVS 표준 `select` group type (ECMP, equal-weight) 사용
- 링크 대역폭: Host ↔ Edge = 1000Mbps, Edge ↔ Core = 10Mbps
