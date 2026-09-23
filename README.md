# Matchuri Load Test

Matchuri 백엔드의 로컬 성능 및 부하 테스트 환경입니다.

## Stack

- k6 2.3.0
- Prometheus 3.14.0
- Grafana 13.2.2

## Architecture

```text
k6
 ├─ HTTP Request ──────────────> Spring Boot
 │
 └─ Prometheus Remote Write ──> Prometheus
                                  ▲
                                  │
                         /actuator/prometheus
                                  │
                            Spring Boot

Prometheus ──> Grafana
```

## Prerequisites

Spring Boot 애플리케이션이 로컬에서 실행되어 있어야 합니다.

```text
http://localhost:8080
```

Prometheus Metric Endpoint:

```text
http://localhost:8080/actuator/prometheus
```

## Start Monitoring

```bash
docker compose up -d
```

### Grafana

```text
http://localhost:3000
```

### Prometheus

```text
http://localhost:9090
```

## Run k6

### Smoke Test

```bash
docker compose run --rm k6 \
  run \
  -o experimental-prometheus-rw \
  scenarios/smoke.js
```

### Load Test

```bash
docker compose run --rm k6 \
  run \
  -o experimental-prometheus-rw \
  scenarios/load.js
```

## Test ID

각 테스트 실행을 구분해야 하는 경우 `testid` 태그를 사용합니다.

```bash
docker compose run --rm k6 \
  run \
  -o experimental-prometheus-rw \
  --tag testid=baseline-001 \
  scenarios/load.js
```

예시:

```text
baseline-001
baseline-002
query-optimized
cache-enabled
connection-pool-30
```

## Stop

컨테이너를 종료합니다.

```bash
docker compose down
```

Prometheus와 Grafana의 로컬 데이터까지 함께 삭제하려면:

```bash
docker compose down -v
```