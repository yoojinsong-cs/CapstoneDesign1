# 스위치의 flow.id 기반 스케줄링

토폴로지 인자를 받아 두 정책의 전체 전송 완료 시간을 비교하려면
[experiment.py 실행 안내](../EXPERIMENT.md)를 참고한다.

Mininet OVS의 `Edges1 → core들` 출력 포트마다 기존 HTB 속도제어
클래스 아래에 `flowprio` 커널 leaf qdisc를 설치한다.
TrafficGenerator의 TCP 연결 재사용과 16바이트 메타데이터 형식은 유지한다.
`main.py --scheduler priority`가 설치·분류 프로세스·종료 정리를 연결한다.
기존 명령의 기본값은 `legacy`이므로 옵션 없이 실행한 실험은 이전 방식이다.

이 모듈의 담당 범위는 `flow.id` 추적과 스위치 스케줄링이다. 분류 모델 개발은
별도이며, 여기서는 최초 application-data 관측 후 기본 0.5초가 지나면 해당
flow의 메타데이터 크기를 기준으로 등급을 반환하는 모의 분류기를 사용한다.

커널 소스를 사용자 공간 어댑터와 함께 실행하는 테스트와 실제 커널/Mininet
통합 검증은 구별한다. 실제 실행 검증은 아래 TrafficGenerator 실험으로 수행한다.

## 실행

현재 커널에 맞는 헤더와 GCC 12가 필요하다. 빌드된 모듈이 현재 소스 및 커널과
일치하면 실행기는 기존 `.ko`를 사용할 수 있다. 새 빌드가 필요한 경우:

```bash
sudo apt-get install gcc-12
make -C scheduler/kernel
```

현재 정책 버전은 3 (`HTB > UNKNOWN/MICE > ELEPHANT`)이다. 실행기는 로드된 모듈의
버전을 확인한다. 예전 모듈이 사용 중이지 않으면 새 모듈을 준비한 뒤 보통의
`rmmod`/`insmod`로 교체한다. 기존 큐가 사용 중이면 중단하며 강제 제거하지 않는다.

TrafficGenerator 실험:

```bash
sudo python3 main.py 2 2 1 1 1 5001 20 \
  --scheduler priority --classifier-delay 0.5 \
  --flow-threshold 1000000 \
  --seed 1234 --load-mbps 4 \
  --cdf TrafficGenerator/conf/DCTCP_CDF.txt
```

`--load-mbps`는 클라이언트별 요청 부하다. 위 예시는 합계 약 8Mbps이며,
클라이언트마다 20개, 총 40개 flow다. 크기는 CDF에서 무작위 생성되므로 실제
실행시간은 달라진다.

비교 모드는 `--scheduler fifo`, `--scheduler fair`, `--scheduler priority`다.
같은 seed/CDF/load로 비교한다. `fifo`와 `fair`도 같은 위치와 같은 HTB
속도제어, 큐 한도를 사용한다. 모든 모드는 같은 토폴로지와 링크 대역폭을
사용한다. `legacy`는 기존 TCLink HTB leaf를 유지하고, 나머지 모드는
서버 측 코어 방향의 기존 HTB `5:1` 클래스 아래에 `flowprio`를 leaf로
붙인다. HTB가 속도제어와 burst credit을 담당하고 `flowprio`는 패킷
선택만 하는 work-conserving scheduler이다.

## 100개 flow씩 정책별 5회 비교

`scripts/`에서 다음 명령으로 실행한다.

```bash
sudo python3 -m scheduler.benchmark
```

기존 서버 2대·클라이언트 2대·코어 2경로 토폴로지를 사용한다. Host 링크는
1Gbps, Edge-Core 링크는 양쪽 모두 10Mbps로 동일하다. VL2 CDF와 클라이언트당
900Mbps 요청 부하를 유지한다. 회차별 총 100개(클라이언트당 50개)를 생성하며,
priority/legacy 각 5회, 총 10회 실행한다. priority의 분류 지연은 0.5초다.
같은 비교 회차는 같은 seed를 쓰며, 먼저 실행하는 정책은 회차마다 교대한다.

`data/comparisons/<실행>/` 아래 각 실행의 FCT·manifest·로그를 보존하고,
`summary.csv`에 전체/MICE/ELEPHANT의 완료 수와 평균·중앙값·p99 FCT(ms)를 기록한다.
클라이언트별 완료 수와 비교 쌍의 flow 크기 구성이 일치해야 결과에 포함한다.
준비만 하려면 `--prepare-only`, 출력 폴더를 지정하려면 `--output PATH`를 사용한다.
`status.json`은 준비·권한 차단·완료 상태를 기록하며, 준비 기록은 실행 결과가 아니다.
두 정책 모두 ARP는 core1을 통한 트리로 전달하고, IPv4 데이터는 기존의 모든
ECMP 경로를 사용한다. 다중 코어에서 ARP flood가 순환하던 기존 규칙을 수정한
공통 조건이다. 회차별 `ovs_스위치.txt`에 OpenFlow 패킷 카운터도 보존한다.

