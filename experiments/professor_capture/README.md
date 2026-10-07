# 원본 생성기 기반 TShark 캡처와 요청 단위 데이터셋

서버/클라이언트 2쌍의 TCP 트래픽을 만들고, 패킷을 캡처한 뒤 **요청별 정답**과 **초기 1~10개 응답 패킷 특징**을 자동 생성한다. 2026-10-02 시윤의 Ubuntu에서 성공한 실습을 팀원이 재현하도록 공유한 코드다. 2026-10-08 공유 준비에서 개인 경로와 교수님 원본 저장소의 런타임 의존을 제거했다.

이 표본의 단위는 **생성기 요청 1건**이다. 같은 TCP 연결을 여러 요청이 재사용하므로 TCP 연결 1개와 같지 않다. 모델 학습 코드는 포함하지 않는다.

## 1. Ubuntu에서 준비

아래 명령은 모두 **Ubuntu 셸**에서 입력한다. `mininet>`이나 Windows PowerShell에 입력하는 명령이 아니다. Ubuntu 24.04의 시스템 Python/Mininet을 기준으로 작성했다.

처음 받는 팀원은 접근 권한이 있는 본인 GitHub 계정으로 clone한다.

```bash
git clone --branch siyoon/traffic-experiments --single-branch https://github.com/yoojinsong-cs/CapstoneDesign1.git CapstoneDesign1-siyoon
cd CapstoneDesign1-siyoon
```

이후 명령은 **clone한 저장소 최상위 폴더**에서 실행한다.

```bash
sudo apt update
sudo apt install -y python3 git build-essential mininet openvswitch-switch openvswitch-testcontroller tshark ethtool iproute2 iputils-ping
sudo systemctl start openvswitch-switch
```

TShark 설치 중 일반 사용자 캡처 허용 여부를 물으면 이 실습에는 `No`로 충분하다. 실제 캡처는 sudo 실행 안에서 수행하고, 저장된 pcap 전처리는 일반 사용자로 수행한다. Python 전처리는 표준 라이브러리와 TShark를 사용하므로 별도 pip/Conda 설치가 없다. `/usr/bin/python3`을 사용한다.

## 2. 호환되는 생성기 준비

```bash
/usr/bin/python3 experiments/professor_capture/prepare_generator.py
```

