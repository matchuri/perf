"""Run one measured case at a time and retain sanitized artifacts."""

from datetime import UTC, datetime
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .common import (
    JOB_NAMES, RESULTS, ROOT, PerfError, code_fingerprint, git_state, local_docker, query, read_json, write_json,
)
from .setup import is_healthy, preflight, target_identity, up_jobs

LIMITS = {
    "disk_free_ratio": 0.10,
    "memory_available_ratio": 0.05,
    "server_error_ratio": 0.10,
    "server_error_min_requests": 20,
    "health_failures": 3,
    "scrape_failures": 3,
    "poll_seconds": 5,
    "min_arrival_fraction": 0.98,
}
DISK_RATIO = (
    'node_filesystem_avail_bytes{job=~"host-a|host-b",fstype=~"ext[234]|xfs|btrfs|zfs",mountpoint=~"/|/var|/var/lib/docker"}'
    ' / node_filesystem_size_bytes'
    ' or windows_logical_disk_free_bytes{job=~"host-a|host-b",volume=~"[A-Z]:"} / windows_logical_disk_size_bytes'
)
MEMORY_RATIO = (
    'node_memory_MemAvailable_bytes{job=~"host-a|host-b"} / node_memory_MemTotal_bytes'
    ' or windows_memory_available_bytes{job=~"host-a|host-b"} / windows_memory_physical_total_bytes'
    ' or windows_os_physical_memory_free_bytes{job=~"host-a|host-b"} / windows_cs_physical_memory_bytes'
)
PROCESS_START = 'process_start_time_seconds{job="matchuri-backend"}'
MYSQL_UPTIME = 'mysql_global_status_uptime{job="mysql"}'
MYSQL_START = f'timestamp({MYSQL_UPTIME}) - {MYSQL_UPTIME}'
SERIES = {
    "app_cpu": 'process_cpu_usage{job="matchuri-backend"}',
    "app_rss": 'process_resident_memory_bytes{job="matchuri-backend"}',
    "app_start_time": PROCESS_START,
    "jvm_heap": 'sum(jvm_memory_used_bytes{area="heap",job="matchuri-backend"})',
    "jvm_threads": 'jvm_threads_live_threads{job="matchuri-backend"}',
    "jvm_gc_pause_seconds_per_second": 'sum(rate(jvm_gc_pause_seconds_sum{job="matchuri-backend"}[30s]))',
    "jvm_gc_pauses_per_second": 'sum(rate(jvm_gc_pause_seconds_count{job="matchuri-backend"}[30s]))',
    "tomcat_threads": '{__name__=~"tomcat_threads_(busy|current|config_max)_threads",job="matchuri-backend"}',
    "hikari_connections": '{__name__=~"hikaricp_connections_(active|idle|pending|max)",job="matchuri-backend"}',
    "hikari_timeouts_per_second": 'sum(rate(hikaricp_connections_timeout_total{job="matchuri-backend"}[30s]))',
    "app_http_rps": 'sum by(method,status) (rate(http_server_requests_seconds_count{job="matchuri-backend",uri="${route}"}[30s]))',
    "app_http_p95": 'histogram_quantile(0.95, sum by(le) (rate(http_server_requests_seconds_bucket{job="matchuri-backend",uri="${route}"}[30s])))',
    "mysql_up": 'mysql_up{job="mysql"}',
    "mysql_threads": '{__name__=~"mysql_global_status_threads_(connected|running)",job="mysql"}',
    "mysql_queries_per_second": 'rate(mysql_global_status_queries{job="mysql"}[1m])',
    **{f"mysql_innodb_{metric}_per_second": f'rate(mysql_global_status_innodb_{metric}{{job="mysql"}}[1m])'
       for metric in ("buffer_pool_read_requests", "buffer_pool_reads", "row_lock_waits", "row_lock_time",
                      "data_reads", "data_writes")},
    "host_cpu_busy": (
        '1 - avg by(job) (rate(node_cpu_seconds_total{mode="idle",job=~"host-a|host-b"}[1m]))'
        ' or 1 - avg by(job) (rate(windows_cpu_time_seconds_total{mode="idle",job=~"host-a|host-b"}[1m]))'
        ' or 1 - avg by(job) (rate(windows_cpu_time_total{mode="idle",job=~"host-a|host-b"}[1m]))'
    ),
    "host_memory_free_ratio": MEMORY_RATIO,
    "host_disk_free_ratio": f"min by(job) ({DISK_RATIO})",
    "host_network_bytes_per_second": (
        'sum by(job) (rate(node_network_receive_bytes_total{job=~"host-a|host-b",device!="lo"}[1m]))'
        ' or sum by(job) (rate(windows_net_bytes_received_total{job=~"host-a|host-b"}[1m]))'
    ),
    "scrape_health": 'up{job=~"matchuri-backend|mysql|host-a|host-b|prometheus"}',
    "scrape_duration_seconds": 'scrape_duration_seconds{job=~"matchuri-backend|mysql|host-a|host-b|prometheus"}',
}
K6_SERIES = {
    "k6_started_count": 'sum(k6_perf_started_total{${tags}})',
    "k6_started_rps": 'sum(rate(k6_perf_started_total{${tags}}[30s]))',
    "k6_completed_rps": 'sum(rate(k6_perf_completed_total{${tags}}[30s]))',
    "k6_failed_ratio": 'sum(rate(k6_perf_failed_total{${tags}}[30s])) / sum(rate(k6_perf_completed_total{${tags}}[30s]))',
    "k6_dropped_rps": 'sum(rate(k6_dropped_iterations_total{${run},scenario="measurement"}[30s]))',
    "k6_http_p95": 'histogram_quantile(0.95, sum(rate(k6_perf_http_duration_seconds{${tags}}[30s])))',
    "k6_sse_first_event_p95": 'histogram_quantile(0.95, sum(rate(k6_perf_sse_first_event_seconds{${tags}}[30s])))',
    "k6_sse_connections": 'sum(k6_perf_sse_connected_total{${tags}}) - sum(k6_perf_sse_closed_total{${tags}})',
    "k6_vus": 'max(k6_vus{${run}})',
    "k6_vus_max": 'max(k6_vus_max{${run}})',
    "k6_client_wait_p95": 'histogram_quantile(0.95, sum(rate(k6_http_req_blocked_seconds{${tags}}[30s])))',
}