## 데이터 경로

```text
server1 ─┐
         ├─ Hedges1 → Edges1 [HTB 10Mbps → flowprio] → core1 → Edgec1 → client들
server2 ─┘
                         ↑
             flow.id별 FIFO + 다음 패킷을 보낼 flow 선택
                         ↑
             사용자 공간 분류 프로세스가 0.5초 뒤 등급 갱신
```

모든 모드에서 기존 링크 대역폭을 유지한다. 그림은 코어 하나를 표시한 예시이며,
여러 경로에서는 `Edges1 → coreN` 출력 포트마다 큐를 설치한다.
서버·클라이언트 수, 양쪽 엣지 수, 코어 경로 수를 인자로 받으며 기본 OVS system
datapath를 지원한다. 큐는 모든 서버 엣지의 코어 방향 출력 포트에 설치한다.
큐는 실제 스위치 출력 인터페이스에 있으며 서버에서 전송 순서를 정하지 않는다.

## 플로우 추적

- `(client IP, flow.id)`가 논리적 식별자이며 실행별 로그로 namespace를 구분한다.
- 커널 내부에서는 연결과 TCP sequence 범위에 고유 `cookie`를 추가한다.
  우선순위 갱신은 `(ifindex, cookie)`를 사용해 오래된 결과의 오적용을 막는다.
- 서버 응답의 메타데이터에서 ID와 크기를 읽는다. 크기는 sequence 경계 추적에
  사용하고, 우선순위 결정에는 분류 결과만 사용한다.
- 분할된 16바이트 메타데이터와 out-of-order 조각을 누적하고, 재전송은 기존
  sequence 범위로 식별한다. 기존 연결의 다음 요청은 새로운 논리적 큐다.
- TCP sequence wrap을 처리한다. 현재 지원하는 개별 flow size는 2GiB 미만이며
  포함된 CDF들의 최대 크기 1GB는 이 범위에 들어간다.
- 하나의 Ethernet 프레임이 논리적 flow 경계를 걸치면 프레임을 쪼개지 않고 첫
  flow의 큐에 넣으며 `cross-boundary` 통계에 기록한다.
- 새 SYN-ACK부터 관측해야 한다. 이미 진행 중인 연결에 중간 부착한 경우에는
  경계를 추측하지 않고 fallback 큐로 보내며 통계에 기록한다.
- `flow.id=0`은 연결 종료 제어 요청으로 분류기에 제출하지 않는다.
- 다음 메타데이터로 이전 flow의 완료가 확인되면 지연 판정을 취소한다.
  큐가 비었다는 이유만으로 완료 처리하지 않는다. 아직 완료가 확인되지 않은
  빈 큐에는 판정이 전달될 수도 있지만 새 패킷이 없는 한 전송에는 영향이 없다.
- superseded 상태는 지연 재전송을 위해 최소 30초 유지한다. flow/connection
  테이블에도 상한이 있다. 이 첫 버전은 장시간 무제한 TCP 연결 churn용이 아니다.

## 분류와 우선순위

`MockClassifier`는 첫 application-data 패킷 관측 시각을 기준으로 비동기 대기한다.
메타데이터 echo는 관측 데이터에 포함하지 않는다. 각 flow의 대기는 독립적이다.

- 기본 0.5초 뒤 `size < 1_000_000`이면 `MICE`, 그 이상이면 `ELEPHANT`.
- 관측부터 0.5초가 지나기 전에는 종류를 전달하지 않는다. 프로세스 polling 주기
  20ms 및 CPU 스케줄링에 따라 실제 전달은 늦어질 수 있다.
- 커널은 대기 중에도 전송을 계속한다. 커널 패킷 처리 함수에서 sleep하지 않는다.
- 결과에는 flow의 식별자·등급·시각만 포함한다. 정확한 크기를 우선순위 갱신
  명령에 넣지 않는다.
- UNKNOWN과 MICE는 높은 등급에서 전송한다. 새 대형 flow도 분류 전까지는
  높은 등급을 받으며, ELEPHANT로 확인되면 기존 backlog를 포함해 낮은 등급으로 내린다.
- 높은 등급은 엄격 우선순위(strict priority)로 선택한다. 대기 중인 UNKNOWN/MICE
  패킷이 있으면 ELEPHANT 데이터 패킷을 선택하지 않는다. 높은 등급의 큐가
  비었을 때만 ELEPHANT를 전송하며, 활성 등급은 링크 전체를 사용한다.
  등급 간 대역폭 비율이나 일반 등급의 최소 전송 몫을 설정하지 않는다.
