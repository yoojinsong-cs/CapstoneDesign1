# CapstoneDesign1

네트워크 트래픽 생성·캡처와 flow 크기 예측 실험을 공유하는 팀 저장소다.

| 실험 | 시작 문서 |
|---|---|
| 팀의 트래픽 생성·분류 실험 | [scripts/README.md](scripts/README.md) |
| 시윤의 원본 생성기 기반 캡처·요청별 정답·초기 특징 추출 재현 | [experiments/professor_capture/README.md](experiments/professor_capture/README.md) |

두 실험은 생성기 프로토콜과 전처리 방식이 다르다. 시윤의 재현 실험은 별도로 내려받는 **16 B 원본 생성기**를 사용하며, `scripts/TrafficGenerator`의 **20 B 수정 생성기**와 서로 바꿔 사용하지 않는다.

시윤의 실습을 재현하려면 위 두 번째 README의 설치 → 생성기 준비 → 검사 → 실행 순서를 따른다. Mininet 실행에는 Ubuntu와 sudo 권한이 필요하다. 결과 pcap/CSV와 내려받은 생성기는 해당 실습 폴더의 Git 제외 경로에 저장된다.