def stage_plan(operations, args):
    plan = []
    for operation in operations:
        if args.api != "all" and args.api != operation["id"]:
            continue
        for case in operation["cases"]:
            if args.case and args.case != case["id"]:
                continue
            if args.suite not in case.get("suites", ["smoke", "load", "stress"]):
                continue
            if case.get("oneShot"):
                if args.case != case["id"]:
                    continue
                if (args.repeat != 1 or args.rates or args.sse_rates or args.duration or
                        args.warmup not in (None, 0) or args.vus not in (None, 1)):
                    raise PerfError("최초 만료 케이스는 기본 1회 요청으로만 실행할 수 있습니다.")
                plan.append({"operation_id": operation["id"], "path": operation["path"], "kind": operation["kind"],
                             "case_id": case["id"], "fixture": case["fixture"], "rate": 1,
                             "rate_unit": "1m", "duration": 15, "warmup": 0,
                             "repeat": 1, "vus": 1, "hold_seconds": 0, "one_shot": True})
                continue
            sse = operation["kind"] == "sse"
            smoke = args.suite == "smoke"
            rates = args.sse_rates if sse else args.rates
            rates = rates or ([4 if smoke else 6] if sse else [1 if smoke else 2])
            duration = args.duration or (20 if sse and smoke else 15 if smoke else 180)
            warmup = args.warmup if args.warmup is not None else (0 if smoke or sse else 30)
            # SSE concurrency is approximately arrival rate times connection hold.
            for rate in rates:
                vus = args.vus or (max(3, (rate * 75 + 59) // 60 + 3) if sse else max(4, rate * 2))
                if vus > 200:
                    raise PerfError("단계별 VU 상한은 200입니다.")
                for repeat in range(1, args.repeat + 1):
                    plan.append({
                        "operation_id": operation["id"], "path": operation["path"], "kind": operation["kind"],
                        "case_id": case["id"], "fixture": case.get("fixture"), "rate": rate,
                        "rate_unit": "1m" if sse else "1s", "duration": duration, "warmup": warmup,
                        "repeat": repeat, "vus": vus, "hold_seconds": 70 if sse else 0,
                    })
    if not plan:
        raise PerfError("선택한 API·케이스에 해당하는 실행 계획이 없습니다.")
    if args.case and args.api == "all":
        raise PerfError("--case는 --api ID와 함께 지정하세요.")
    if args.suite == "stress":
        if args.api == "all" or not (args.rates or args.sse_rates):
            raise PerfError("Stress는 선정된 단일 API와 명시한 단계별 부하가 필요합니다.")
        if any(stage["kind"] == "sse" for stage in plan) and not args.sse_rates:
            raise PerfError("SSE Stress에는 --sse-rates를 지정하세요.")
        if any(stage["kind"] != "sse" for stage in plan) and not args.rates:
            raise PerfError("HTTP Stress에는 --rates를 지정하세요.")
    return plan


def fixture_check(plan, fixtures):
    missing = {stage["fixture"] for stage in plan if stage["fixture"] and stage["fixture"] not in fixtures["cases"]}
    if missing:
        raise PerfError(f"fixture가 없는 케이스: {', '.join(sorted(missing))}")


def planned_seconds(plan):
    return sum(stage["duration"] + stage["warmup"] + 2 * stage["hold_seconds"] + 45 for stage in plan)


def guard_snapshot(config, stage_id=None):
    stages = ""
    if stage_id:
        tags = f'run_id="{stage_id}"'
        stages = (
            f' or label_replace(sum(increase(k6_perf_completed_total{{{tags}}}[30s])), "guard", "completed", "", ".*")'
            f' or label_replace(sum(increase(k6_perf_server_error_total{{{tags}}}[30s])), "guard", "server_errors", "", ".*")'
        )
    promql = (
        f'label_replace({PROCESS_START}, "guard", "app_start", "", ".*")'
        f' or label_replace({MYSQL_START}, "guard", "mysql_start", "", ".*")'
        f' or label_replace(min({DISK_RATIO}), "guard", "disk_free", "", ".*")'
        f' or label_replace(min({MEMORY_RATIO}), "guard", "memory_free", "", ".*")'
        f' or label_replace(count(count by(job) ({DISK_RATIO})), "guard", "disk_jobs", "", ".*")'
        f' or label_replace(count(count by(job) ({MEMORY_RATIO})), "guard", "memory_jobs", "", ".*")'
        ' or label_replace(mysql_up{job="mysql"}, "guard", "mysql_up", "", ".*")'
        + stages
    )
    return {item["metric"]["guard"]: float(item["value"][1]) for item in query(config, promql)}


def guard_reason(snapshot, first):
    if snapshot.get("app_start", first["app_start"]) != first["app_start"]:
        return "app_restarted"
    if abs(snapshot.get("mysql_start", first["mysql_start"]) - first["mysql_start"]) > 3:
        return "mysql_restarted"
    if snapshot.get("disk_free", 1) < LIMITS["disk_free_ratio"]:
        return "disk_pressure"
    if snapshot.get("memory_free", 1) < LIMITS["memory_available_ratio"]:
        return "memory_pressure"
    count = snapshot.get("completed", 0)
    if count >= LIMITS["server_error_min_requests"] and snapshot.get("server_errors", 0) / count >= LIMITS["server_error_ratio"]:
        return "server_errors_or_timeouts"
    return None


def stop_container(config, container, process):
    env = config.env()
    for args in (["kill", "--signal=SIGINT", container], ["stop", "--time", "2", container]):
        try:
            subprocess.run(["docker", *args], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=12, check=False)
            process.wait(timeout=8)
            alive = subprocess.check_output(
                ["docker", "ps", "-q", "--filter", f"name=^/{container}$"], env=env,
                stderr=subprocess.DEVNULL, text=True, timeout=6,
            ).strip()
            if not alive:
                return
        except subprocess.SubprocessError:
            continue
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    raise PerfError("k6 컨테이너 종료를 확인하지 못했습니다. PC A에서 해당 실행을 확인하세요.")


def collect(config, stage, stage_id, start, end, drain_end):
    run_label = f'run_id="{stage_id}"'
    tags = run_label + ',phase="measurement"'
    expressions = {key: value.replace("${route}", stage["path"]) for key, value in SERIES.items()}
    expressions.update({key: value.replace("${tags}", tags).replace("${run}", run_label)
                        for key, value in K6_SERIES.items()})
    allowed_labels = {"__name__", "guard", "job", "method", "status", "pool", "area", "name"}
    def collect_one(item):
        key, expression = item
        try:
            stop = drain_end if key.startswith("k6_") else end
            rows = query(config, expression, start, stop, step=max(5, int((stop - start) / 800)))
            return key, [{"labels": {k: v for k, v in row["metric"].items() if k in allowed_labels},
                          "values": row["values"]} for row in rows]
        except PerfError:
            return key, None

    # Query after k6 exits, with bounded concurrency to keep wall time predictable.
    with ThreadPoolExecutor(max_workers=4) as workers:
        return dict(workers.map(collect_one, expressions.items()))


def k6_image(config):
    try:
        output = subprocess.check_output(
            ["docker", "image", "inspect", "matchuri-perf-k6:2.3.0-sse0.2.0", "--format", "{{.Id}}"],
            cwd=ROOT, env=config.env(), stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        raise PerfError("PC A의 고정 k6 이미지를 준비하세요 (up --role a).") from None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", output):
        raise PerfError("PC A k6 이미지 ID를 검증하지 못했습니다.")
    return output


def observer_images(config):
    inspected = {}
    for service in ("prometheus", "grafana"):
        try:
            container_id = subprocess.check_output(config.compose() + ["ps", "-q", service],
                                                   cwd=ROOT, env=config.env(), stderr=subprocess.DEVNULL,
                                                   text=True, timeout=15).strip()
            if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
                raise PerfError(f"PC A의 {service} 컨테이너가 하나 필요합니다.")
            image_id = subprocess.check_output(["docker", "inspect", container_id, "--format", "{{.Image}}"],
                                               cwd=ROOT, env=config.env(), stderr=subprocess.DEVNULL,
                                               text=True, timeout=10).strip()
        except (OSError, subprocess.SubprocessError):
            raise PerfError(f"PC A의 {service} 이미지 ID를 확인할 수 없습니다.") from None
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise PerfError(f"PC A의 {service} 이미지 ID 형식이 올바르지 않습니다.")
        inspected[service] = image_id
    return inspected


def environment_metadata(config):
    return {
        "host_a_profile": config.value("PERF_HOST_A_PROFILE", "unset"),
        "host_b_profile": config.value("PERF_HOST_B_PROFILE", "unset"),
        "network_profile": config.value("PERF_NETWORK_PROFILE", "unset"),
        "host_a_os": platform.system(),
        "host_a_arch": platform.machine(),
        "host_a_logical_cpus": os.cpu_count(),
        "api_port": config.api_port,
        "management_port": config.app_metrics_port,
        "mysql_metrics_port": config.mysql_metrics_port,
    }


def stage_result(config, stage, args, identity, folder, directory, stage_id, first):
    directory.mkdir(mode=0o700)
    summary_file = directory / "summary.json"
    container = f"matchuri-perf-k6-{stage_id}"
    env_values = {
        "BASE_URL": config.origin, "PERF_BACKEND_SHA": identity["backend_sha"], "PERF_RUN_ID": stage_id,
        "PERF_API_ID": stage["operation_id"], "PERF_CASE_ID": stage["case_id"], "PERF_SUITE": args.suite,
        "PERF_RATE": str(stage["rate"]), "PERF_RATE_UNIT": stage["rate_unit"],
        "PERF_DURATION": str(stage["duration"]), "PERF_WARMUP": str(stage["warmup"]),
        "PERF_VUS": str(stage["vus"]), "PERF_MAX_VUS": str(stage["vus"]), "PERF_HTTP_TIMEOUT": "5",
        "PERF_SSE_HOLD_SECONDS": str(stage["hold_seconds"] or 70),
        "PERF_FIXTURES_PATH": "/fixtures/fixtures.json", "PERF_PASSWORD_PATH": "/fixtures/account-password",
        "PERF_SUMMARY_PATH": "/results/summary.json",
    }
    command = config.compose() + ["--profile", "load-test", "run", "--no-deps", "--pull", "never", "--rm", "-T", "--name", container,
                                  "-v", f"{directory}:/results", "-v", f"{folder}:/fixtures:ro"]
    if os.name != "nt":
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for key, value in env_values.items():
        command += ["-e", f"{key}={value}"]
    command += ["k6", "run", "-o", "experimental-prometheus-rw",
                "/scripts/sse.js" if stage["kind"] == "sse" else "/scripts/http.js"]
    start = time.time()
    session = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=ROOT, env=config.env(), stdin=subprocess.DEVNULL, **session,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + stage["warmup"] + stage["duration"] + 2 * stage["hold_seconds"] + 100
    health_errors = scrape_errors = 0
    reason = None
    try:
        while process.poll() is None:
            try:
                healthy = is_healthy(config)
            except PerfError:
                healthy = False
            health_errors = 0 if healthy else health_errors + 1
            try:
                snapshot = guard_snapshot(config, stage_id)
                scraping = (set(JOB_NAMES).issubset(up_jobs(config)) and snapshot.get("mysql_up") == 1 and
                            {"app_start", "mysql_start"}.issubset(snapshot) and
                            snapshot.get("disk_jobs") == snapshot.get("memory_jobs") == 2)
                reason = guard_reason(snapshot, first)
            except PerfError:
                scraping = False
            scrape_errors = 0 if scraping else scrape_errors + 1
            if reason is None and health_errors >= LIMITS["health_failures"]:
                reason = "health_unavailable"
            if reason is None and scrape_errors >= LIMITS["scrape_failures"]:
                reason = "metrics_unavailable"
            if reason is None and time.monotonic() >= deadline:
                reason = "stage_deadline"
            if reason:
                break
            try:
                process.wait(timeout=LIMITS["poll_seconds"])
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        reason = "cancelled"
    finally:
        if reason or process.poll() is None:
            try:
                stop_container(config, container, process)
            except (PerfError, OSError, subprocess.SubprocessError):
                reason = f"{reason or 'stage_stop'};container_stop_unverified"

    end = time.time()
    try:
        summary = read_json(summary_file) if summary_file.is_file() else {}
    except PerfError:
        summary = {}
        reason = reason or "summary_unreadable"
    if not isinstance(summary, dict) or not isinstance(summary.get("measurement", {}), dict):
        summary = {}
        reason = reason or "summary_unreadable"
    measurement = summary.get("measurement", {})
    expected_summary = {"run_id": stage_id, "operation_id": stage["operation_id"], "case_id": stage["case_id"],
                        "suite": args.suite, "target_rate": stage["rate"], "rate_unit": stage["rate_unit"]}
    if summary and any(summary.get(key) != value for key, value in expected_summary.items()):
        summary = {}
        measurement = {}
        reason = reason or "summary_identity_mismatch"
    query_start = measurement["start_ms"] / 1000 if measurement.get("start_ms") else start
    query_end = min(measurement["end_ms"] / 1000, end) if measurement.get("end_ms") else end
    if query_start >= query_end:
        query_start, query_end = start, end
    expected_starts = stage["rate"] * stage["duration"] / (60 if stage["rate_unit"] == "1m" else 1)
    dropped = measurement.get("dropped", 0)
    actual = measurement.get("started", 0)
    # Arrival-rate executors may place one less sample at the edge of a short window.
    tolerance = max(1, expected_starts * (1 - LIMITS["min_arrival_fraction"]))
    reached = bool(actual and not dropped and actual >= expected_starts - tolerance)
    process_success = process.returncode == 0 and bool(summary)
    smoke_check_failed = (args.suite == "smoke" and process.returncode == 99
                          and measurement.get("failed", 0) > 0
                          and measurement.get("server_errors", 0) == 0 and reached)
    record = {
        "stage_id": stage_id, "plan": stage, "started_utc": utc(start), "ended_utc": utc(end),
        "measurement_window_utc": [utc(query_start), utc(query_end)],
        "process_exit_code": process.returncode, "execution_status": "aborted" if reason else "completed" if process_success else "failed",
        "arrival_status": "attained" if reached else "unattained",
        "stop_reason": reason or ("k6_exit" if not process_success and not smoke_check_failed else None),
        "measurement": measurement, "sse": summary.get("sse"),
    }
    # Write the core result before querying Prometheus so a later interruption leaves the stage usable.
    write_json(directory / "stage.json", record)
    exported = collect(config, stage, stage_id, query_start, query_end, end)
    write_json(directory / "metrics.json", exported)
    required = ("scrape_health", "k6_started_count", "jvm_heap", "mysql_up", "host_cpu_busy",
                "host_disk_free_ratio", "host_memory_free_ratio", "hikari_connections", "tomcat_threads")
    host_jobs = {"host-a", "host-b"}
    has_host_data = all(host_jobs <= {row["labels"].get("job") for row in exported[key] or []}
                        for key in ("host_cpu_busy", "host_disk_free_ratio", "host_memory_free_ratio"))
    record["metrics_status"] = ("partial" if not has_host_data or any(value is None for value in exported.values())
                                or not all(exported[key] for key in required) else "collected")
    if record["metrics_status"] == "partial" and record["stop_reason"] is None:
        record["stop_reason"] = "metrics_partial"
    write_json(directory / "stage.json", record)
    return record


def utc(timestamp=None):
    value = datetime.now(UTC) if timestamp is None else datetime.fromtimestamp(timestamp, UTC)
    return value.isoformat()


def run(config, args, plan, folder, fixtures):
    local_docker(config)
    fixture_check(plan, fixtures)
    if not (1 <= len(plan) <= 2000):
        raise PerfError("한 실행에서 최대 2,000단계를 지원합니다.")
    RESULTS.mkdir(mode=0o700, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    lock = config.runtime / "run.lock"
    try:
        with lock.open("x", encoding="ascii") as handle:
            handle.write(f"{run_id} {os.getpid()}\n")
    except FileExistsError:
        raise PerfError("PC A에서 이미 실행 중입니다 (runtime/a/run.lock).") from None

    try:
        identity = preflight(config, fixtures, planned_seconds(plan))
        image = k6_image(config)
        first = guard_snapshot(config)
        if (not {"app_start", "mysql_start"}.issubset(first) or
                first.get("disk_jobs") != 2 or first.get("memory_jobs") != 2):
            raise PerfError("중단 기준에 필요한 app/MySQL 시작 시각 또는 양쪽 호스트 디스크·메모리 지표가 없습니다.")
        if guard_reason(first, first):
            raise PerfError("사전 점검에서 호스트 자원 한도를 초과했습니다.")
        if shutil.disk_usage(RESULTS).free / shutil.disk_usage(RESULTS).total < LIMITS["disk_free_ratio"]:
            raise PerfError("PC A 결과 저장소의 여유 공간이 10% 미만입니다.")

        directory = RESULTS / run_id
        directory.mkdir(mode=0o700)
        manifest = {
            "schema": 1, "run_id": run_id, "started_utc": utc(), "suite": args.suite,
            "status": "running", "target": identity, "perf_git": git_state(ROOT),
            "perf_code_sha256": code_fingerprint(), "k6_image_id": image,
            "observer_images": observer_images(config),
            "environment_label": config.value("PERF_ENVIRONMENT_LABEL", "lan"),
            "environment": environment_metadata(config), "guards": LIMITS, "plan": plan,
            "stages": [], "skipped": [],
        }
        write_json(directory / "manifest.json", manifest)
        print(f"Run {run_id} · {len(plan)} stages", flush=True)
        try:
            max_observed_rate = {}
            partial = False
            for index, stage in enumerate(plan, 1):
                stage_id = f"{run_id}-{index:03d}"
                case_key = (stage["operation_id"], stage["case_id"])
                if stage["rate"] > max_observed_rate.get(case_key, stage["rate"]):
                    manifest["skipped"].append({"id": stage_id, "plan": stage, "reason": "lower_rate_unattained"})
                    continue
                print(f"[{index}/{len(plan)}] {stage['operation_id']}/{stage['case_id']} "
                      f"{stage['rate']}/{stage['rate_unit']} · repeat {stage['repeat']}", flush=True)
                current = target_identity(config, fixtures, stage["duration"] + stage["warmup"] + 2 * stage["hold_seconds"])
                if current != identity:
                    raise PerfError("실행 중 PC B 식별자가 변경되었습니다.")
                if stage.get("one_shot"):
                    claim_expiration(config, identity, stage)
                record = stage_result(config, stage, args, identity, folder, directory / stage_id, stage_id, first)
                manifest["stages"].append({"id": stage_id, "status": record["execution_status"], "reason": record["stop_reason"],
                                           "arrival_status": record["arrival_status"], "metrics_status": record["metrics_status"]})
                write_json(directory / "manifest.json", manifest)
                if record["stop_reason"]:
                    manifest["status"] = "stopped"
                    manifest["stop_reason"] = record["stop_reason"]
                    break
                if record["execution_status"] != "completed":
                    partial = True
                if record["arrival_status"] != "attained":
                    if args.suite != "load":
                        manifest["status"] = "stopped"
                        manifest["stop_reason"] = "arrival_rate_unattained"
                        break
                    max_observed_rate[case_key] = stage["rate"]
                    partial = True
                if record["metrics_status"] != "collected" and args.suite == "load":
                    partial = True
            else:
                manifest["status"] = "partial" if partial else "completed"
        except (PerfError, OSError, KeyboardInterrupt, subprocess.SubprocessError) as exc:
            manifest["status"] = "stopped"
            manifest["stop_reason"] = type(exc).__name__
            raise
        finally:
            manifest["ended_utc"] = utc()
            write_json(directory / "manifest.json", manifest)
        if manifest["status"] == "partial":
            raise PerfError(f"Run {run_id} 부분 완료. 실패·미달 단계의 결과를 보존했습니다.")
        if manifest["status"] != "completed":
            raise PerfError(f"Run {run_id} 중단 ({manifest['stop_reason']}). 부분 결과를 보존했습니다.")
    finally:
        lock.unlink(missing_ok=True)


def claim_expiration(config, identity, stage):
    if stage["fixture"] not in identity["first_expiration_available"]:
        raise PerfError("대상 DB에 실행 가능한 최초 만료 케이스가 없습니다.")
    stamp = datetime.fromisoformat(identity["anchor"].replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%S")
    path = config.runtime / "claims" / f"{stamp}-{stage['operation_id']}-{identity['fingerprint'][-12:]}"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        with path.open("x", encoding="ascii") as handle:
            handle.write(utc() + "\n")
    except FileExistsError:
        raise PerfError("같은 fixture의 최초 만료 케이스가 이 PC에서 이미 실행되었습니다.") from None
