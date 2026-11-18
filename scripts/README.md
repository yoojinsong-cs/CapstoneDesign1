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
| `experiment.py` | 스케줄러 정책별 전송 시간·QoS 비교 |
| `show_flows.py` | PCAP의 TCP 연결과 논리적 flow 진단 |
| `export_packet_headers.py` | PCAP의 TCP/IP 헤더를 패킷 단위 CSV로 추출 |

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
| `flowsPerPair#` | 클라이언트당 플로우 수 |

TrafficGenerator 방식대로 CDF에서 흐름 크기를 뽑고, 모든 흐름을 bulk 전송합니다. 기본 분포는 `VL2_CDF.txt`이며 `--cdf`로 변경할 수 있습니다. 요청 메타데이터는 16바이트이며 앱 패턴이나 `app_mix`는 사용하지 않습니다.

### 예시

```bash
# 서버 2대, 클라이언트 2대, 엣지 1대씩, 코어(경로) 2개, 포트 5001, 총 200개 flow
sudo python3 main.py 2 2 1 1 2 5001 100

# 클라이언트마다 5,000개씩, 총 10,000개 flow 생성
sudo python3 main.py 2 2 1 1 2 5001 5000

# 클라이언트마다 1,000개씩, 총 2,000개 flow 생성
sudo python3 main.py 2 2 1 1 2 5001 1000
```

마지막 인자는 전체 flow 수가 아니라 **클라이언트당 flow 수**다. 따라서
클라이언트가 2대일 때 `2500`을 입력하면 총 5,000개이고, 총 10,000개가
필요하면 `5000`을 입력해야 한다.

`main.py`는 트래픽 완료 후 원본 PCAP을 `/tmp`에서 다음 영구 경로로 복사합니다.
분류기 학습용 특징 추출과 CSV 자동 생성은 제거했습니다.

```text
data/pcap/run_YYYYMMDD_HHMMSS_VL2_1000flows/client1.pcap
data/pcap/run_YYYYMMDD_HHMMSS_VL2_1000flows/client2.pcap
```

분포명은 실제 CDF 파일에서 가져옵니다. PCAP은 패킷·flow 진단에 사용할 수 있습니다.
기존 데이터와 실험 결과는 유지합니다.

정책별 전체 전송 시간과 수신 QoS 비교는 [EXPERIMENT.md](EXPERIMENT.md)를 참고하세요.
`experiment.py`는 Edge–Core 링크를 100Mbps로 설정하며 PCAP을 캡처하지 않습니다.

## TCP/IP 헤더 분석

스위치의 flow.id별 큐·0.5초 지연 모의 분류·MICE 엄격 우선순위 실험은
[scheduler/README.md](scheduler/README.md)를 참고하세요.

실험이 만든 PCAP을 패킷 1개당 CSV 1행으로 변환합니다. TCP payload
본문은 저장하지 않고, 16바이트 요청 메타데이터를 해석해 각 패킷에
`flow_id`, `flow_size`, `flow_tos`, `flow_rate`를 연결합니다.

```bash
python3 export_packet_headers.py /tmp/client1.pcap client1_headers.csv
```

특정 TCP 연결만 빠르게 확인하려면 `tcp.stream` 번호를 지정합니다.

```bash
python3 export_packet_headers.py /tmp/client1.pcap stream0_headers.csv --stream 0
```

주요 컬럼은 IP DSCP/ECN, TTL, checksum, TCP seq/ack, FIN/SYN/RST/PSH/ACK,
ECE/CWR, window, MSS, window scale, timestamp, RTT, retransmission, out-of-order,
duplicate ACK입니다. `ip_ecn_name` 값은 `Not-ECT`, `ECT(0)`, `ECT(1)`,
`CE` 중 하나이며, DCTCP 분석에서는 `CE`, `tcp_ece`, `tcp_cwr`를
함께 확인하면 됩니다.

## OVS 동작 방식

- Default OVS + `OVSController` 사용 (커스텀 OVS 불필요)
- 멀티패스는 OVS 표준 `select` group type (ECMP, equal-weight) 사용
- 링크 대역폭: Host ↔ Edge = 1000Mbps, Edge ↔ Core = 10Mbps