- 같은 등급의 flow들은 바이트 기준 DRR로 전송 기회를 나눈다.
- 대기 시간에 따른 승격은 없다. UNKNOWN/MICE 패킷이 계속 대기하면 ELEPHANT는 기다린다.
- flow 큐 자체를 유지하고 선택 등급을 바꿔 기존 backlog에도 결과를 적용한다.
  같은 flow의 도착 순서를 유지하므로 큐 변경으로 추가적인 순서 역전을 만들지 않는다.
- 선점은 패킷 경계에서 한다. 출력 경로에 이미 넘긴 패킷을 회수하지 않는다.
- 전체 4,096개, flow별 256개 packet 한도를 적용하고 초과 패킷은 tail drop한다.
  GSO 패킷은 거부하므로 실행기가 트래픽 시작 전에 GRO/GSO/TSO를 비활성화한다.
- 내부 watchdog pacing은 사용하지 않는다. 상위 HTB가 늦은 wakeup을
  token/burst credit으로 보상하며, `flowprio`는 backlog가 있으면 즉시 다음
  패킷을 반환한다.
- ARP/ACK 등 fallback/control 패킷은 데이터가 대기할 때 대략 다섯 번 선택마다
  한 번 기회를 받는다. control만 있으면 전체 링크를 사용한다.

`fair`는 모든 flow를 같은 등급으로 DRR 처리하고, `fifo`는 전역 도착 순서로
처리한다. 둘 다 지연 분류 로그를 남기지만 분류 결과를 선택 순서에 반영하지 않는다.

## 결과와 정리

`data/scheduler/<timestamp>_<mode>_<pid>/`에 다음을 남긴다.

- `manifest.json`: 설정, 인터페이스, 실행 인자.
- `classifications.jsonl`: 최초 관측, 지연 판정, stale 결과, 종료 이벤트.
- `final_stats.txt`: 포트 전송량, fallback 수, drops, 최대 서비스 대기시간,
  보존 중인 flow 상태. 만료된 flow의 세부 상태는 없지만 포트 누적 통계는 유지한다.
  기존 로그 파서와의 호환을 위해 예전 boost 수 열은 항상 0으로 남긴다.
- `flows_N.txt`, `log_N.txt`: TrafficGenerator FCT와 실행 로그 사본.
- `controller.log`: 분류 프로세스 오류.

```bash
python3 -m scheduler.analyze data/scheduler/<실행_디렉터리>
```

정상 종료/오류/인터럽트에서 분류 프로세스를 종료하고 설치한 출력 qdisc를 제거한다.
모듈은 다음 실험에서 재사용하도록 남겨둔다. 서버 포트를 바꾸려면 관련 qdisc가
없는 상태에서 `sudo rmmod sch_flowprio` 후 새 포트로 실행한다. 모듈 강제 제거는 하지 않는다.

제어 인터페이스 `/proc/net/flowprio`는 root 전용이며 다음 명령을 받는다.

```text
config IFINDEX RATE_BPS MODE QUANTUM LIMIT PER_FLOW_LIMIT
class IFINDEX COOKIE KIND
```

MODE는 FIFO=0, FAIR=1, PRIORITY=2, KIND는 MICE=1, ELEPHANT=2다.
config는 트래픽 시작 전에만 허용한다. v3에서 `RATE_BPS`는 기존 제어 메시지와
manifest 호환을 위해 남아 있으며 실제 속도 제한은 상위 HTB `5:1` 클래스가
담당한다. 읽기는 미분류 flow를, 읽기 전용
`/proc/net/flowprio_stats`는 보존 중인 전체 상태를 반환한다.
이전 정책의 모듈이 로드되어 있다면 실행기의 버전 확인을 거쳐 새 모듈을 적용한다.
기존 실험이 사용 중인 qdisc를 자동 제거하지 않으므로 먼저 그 실험을 종료해야 한다.

## 검증

```bash
python3 -m unittest discover -s scheduler/tests -v
```

Python 테스트에는 분류 프로세스의 실제 기동·0.5초 지연·제어 명령·종료 로그 검증이
포함된다. C 테스트는 실제 `sch_flowprio.c`를 사용자 공간 어댑터와 함께 컴파일해
AddressSanitizer/UBSan으로 MICE 엄격 우선순위·기존 UNKNOWN backlog의 ELEPHANT 강등·
새 MICE/UNKNOWN 도착 시 선점·서로 다른 패킷 크기·장시간 대기해도 일반 등급 유지·
UNKNOWN/MICE의 같은 등급 DRR·
FAIR·FIFO·work-conserving dequeue·drop 한도·
메타데이터 분할·재전송·연결 재사용·sequence wrap을 확인한다. 이는 실제 커널
락/타이머/OVS 경로 검증을 대신하지 않는다.