sudo 없이 실행한다. 공개 [HKUST-SING/TrafficGenerator](https://github.com/HKUST-SING/TrafficGenerator)의 커밋 `8e7c51680890fb3d91b7bc3e9a56da8e987f1bf7`을 `experiments/professor_capture/.deps/TrafficGenerator/`에 clone하고 빌드한다. 기존 소스의 커밋/수정 여부를 검사하며 다른 버전을 자동으로 덮어쓰지 않는다.

**`scripts/TrafficGenerator`와 섞지 않는다.** 이 추출기는 `id, size, tos, rate` 네 정수의 16 B 메타데이터를 해석한다. 팀 수정 생성기는 `app` 필드가 있는 20 B 형식이므로 호환되지 않는다. 이 코드는 임의의 인터넷/팀 수정본 pcap을 해석하는 범용 추출기가 아니다.

## 3. 실행 전 검사

```bash
/usr/bin/python3 experiments/professor_capture/main.py --check
/usr/bin/python3 -m unittest discover -s experiments/professor_capture -p 'test_*.py' -v
```

첫 명령의 예상 출력은 `READY: dependencies and pinned generator verified; no network started.`다. 필수 실행 파일·Mininet import·생성기 버전/빌드를 검사하며 네트워크·캡처는 시작하지 않는다. OVS 서비스 연결이나 실제 통신까지 보장하는 검사는 아니다.

두 번째 명령은 재조립, 재전송/누락 검사, 요청 경계, 초기 구간만 사용되는지를 검증한다. sudo나 실행 중인 Mininet이 필요 없다.

## 4. 새 트래픽 생성과 캡처·전처리

같은 OS에서 다른 Mininet 실습을 동시에 실행하지 않은 상태로 시작한다. sudo 비밀번호는 터미널에 입력한다.

```bash
sudo /usr/bin/python3 experiments/professor_capture/main.py
```

흐름: 의존성 확인 → 토폴로지/규칙 구성 → 가상 인터페이스 offload 조정 → 두 쌍 ping → TShark 캡처 시작 → 요청 생성 → 캡처/네트워크 종료 → 일반 사용자 전처리.

매번 새 `experiments/professor_capture/results/professor-capture-.../` 폴더를 만들고 시작 시 `RESULT_DIR=...`, 성공 시 `COMPLETE=...`를 출력한다. 처리기 마지막 JSON의 `status`는 `complete`, 요청은 200건, 특징은 2,000행이어야 한다. 일부 잘못된 캡처나 로그 불일치가 있으면 정상 데이터로 넘기지 않고 실패한다.

기본 제한 시간은 트래픽 생성 1,200초이며 필요하면 `--timeout 1800`처럼 지정한다. Ctrl+C나 오류에도 이 실행에서 만든 네트워크와 자식 프로세스를 정리한다. 같은 이름의 OVS 브리지가 이미 있으면 중단한다. 전역 `mn -c`/`pkill tshark`는 실행하지 않는다.

## 5. 결과 확인과 기존 pcap 재처리

| 파일 | 의미 |
|---|---|
| `traffic.pcap` | TCP 5001 양방향 원본 캡처. 생성기 제어 내용도 포함 |
| `manifest.json` | 조건·버전·해시·요청 로그 경로·진행/완료 상태 |
| `client1-flows.txt`, `client2-flows.txt` | 요청별 생성기 정답 로그 |
| `dataset/packets.csv` | 전체 선택 패킷의 시각·주소/포트·TCP sequence·길이. 원시 payload 제외 |
| `dataset/requests.csv` | 요청별 최종 본문 크기, 임시 라벨, 추적 정보 |
| `dataset/request-packets.csv` | 요청 본문과 실제 캡처 프레임의 대응 |
| `dataset/features.csv` | 요청별 N=1..10 초기 특징. `sample_id`로 정답 표와 연결 |
| `dataset/feature-columns.json` | 모델 입력으로 허용한 열 |
| `dataset/summary.json`, `dataset/DATASET.md` | 자동 집계, 데이터 정의와 한계 |
| `source/`, `offloads.txt`, `network-before/after.txt`, `capture.log` | 실행 소스·환경·관측 로그 |

재처리에는 **pcap 하나만이 아니라 manifest와 두 client의 flow 로그가 들어 있는 실행 폴더**가 필요하다. 터미널을 다시 열었다면 아래 변수를 다시 설정한다. 예시 경로의 `...`는 실제 출력된 폴더 이름으로 바꾼다.

```bash
run_dir="$(pwd)/experiments/professor_capture/results/professor-capture-..."
/usr/bin/python3 experiments/professor_capture/build_dataset.py "$run_dir" --out "$run_dir/dataset-recheck"
```

sudo 없이 실행한다. 출력 폴더가 이미 있으면 덮어쓰지 않으므로 새 이름을 사용한다. 과거 시윤의 실행 폴더 형식도 지원한다. 구형 한 쌍 10요청 학습 캡처에만 쓰는 `--legacy`는 일반 재현 절차에 필요 없다.

## 6. 정답·특징의 정의

- `final_bytes`: 서버 응답의 본문 크기. 헤더, 생성기 메타데이터, 종료용 응답, 중복 재전송을 제외한다. TCP 재조립과 요청별 로그를 대조한다.
- 임시 `label`: `final_bytes >= 1,000,000`이면 `elephant`, 아니면 `mice`. 교수님/팀 확정 기준은 아니다. 재처리 때 `--threshold-bytes`로 변경할 수 있으며 조건을 명시해야 한다.
- 초기 N: **서버→클라이언트 방향에서 새 응답 본문 바이트를 포함한 캡처 기록** N개. SYN/ACK-only·제어-only·완전 중복 재전송을 제외한다.
- `body_bytes_i`: i번째 기록의 새 본문 바이트 수. `ip_len_i`: 해당 기록의 IP 전체 길이. `iat_ms_i`: 선택한 기록 사이 도착 간격(ms), 첫 값은 0.
- `mask_i`와 `observed_count`: N보다 짧은 응답을 0 패딩한 위치와 실제 관측 수. `prefix_bytes`, `prefix_elapsed_ms`는 해당 초기 구간의 본문 합계와 경과 시간이다.
- `features.csv`의 `sample_id`는 정답 연결용이다. 학습 입력은 `feature-columns.json`에 있는 열만 사용한다. 최종 크기/FCT, 주소·포트·ID, raw payload를 넣지 않는다. 정답 자체가 들어 있는 생성기 제어 내용도 제외한다.
- 짧은 응답을 패딩한 표는 오프라인 전처리 결과다. 실제 조기 판정에서는 응답 완료/대기 한도를 언제 알 수 있는지 별도 정의해야 한다. 최종 길이를 미리 알고 예측 시점을 결정하지 않는다.

## 7. 고정된 실험 조건과 원본 대비 변경

- 교수님 [Mininet-traffic-generator](https://github.com/yeonhooy/Mininet-traffic-generator) 기준 커밋: `076079bbfc853c4eddd086acacb767e5691a49bf`. 이 공유본에 필요한 Python 모듈을 포함했으므로 별도 교수님 저장소 clone은 런타임에 필요 없다. `manifest.professor_commit`은 기준 출처를 뜻한다.
- 원본 인자 `2 2 1 1 2 5001 100`: 4호스트, 6스위치, 10링크, OVSController. client1↔server1 및 client2↔server2, 각 100요청.
- 코어 인접 링크 10 Mbps, 나머지 1000 Mbps. 각 client의 목표 RX 900 Mbps, DCTCP CDF, seed 1. 목표 RX는 실제 달성 속도가 아니다. DCTCP CDF 선택은 TCP 혼잡 제어 DCTCP 활성화와 별개다.
- 원본 IP 지정의 prefix 생략을 유지한다. 과거 실행에서 `/8`을 확인했으며 실제 값은 `network-before.txt`로 확인한다.
- OpenFlow13과 고유 DPID, core1 트리의 ARP, IPv4 select group을 사용한다. 원본의 기본 전달 의도와 장치/부하를 유지하되 규칙 호환성·ARP 순환을 보완했다.
- `Hedges1-eth3`에서 두 쌍의 TCP를 TShark로 합쳐 캡처한다. 실험 가상 인터페이스에만 TSO/GSO/GRO를 끈다. 원본과 동일한 offload 조건의 성능 재현으로 보고하지 않는다.
- 원본 C 프로그램의 짧은 경로 제약 때문에 client의 작업 폴더를 결과 폴더로 두고 짧은 config/CDF/log 이름을 전달한다.
- 공유본은 결과를 이 폴더의 `results/`에 저장하고 시스템 `ovs-testcontroller`를 사용한다. 매 실행 소스와 생성기 바이너리/pcap 해시를 보관한다. 개인 폴더의 원본 Git 조회/자동 diff 생성은 실행 의존에서 제거했다.

`main.py`/`flowGenerator_parll.py`의 조건을 고정해 첫 재현에 집중한다. CDF·seed·요청 수·토폴로지 변경과 20 B 팀 생성기 지원은 별도 실험으로 구현·검증해야 한다.

## 8. 예시와 검증 범위

[examples/README.md](examples/README.md)에 과거 성공 실행에서 가져온 작은 요청/특징 예시가 있다. 전체 pcap/패킷 CSV·빌드 결과는 Git에 넣지 않는다.

2026-10-02 원본 기반 실행: 200요청, TCP 연결 88개, 패킷 294,506개, 본문 278,885,260 B, 특징 2,000행, 임시 Elephant 48/Mice 152. 로그와 요청 크기를 대조했다. 캡처 드롭 수는 당시 로그에 미보고였다. 이 수치는 과거 실행 결과이며 공유본을 다른 머신에서 실행해 얻은 결과가 아니다.

2026-10-08 공유본 검증:

- 지정 원본 생성기를 별도 `.deps/`에 새로 clone·빌드하고 커밋을 확인했다.
- 전처리 단위 테스트 5개가 통과했다.
- 과거 TShark 10요청 pcap을 공백이 있는 임시 경로에서 재처리했다. 10정답/100특징행, 본문 9,775,575 B를 확인했고, run/sample ID의 경로 차이를 제외한 CSV 4종과 입력 열 목록이 기존 검증 결과와 일치했다. TShark 분석 stderr는 비어 있었다.
- `--check`의 의존성 누락 오류와 성공 경로를 확인했다. 검증 머신에는 컨트롤러가 시스템 설치되어 있지 않아 과거에 추출한 컨트롤러를 해당 검사 프로세스의 PATH에만 지정했다. 팀원 절차는 위 Ubuntu 패키지 설치를 따른다.
- 이번 공유 준비에서 Mininet 네트워크와 200요청 캡처를 다시 실행하지 않았다. 다른 머신의 전체 실행은 아직 미검증이다.

동일 seed라도 OS 스케줄링/연결 배정/관측 타이밍 때문에 TCP 연결 수·패킷 수·간격·FCT가 달라질 수 있다. 재현 성공은 요청 완료, 로그-본문 크기 일치, 특징 정의/행 수, 완료 상태로 확인한다. 과거 CSV와 바이트 단위 일치를 요구하지 않는다.

두 client가 같은 seed를 사용해 크기열이 반복된다. 2,000행은 200요청의 10가지 N이며 독립 표본 2,000개가 아니다. 독립 실행·조건을 추가하기 전 이 표 하나를 무작위로 나눠 모델의 일반화 성능을 주장하지 않는다. 완성된 flow 크기를 이용한 패딩/마스크와 실제 온라인 관측 가능성도 별도로 검증해야 한다.

## 9. 자주 만나는 문제

- `Missing executable: ovs-testcontroller`: Ubuntu 패키지 `openvswitch-testcontroller` 설치 여부를 확인한다.
- Mininet import 실패: Conda의 python 대신 `/usr/bin/python3`으로 실행하고 `mininet` 패키지를 확인한다.
- `Existing Mininet bridges`: 앞선 실습을 정상 종료한 뒤 재실행한다. 다른 작업의 네트워크를 강제로 정리하지 않는다.
- `Generator commit differs` 또는 Git diff 오류: 이 실습의 `.deps/TrafficGenerator`가 지정 원본인지, 수정 사항이 있는지 확인한다. 기존 소스를 자동 reset하지 않는다.
- `Pcap and flow log disagree`, `Incomplete response`, `TCP capture gap`: 생성기 버전/로그 짝, 캡처 범위·중단·누락을 확인한다. 실패한 표본을 임의로 Mice로 채우지 않는다.
- `FileExistsError`로 재처리 실패: `--out`에 아직 없는 출력 폴더를 지정한다.

공식 도구 설명: [TShark](https://www.wireshark.org/docs/man-pages/tshark.html), [Mininet](https://mininet.org/overview/). 실험 정의·버전은 이 README와 실행 `manifest.json`을 함께 보관한다.
