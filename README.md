# Matchuri 성능 테스트

PC A에서 k6·Prometheus·Grafana를 실행하고, PC B에서 Nginx 게이트웨이·Java 21/Spring Boot·MySQL·MySQL exporter를 실행합니다. A와 B의 물리 호스트 메트릭도 A의 Prometheus에서 수집합니다. Spring Boot와 MySQL은 B의 전용 Compose 네트워크와 데이터 볼륨을 사용합니다. k6는 B의 사설 LAN 주소를 사용하고 결과를 A의 Compose 네트워크에서 Prometheus에 전송합니다.

[k6 카탈로그](k6/catalog.json)는 업무 GET 33개, 조회 성격의 POST 2개, SSE GET 2개, health와 OAuth 진입점까지 39개 API와 91개 케이스를 추적합니다. 경로·API ID는 백엔드 OpenAPI 레지스트리에서 검증합니다. 관리자 JSON과 호출 가능한 deprecated v1 조회도 측정합니다. OAuth는 Smoke에서 로컬 리다이렉트를 확인합니다. 프리셋과 페이지 없는 관리자 목록은 데이터셋을 바꿔 응답 행 수를 비교합니다.

## 1. 두 PC 준비

- 양쪽 PC에서 `app/perf`와 `app/backend`를 같은 상위 디렉터리에 둡니다. 백엔드는 양쪽 모두 동일한 커밋으로 체크아웃하고 작업 트리를 깨끗하게 준비합니다. 각 명령은 `app/perf`에서 실행합니다.
- Python 3.11 이상, Git, 로컬 Docker Engine 또는 Docker Desktop과 Compose v2를 준비합니다. Compose가 BuildKit의 `additional_contexts`를 지원해야 합니다. 첫 빌드는 레지스트리와 Gradle·k6 빌드 의존성에 접근합니다.
- 두 PC의 시계를 동기화하고 안정적인 사설 LAN을 사용합니다. B의 방화벽에서 A의 IP에만 API `18080`, Actuator `18081`, MySQL exporter `19104`, 물리 호스트 exporter 포트를 허용합니다. Linux Docker에서는 Docker가 게시한 포트에도 제한이 적용되는지 확인합니다. A에서 접근 가능하고 다른 LAN 주소에서는 차단되는지 확인한 후 Smoke를 실행합니다. [Docker 포트와 방화벽](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- Windows에서 `windows_exporter`를 호스트에 설치해 CPU, memory, logical_disk, net을 수집합니다. Linux에서는 호스트에서 `node_exporter`를 구동합니다. A의 exporter는 A의 Prometheus 컨테이너가 접근할 수 있는 호스트 주소에 바인딩합니다. B의 exporter는 B 사설 LAN IP에 바인딩합니다. Linux에서 별도 마운트에 Docker 데이터를 저장한다면, 디스크 감시 쿼리의 마운트 목록이 실제 Docker 데이터 디스크를 포함하는지 확인합니다. [Windows exporter](https://github.com/prometheus-community/windows_exporter) · [Linux Node Exporter](https://prometheus.io/docs/guides/node-exporter/)
- 최초 실측에 앞서 각 PC의 CPU·RAM·OS, Docker/WSL VM 한도, exporter 버전, 디스크, 네트워크 프로필을 기록합니다. Windows 호스트 지표와 Docker Desktop Linux VM 자원은 구분합니다. 비교할 실행 동안 같은 리소스 설정을 유지하고, 다른 대규모 작업을 중단합니다.

## 2. PC B에서 대상 실행 및 시드 적재

`perf-b.env.example`을 `perf-b.env`로 복사하고 `PERF_BIND_IP`를 B의 LAN 주소로 바꿉니다. PC A 설정에도 같은 주소와 포트를 사용합니다.

```text
python -m pip install -r fixtures/requirements.txt
python perf.py up --role b
```

`up`은 전용 프로젝트 `matchuri-perf-b`를 시작하고 로컬 시크릿 파일을 준비합니다. B의 관리 포트에서 `/api/v1/health`가 `UP`으로 응답하고 Spring Boot 기준 시드 적재가 끝난 뒤 다음 명령을 실행합니다.

```text
python perf.py seed --dataset mvp-v1
```

| 데이터셋 | 용도 |
|---|---|
| `smoke-v1` | 초기 기동·계약 확인에 쓸 소규모 데이터. 깊은 페이지 확인용 300건 코호트를 유지합니다. |
| `mvp-v1` | Baseline용 데이터. 기존 참조 시드와 합성 회원 5,000명, 그룹 500개, 개인 이력 30,000건, 그룹 이력 10,000건, 초대 5,000건 규모로 구성합니다. |
| `mvp-v1-expanded` | 회원·이력 규모를 유지하면서 비활성 메뉴·카테고리·재료와 활성 프리셋을 32개씩 추가합니다. 관리자 목록과 프리셋 조회에서 응답 행 수의 영향을 확인할 수 있습니다. |

시드는 전용 DB 표식과 볼륨 소유권, 앱 프로파일·이미지 SHA를 확인합니다. 합성 상태와 관계를 고정하고 깊은 페이지에 해당 필터를 만족하는 300건 이상을 배치합니다. 활성 추천·초대는 적재 후 약 23시간 55분 동안 유효합니다. A는 예정 실행이 유효 시간에 들어가는지 시작 전에 확인합니다.

적재가 끝나면 B에서 아래 두 파일을 준비하고 3절에서 A로 안전하게 복사합니다.

- `runtime/b/fixtures.json`
- `runtime/b/secrets/account-password`

이 파일에는 합성 계정과 초대 링크에 사용하는 정보가 있으므로 복사 대상은 A로 제한합니다. `runtime/`, 로컬 설정 파일 및 `results/`는 Git에서 제외됩니다. 동등한 비교를 위해 스크립트와 설정 방식을 동일하게 유지합니다.

## 3. PC A에서 관측과 사전 점검

`perf-a.env.example`을 `perf-a.env`로 복사합니다. B의 주소와 포트, 양쪽 호스트 exporter 주소를 설정합니다. A의 `host.docker.internal`을 사용할 때는 exporter가 컨테이너 쪽에서 접근 가능한 인터페이스를 사용하도록 설정합니다. 장비·리소스·네트워크 조건을 나타내는 환경 설정값도 채웁니다. `python perf.py prepare --role a`로 목적지를 만든 뒤 2절의 두 파일을 `runtime/a/fixtures/`에 같은 파일명으로 복사합니다.

```text
python perf.py up --role a
python perf.py audit
python perf.py preflight --dataset mvp-v1
```

Grafana는 A에서 `http://127.0.0.1:13000`으로 접속합니다. 첫 기동 때 기본 사용자 `admin`과 `runtime/a/grafana-password`의 값을 사용합니다. Prometheus는 `http://127.0.0.1:19090`의 Targets 화면에서 앱, MySQL, 호스트 A·B, Prometheus가 모두 `UP`인지 확인합니다. 사전 점검은 대상 환경 표식·백엔드 SHA·데이터셋, 상태 응답, 필수 메트릭, k6의 Remote Write 수신 설정을 확인합니다.

## 4. Smoke에서 Baseline까지

```text
python perf.py plan --suite smoke --api all
python perf.py run --suite smoke --api all --dataset mvp-v1
python perf.py run --suite load --api OPS.020.000 --case heavy --rates 2,5,10 --repeat 1
```

전체 Smoke는 HTTP 상태와 응답 계약, 기대 건수·상태를 확인합니다. 로그인은 측정 전에 합성 계정으로 실행합니다. 일반 HTTP는 일정한 도착률로 iteration당 요청 1개를 보내고, 기본 Smoke는 1 RPS로 15초간 측정합니다. SSE는 기본 4연결/분으로 도착을 시작하고 각 연결을 최소 70초 유지해 초기 이벤트·heartbeat를 확인합니다. 첫 설치에서는 전체 Smoke 전 `--api REF.010.000 --case all`로 HTTP 결과 저장을, `--api RT.010.000 --case member`로 SSE 연결과 종료를 확인할 수 있습니다.

짧은 저부하 실행에서 앱·DB·양쪽 호스트 스크레이프가 함께 들어오는지 확인합니다. 탐색에서 유지 가능한 공통 HTTP 요청률을 선정합니다. 다음 예에서는 2 RPS를 사용합니다.

```text
python perf.py plan --suite load --api all --rates 2 --sse-rates 6 --repeat 3
python perf.py run --suite load --api all --rates 2 --sse-rates 6 --repeat 3 --dataset mvp-v1
python perf.py run --suite load --api REC.020.500 --case deep-page --rates 2 --repeat 3
```

기본 HTTP Load는 30초 워밍업과 180초 측정, 3회 반복입니다. SSE Load는 기본 6연결/분과 180초 도착 구간을 사용하며 연결 종료까지 기다립니다. 전체 Load 계획은 210단계이며 시작·종료와 수집 여유를 포함한 사전 점검 구간은 약 15시간입니다. `plan`으로 소요 시간을 먼저 확인하고, 작업 시간이나 데이터 유효 시간에 맞춰 API별로 실행할 수 있습니다. 확장 데이터셋에서는 관리자 목록과 프리셋의 `baseline` 케이스를 재실행해 데이터 규모 효과를 기록합니다.

Stress는 초기 Load를 보고 선택한 API에 적용합니다. 가장 낮은 값부터 명시한 상한까지 독립된 고정 시간 단계로 진행합니다. 예를 들어 탐색 결과 5 RPS를 안정적으로 처리한 경우 다음처럼 시작할 수 있습니다.

```text
python perf.py run --suite stress --api OPS.020.000 --case heavy --rates 5,10,15 --repeat 1
```

최초 만료 전이는 해당 행의 상태를 갱신하는 단발 검증입니다. 전체 Smoke와 Load에서 빠지며, 별도로 조회 한 건만 실행합니다.

```text
python perf.py run --suite smoke --api REC.050.000 --case first-expiration
python perf.py run --suite smoke --api GREC.050.000 --case first-expiration
```

실행 후 B에서 기존 시드 명령을 다시 호출하면 두 행의 상태를 확인해 만료 검증 가능 목록을 갱신합니다. 같은 전이의 다음 측정은 새 프로젝트에서 시작합니다.

### 데이터세트 교체 또는 새 반복 시작

활성 데이터가 오래됐거나 데이터셋·대상 SHA를 바꿀 때는 B에서 현재 프로젝트를 중지하고 새 접미사를 사용합니다.

```text
python perf.py stop --role b
python perf.py up --role b --project-suffix expanded-01
python perf.py seed --project-suffix expanded-01 --dataset mvp-v1-expanded
```

새 프로젝트의 `runtime/b-expanded-01/fixtures.json`과 `runtime/b-expanded-01/secrets/account-password`를 A의 기존 목적지에 복사하고 `preflight --dataset mvp-v1-expanded`를 실행합니다. 다음에 중지할 때는 현재 프로젝트 접미사를 `stop --role b --project-suffix expanded-01`에 전달합니다. 이전 프로젝트의 MySQL 볼륨은 보존됩니다.

### 실행 중단 조건

실행기는 대상 식별자, health, 수집 상태, 호스트 여유 메모리·디스크, 앱·DB 재시작, 최근 30초의 5xx·타임아웃 비율을 감시합니다. 기본 보호 기준은 health 또는 필수 수집 3회 연속 실패, 디스크 여유 10% 미만, 가용 메모리 5% 미만, 최근 30초 완료 20건 이상에서 5xx·타임아웃 비율 10% 이상입니다. 보호 기준에 도달하면 현재 실행을 중단하고 부분 결과를 저장합니다. Load에서 요청률만 미달하면 같은 케이스의 더 높은 단계는 건너뛰고 다른 케이스는 계속 측정합니다. 이 실행은 `partial`로 보존됩니다. 초기 Baseline에는 처리량·응답시간 합격 기준을 두지 않습니다.

취소는 A의 실행 콘솔에서 `Ctrl+C`를 사용합니다. 정상 종료 후 A 또는 B 서비스를 중지하려면 아래 명령을 실행합니다.

```text
python perf.py stop --role a
python perf.py stop --role b
```

A가 강제 종료되어 `runtime/a/run.lock`이 남으면, 잠금 파일에 적힌 실행 PID와 Docker의 해당 k6 컨테이너 종료를 확인한 뒤 그 잠금 파일만 제거합니다.

## 5. 실측 결과 조회와 비교

```text
python perf.py list
python perf.py show <run-id>
python perf.py compare <baseline-run-id> <candidate-run-id>
```

각 실행은 PC A의 `results/<run-id>/`에 저장합니다.

| 파일 | 확인할 정보 |
|---|---|
| `manifest.json` | UTC 시각과 Run ID, 백엔드·성능 저장소 Git 정보, 이미지·데이터셋 식별자, 환경 프로필, 계획, 단계별 상태·중단 사유 |
| `<stage-id>/summary.json` | k6 요약. 목표와 실제 시작·완료·성공률, 지연 p50/p95/p99, 요청 실패율, 드롭, SSE 최초 이벤트·heartbeat·종료 |
| `<stage-id>/stage.json` | 단계 식별자, 측정 시간 구간, 실행·요청률 달성·수집 상태, 중단 사유 |
| `<stage-id>/metrics.json` | 측정 구간의 앱·DB·호스트와 종료 대기까지의 k6 시계열. 관측되지 않은 항목은 비어 있거나 null로 저장됩니다. |

Grafana의 **Matchuri · k6**에서 단계 ID를 선택하고, **Matchuri · application and database**에서 동일한 UTC 구간을 적용합니다. 단계별 백분위수는 k6 요약, 시간대별 추세는 Grafana 히스토그램을 봅니다. 앱·DB 해석에는 A의 CPU·메모리·네트워크와 k6 VU·드롭을 함께 확인해 발생기 포화와 B 지연을 구분합니다. Prometheus 보존 정책은 최대 15일 또는 2GB입니다. 장기 비교가 필요하면 결과 폴더를 별도 보존합니다.

호스트 지표와 JVM·DB 지표로 기본 Baseline을 수집합니다. Docker Desktop에서 VM 또는 컨테이너별 CPU·메모리 확인이 필요하면 저부하 예행 측정에서 각 PC의 `docker stats --no-stream`을 별도로 기록합니다. Baseline을 시작하기 전에 스크레이프 주기와 관측 방식의 비용을 확인하고 이후 비교에서 같은 조건을 유지합니다.

후속 코드 비교에는 같은 데이터셋·케이스·부하·환경을 사용합니다. `compare`는 유효 완료 단계의 반복값, 중앙값·범위를 비교하며 중단, 관측 누락, 실제 요청률 차이가 있는 결과는 별도로 확인합니다. 우선 탐색 후보는 홈, 메뉴 복합 검색, 개인·그룹 v2 이력, 비회원 추천입니다. 이 목록은 코드 경로를 바탕으로 선정했습니다. 실제 후보는 측정 데이터로 확정합니다.

## 대상 PC 없이 확인 가능한 항목

```text
python perf.py audit
python perf.py plan --suite smoke --api all
python perf.py plan --suite load --api all --rates 2 --sse-rates 6 --repeat 3
python -m unittest discover -s fixtures
python -m unittest discover -s tools
```

물리 PC에서 처음 실행할 때는 이미지 빌드, MySQL 적재, A/B 연결과 방화벽, SSE heartbeat, Grafana 패널과 저장 번들까지 확인합니다. 실측 Baseline은 이 검증 후 생성합니다.
