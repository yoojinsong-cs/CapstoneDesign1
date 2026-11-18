# 정책별 전체 전송 완료 시간 실험

`scripts/`에서 기존 main.py와 같은 순서로 토폴로지 인자를 입력한다.

```bash
sudo python3 experiment.py 서버수 클라이언트수 서버엣지수 클라이언트엣지수 코어경로수 포트 클라이언트당flow수
```

예를 들어 서버 2대·클라이언트 2대·양쪽 엣지 1대·코어 2경로에서 총 100개
flow를 생성하고, MICE 우선 정책과 legacy를 각각 한 번 실행하려면:

```bash
sudo python3 experiment.py 2 2 1 1 2 5001 50
```

마지막 인자는 **클라이언트당 flow 수**이므로 위 예시는 `2 × 50 = 100`개다.
클라이언트는 기존 generator 방식대로 서버에 순환 배정된다.

실행 중에는 현재 정책과 전체 실행 순서, 준비·전송·정리 단계가 표시된다.
전송 중에는 약 2초마다 완료 flow 수·백분율·경과 시간·client별 완료 수를 갱신한다.
30초 이상 새 flow가 완료되지 않으면 그 대기 시간도 표시한다.
완료율은 flow 개수 기준이며 전송 바이트 기준은 아니다.
최신 진행 상태는 결과 폴더의 `progress.json`에도 저장된다.

완료되면 정책별로 다음 형식의 실제 측정값을 출력한다.

```text
RESULT policy=priority repeat=1 completed=100/100 total_transfer_seconds=...
RESULT policy=legacy repeat=1 completed=100/100 total_transfer_seconds=...
```

측정 구간은 **모든 클라이언트 중 첫 flow 요청을 보낸 시각부터 마지막 flow의
데이터 수신이 끝난 시각까지**다. 클라이언트 프로그램이 기록한 공통 monotonic
시계를 사용한다. 토폴로지 구성·연결 준비·종료 정리·PCAP/CSV 처리 시간은 포함하지
않으며, 요청 사이의 generator 도착 간격은 포함한다. 시간 측정 실험에서는
PCAP 캡처와 CNN CSV 변환을 수행하지 않는다.

기본 설정은 두 정책 각 1회, DCTCP CDF, 클라이언트당 900Mbps 요청 부하,
모의 분류 지연 0.5초, MICE 기준 1,000,000바이트다. 두 정책은 같은 seed로
크기를 생성하며, client별 flow 크기 구성이 다르거나 미완료 flow가 있으면 실패로 처리한다.
priority는 분류 전 UNKNOWN과 MICE를 높은 등급에서 처리하고, 0.5초 뒤
ELEPHANT로 확인된 flow만 낮은 등급으로 내린다. 같은 등급에서는 DRR을 사용하며,
높은 등급에 데이터가 있으면 낮은 등급의 데이터를 선택하지 않는다.
새 대형 flow도 분류 전까지 높은 등급을 받는다. 분류로 등급이 바뀌어도 기존
대기 패킷은 보존하며 대역폭 배분 비율이나 대기 시간에 따른 승격은 없다.
실험 스크립트에서는 Host 링크 1Gbps와 Edge-Core 링크 **100Mbps 고정**을 두 정책에
동일하게 적용한다. priority에서도 같은 Edge-Core HTB가 100Mbps 전송 제한을
담당하고, `flowprio`는 그 아래에서 다음 패킷만 선택한다.
일반 `main.py` 및 공통 토폴로지·스케줄러의 기본값은 기존 10Mbps를 유지한다.

반복 횟수와 정책, 분포를 변경할 수 있다.

```bash
# 정책별 5회, 총 10회
sudo python3 experiment.py 2 2 1 1 2 5001 50 --repeat 5

# 우선순위 정책만 실행
sudo python3 experiment.py 3 4 2 3 2 5001 25 --policy priority

# 다른 분포와 seed 지정
sudo python3 experiment.py 2 2 1 1 2 5001 50 \
  --cdf TrafficGenerator/conf/VL2_CDF.txt --seed 1234
```

`--policy legacy`, `--classifier-delay`, `--flow-threshold`, `--load-mbps`,
`--output-dir`도 지원한다. 기본 출력 위치는 `data/experiments/<시각_PID>/`이며,
정책별 `timing.json`, flow/FCT 로그, 실행 설정과 전체 `summary.csv`를 보존한다.
`summary.txt`에는 실험 조건, 회차별 정책·완료 수·전체 전송 시간과 정책별 평균을
간단히 정리한다. 실행이 중단되거나 실패한 경우도 표시하며, 평균에는 완료된
실행만 포함한다.
기존 결과 폴더나 실행 중인 같은 이름의 스위치는 덮어쓰지 않는다.

## 수신 측 QoS

기존 실행 명령 그대로 QoS도 자동 기록한다. `summary.txt`와
`qos_summary.txt`에서 정책별 ALL/MICE/ELEPHANT의 평균·p95·p99·최대 FCT와
수신 goodput을 확인할 수 있다. FCT는 **클라이언트 요청부터 전체 데이터 수신
완료까지**의 시간(ms)이다. MICE는 설정된 크기 기준 미만, ELEPHANT는 이상이다.
p95·p99는 완료된 flow들을 합쳐 nearest-rank 방식으로 계산한다.

`qos.csv`에는 회차·정책·클라이언트·크기 분류별 통계를, 각 실행의 `qos.json`에는
그 실행의 상세 통계를 저장한다. 수신 goodput은 수신 페이로드 바이트를 실제
전송 구간으로 나눈 Mbps이며, 클라이언트별 값은 해당 클라이언트의 구간을 사용한다.
개별 flow의 goodput 통계는 각 flow의 바이트/FCT로 별도 계산한다.

완료 시간 목표가 있으면 지정한 시간 초과 비율도 기록한다. 아래 100ms는
사용자가 정하는 목표의 예시이며 기본 목표는 없다.

```bash
sudo python3 experiment.py 2 2 1 1 2 5001 50 --repeat 3 --qos-deadline-ms 100
```

단방향 패킷 지연·지터·패킷 손실률은 현재 계측하지 않는다.
중단·실패한 실행은 완료 flow 통계에서 제외하고 `status.json`에 표시한다.

기존 실험도 수신 로그가 남아 있으면 재실행 없이 계산할 수 있다. 이 명령은
기존 결과에 `qos.json`, `qos.csv`, `qos_summary.txt`를 추가하며 기존
`summary.txt`는 변경하지 않는다. `sudo`는 필요 없다.

```bash
python3 -m scheduler.qos data/experiments/결과폴더
```

우선순위 정책은 GCC 12와 현재 커널용 헤더가 필요하며, 실행 시 모듈이 없으면
빌드·로드한다. 이미 로드된 모듈의 서버 포트와 실행 포트가 다르면 중단한다.
예전 정책 모듈이 로드되어 있으면 버전을 확인하고 사용 중인 큐가 없을 때만
새 모듈을 준비한 뒤 교체한다. 사용 중이면 중단하며 강제 제거하지 않는다.

## fair와 priority의 QoS 비교

```bash
# 총 100개 flow, fair/priority 각 1회
sudo python3 experiment.py 2 2 1 1 2 5001 50 --compare-fair

# 각 3회, 총 6회
sudo python3 experiment.py 2 2 1 1 2 5001 50 --compare-fair --repeat 3
```

두 정책은 같은 상위 HTB 100Mbps 속도 제어·커스텀 큐·큐 한도·IP 쌍별 고정 경로를 쓴다.
fair는 모든 flow를 같은 등급으로 DRR 처리하고, priority만 UNKNOWN/MICE를
ELEPHANT보다 먼저 처리한다. 기본 DCTCP CDF와 0.5초 분류 지연은 유지한다.
같은 회차는 같은 seed로 생성하고, 회차마다 두 정책의 실행 순서를 뒤집는다.
`--compare-fair`는 `--policy` 또는 `--diagnose`와 함께 사용하지 않는다.

QoS 결과는 위와 같이 자동 저장한다. TCP 재전송·큐 drop·경로 전송량도 기록하며
`diagnosis.txt`에서 함께 확인한다. 설정과 manifest에는 정책 버전 및
`unknown_mice_first`를 기록하므로 이전 정책 결과와 구별할 수 있다.

## 완료 시간 차이 원인 확인

```bash
sudo python3 experiment.py 2 2 1 1 2 5001 50 --diagnose
```

진단 모드에서는 같은 seed와 같은 IP 쌍별 고정 경로로 legacy, fifo, fair,
priority를 각각 1회 실행한다. CDF·크기 생성·도착 간격·100Mbps 설정은 유지한다.
기본 실행의 ECMP 경로 선택은 변경하지 않는다.

- legacy와 fifo: 같은 HTB 아래 기본 leaf와 커스텀 leaf의 차이
- fifo와 fair: 같은 커스텀 큐에서 flow별 공평 분배 차이
- fair와 priority: 같은 큐 한도·속도 제어에서 MICE 우선순위 차이

각 실행 폴더에 `diagnostics.json`(TCP 재전송 및 인터페이스별 큐 drop·전송량),
`network_before.json`/`network_after.json`(원본 카운터), `link_samples.jsonl`
(전송 중 인터페이스 누적량), `ovs_before_*.txt`/`ovs_after_*.txt`를 남긴다.
전체 비교는 `diagnosis.txt`에 요약한다. 기존 실험 결과는 덮어쓰지 않는다.
