"""Prepare isolated local settings and validate the target without sending load."""

from datetime import UTC, datetime, timedelta
import base64
import os
import re
import secrets

from .common import (
    BACKEND, CATALOG, IDENTITY_PATH, JAVA_OPTION, JOB_NAMES, PERF_NAME, ROOT, SHA, PerfError,
    call, exporter_target, get_json, git_state, lan_ip, local_docker, query, read_json, write_json,
)

SECRET_NAMES = ("mysql-root", "mysql-app", "mysql-exporter", "jwt", "provider-aes", "account-password")
DATASETS = ("mvp-v1", "mvp-v1-expanded", "smoke-v1")
FINGERPRINT = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
REQUIRED_SERIES = (
    'mysql_up{job="mysql"} == 1',
    'process_cpu_usage{job="matchuri-backend"}',
    'jvm_memory_used_bytes{job="matchuri-backend"}',
    'jvm_threads_live_threads{job="matchuri-backend"}',
    'hikaricp_connections_active{job="matchuri-backend"}',
    'tomcat_threads_busy_threads{job="matchuri-backend"}',
    'node_time_seconds{job="host-a"} or windows_system_system_up_time{job="host-a"} or windows_cpu_time_total{job="host-a"}',
    'node_time_seconds{job="host-b"} or windows_system_system_up_time{job="host-b"} or windows_cpu_time_total{job="host-b"}',
)
def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def create_secret(path, value):
    if not path.exists():
        with path.open("x", encoding="ascii") as handle:
            handle.write(value + "\n")
        if os.name != "nt":
            # Compose mounts files for non-root container users. The parent is 0700.
            path.chmod(0o444)


def target_files(config):
    ip = config.target_ip
    targets = {
        "app": f"{ip}:{config.app_metrics_port}",
        "mysql": f"{ip}:{config.mysql_metrics_port}",
        "host-a": exporter_target(config.value("PERF_HOST_A_TARGET"), allow_host_alias=True),
        "host-b": exporter_target(config.value("PERF_HOST_B_TARGET")),
    }
    if targets["host-b"].split(":")[0] != ip:
        raise PerfError("PC B exporter와 API는 같은 LAN 주소를 사용해야 합니다.")
    directory = config.runtime / "targets"
    public_directory(directory)
    for name, target in targets.items():
        path = directory / f"{name}.json"
        write_json(path, [{"targets": [target]}])
        readable_file(path)


def public_directory(directory):
    directory.mkdir(mode=0o755, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o755)


def readable_file(path):
    if os.name != "nt":
        path.chmod(0o644)


def invalidate_identity(config):
    path = config.runtime / "identity" / "identity.json"
    write_json(path, {"environment": "unseeded"})
    readable_file(path)


def prepare(config):
    private_dir(config.runtime)
    if config.role == "a":
        target_files(config)
        private_dir(config.runtime / "fixtures")
        create_secret(config.runtime / "grafana-password", secrets.token_urlsafe(24))
        return
    lan_ip(config.value("PERF_BIND_IP"))
    if len({config.api_port, config.app_metrics_port, config.mysql_metrics_port}) != 3:
        raise PerfError("PC B의 API·관리·DB exporter 포트는 서로 달라야 합니다.")
    secret_dir = config.runtime / "secrets"
    private_dir(secret_dir)
    for name in SECRET_NAMES:
        if name == "provider-aes":
            value = base64.b64encode(secrets.token_bytes(32)).decode()
        elif name == "mysql-exporter":
            value = secrets.token_hex(32)
        else:
            value = secrets.token_urlsafe(48)
        create_secret(secret_dir / name, value)
    public_dir = config.runtime / "identity"
    public_directory(public_dir)
    if not (public_dir / "identity.json").exists():
        invalidate_identity(config)


def backend_build_env():
    backend = git_state(BACKEND)
    if backend["dirty"]:
        raise PerfError("PC B 이미지 빌드 전에 백엔드 작업 트리를 커밋된 상태로 준비하세요.")
    return {"PERF_BACKEND_SHA": backend["sha"]}


def up(config):
    local_docker(config)
    if config.role == "a" and (config.runtime / "run.lock").exists():
        raise PerfError("활성 측정 중에는 PC A 서비스를 변경할 수 없습니다.")
    prepare(config)
    if config.role == "a":
        call(config.compose() + ["up", "-d", "prometheus", "grafana"], config, "PC A Compose")
        call(config.compose() + ["--profile", "load-test", "build", "k6"], config, "k6 이미지 빌드")
    else:
        build_env = backend_build_env()
        invalidate_identity(config)
        call(config.compose() + ["up", "--build", "--wait", "--wait-timeout", "240"], config, "PC B Compose", **build_env)


def stop(config):
    local_docker(config)
    if config.role == "a" and (config.runtime / "run.lock").exists():
        raise PerfError("실행 중인 perf.py를 먼저 종료하세요.")
    env = {"PERF_BACKEND_SHA": git_state(BACKEND)["sha"]} if config.role == "b" else {}
    profile = ["--profile", "load-test"] if config.role == "a" else []
    call(config.compose() + profile + ["stop"], config, "Compose 중지", **env)


def seed(config, dataset):
    local_docker(config)
    secret = config.runtime / "secrets" / "account-password"
    if not secret.is_file():
        raise PerfError("먼저 PC B에서 prepare 또는 up을 실행하세요.")
    import sys
    env = backend_build_env()
    env["MATCHURI_PERF_ACCOUNT_PASSWORD"] = secret.read_text(encoding="ascii").strip()
    call([sys.executable, str(ROOT / "fixtures" / "seed.py"), "--env-file", str(config.env_file),
          "--project-name", config.project, "--dataset", dataset,
          "--output-dir", str(config.runtime)], config, "fixture 적재", **env)


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Fixture seed records its local MySQL timestamps as UTC.
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


def fixture_data(config):
    folder = config.runtime / "fixtures"
    fixtures = read_json(folder / "fixtures.json")
    if not isinstance(fixtures, dict) or not isinstance(fixtures.get("cases"), dict) or not isinstance(fixtures.get("users"), dict):
        raise PerfError("fixture JSON 형식이 올바르지 않습니다.")
    password = folder / "account-password"
    if not password.is_file() or not 16 <= len(password.read_text(encoding="ascii").strip()) <= 72:
        raise PerfError("PC B의 합성 계정 비밀번호 파일을 A의 fixture 폴더에 준비하세요.")
    return folder, fixtures


def target_identity(config, fixtures, planned_seconds=0):
    identity = get_json(config.origin + IDENTITY_PATH, "PC B 식별자")
    if not isinstance(identity, dict) or identity.get("environment") != PERF_NAME:
        raise PerfError("PC B의 성능 테스트 환경 표식이 일치하지 않습니다.")
    required = ("dataset", "fingerprint", "anchor")
    if any(not identity.get(key) or identity.get(key) != fixtures.get(key) for key in required):
        raise PerfError("PC B의 dataset, fingerprint 또는 anchor가 A의 fixture와 다릅니다.")
    if identity["dataset"] not in DATASETS or not FINGERPRINT.fullmatch(str(identity["fingerprint"])):
        raise PerfError("데이터셋 식별자 또는 체크섬 형식이 올바르지 않습니다.")
    if not SHA.fullmatch(str(identity.get("backend_sha", ""))):
        raise PerfError("PC B 이미지의 backend SHA를 확인할 수 없습니다.")
    try:
        now = datetime.now(UTC)
        anchor = parse_time(identity["anchor"])
        deadline = parse_time(identity["valid_until"]) if identity.get("valid_until") else anchor + timedelta(hours=23)
        if anchor > now + timedelta(seconds=30) or now + timedelta(seconds=planned_seconds + 120) >= deadline:
            raise PerfError("예정 실행 시간 동안 유지되는 활성 fixture가 필요합니다. B에서 새 데이터셋을 준비하세요.")
    except (ValueError, OverflowError):
        raise PerfError("fixture 기준 시각의 형식이 올바르지 않습니다.") from None
    metadata = {key: identity[key] for key in ("environment", "dataset", "fingerprint", "backend_sha", "anchor")}
    metadata["valid_until"] = deadline.isoformat()
    for key in ("app_image_id", "mysql_image_id"):
        value = identity.get(key)
        if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            metadata[key] = value
    runtime = identity.get("runtime")
    metadata["runtime"] = {}
    if isinstance(runtime, dict):
        for key in ("app_memory_bytes", "mysql_memory_bytes"):
            if type(runtime.get(key)) is int and runtime[key] > 0:
                metadata["runtime"][key] = runtime[key]
        buffer_pool = runtime.get("mysql_innodb_buffer_pool_size")
        if isinstance(buffer_pool, str) and re.fullmatch(r"\d+[KMG]?", buffer_pool, flags=re.IGNORECASE):
            metadata["runtime"]["mysql_innodb_buffer_pool_size"] = buffer_pool
        java = runtime.get("java_memory_options", [])
        if isinstance(java, list) and all(isinstance(item, str) and JAVA_OPTION.fullmatch(item) for item in java):
            metadata["runtime"]["java_memory_options"] = java
    if isinstance(identity.get("seed"), int):
        metadata["seed"] = identity["seed"]
    counts = identity.get("counts")
    if isinstance(counts, dict):
        metadata["counts"] = {key: value for key, value in counts.items()
                              if re.fullmatch(r"[a-z_]+", key) and isinstance(value, int)}
    available = identity.get("first_expiration_available", [])
    if not isinstance(available, list) or any(not isinstance(key, str) for key in available):
        raise PerfError("PC B 최초 만료 케이스 상태를 읽을 수 없습니다.")
    metadata["first_expiration_available"] = [key for key in available if key in {
        "REC.050.000/first-expiration", "GREC.050.000/first-expiration"}]
    return metadata


def is_healthy(config):
    url = f"http://{config.target_ip}:{config.app_metrics_port}/api/v1/health"
    return get_json(url, "PC B health").get("status") == "UP"


def up_jobs(config):
    result = query(config, '(up == 1) and (time() - timestamp(up) < 45)')
    return {row["metric"].get("job") for row in result}


def preflight(config, fixture, planned_seconds=0):
    identity = target_identity(config, fixture, planned_seconds)
    backend = git_state(BACKEND)
    if backend["dirty"] or identity["backend_sha"] != backend["sha"]:
        raise PerfError("PC A의 깨끗한 백엔드 체크아웃이 PC B 이미지 SHA와 일치해야 합니다.")
    if not is_healthy(config):
        raise PerfError("PC B 애플리케이션 health가 UP이 아닙니다.")
    missing = set(JOB_NAMES) - up_jobs(config)
    if missing:
        raise PerfError("응답이 없는 Prometheus 타깃: " + ", ".join(sorted(missing)))
    for expression in REQUIRED_SERIES:
        if not query(config, expression):
            raise PerfError(f"필수 Prometheus 시계열이 없습니다 ({expression}).")
    if not query(config, 'abs(node_time_seconds{job="host-b"} - time()) < 30') and query(
            config, 'node_time_seconds{job="host-b"}'):
        raise PerfError("PC B의 시계가 Prometheus와 30초 이상 다릅니다.")
    flags = get_json(config.prom + "/api/v1/status/flags", "Prometheus flags")
    if flags.get("data", {}).get("web.enable-remote-write-receiver") != "true":
        raise PerfError("Prometheus remote-write receiver를 확인하세요.")
    return identity


def audit_catalog():
    import importlib.util
    import sys

    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise PerfError("카탈로그 JSON에 중복 필드가 있습니다.")
        return fields

    import json
    operations = json.loads(CATALOG.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)["operations"]
    source = BACKEND / "scripts" / "audit_api_contract.py"
    spec = importlib.util.spec_from_file_location("matchuri_api_audit", source)
    registry = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = registry
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(registry)
    finally:
        sys.dont_write_bytecode = previous
    report, has_findings = registry.report(BACKEND)
    if has_findings:
        raise PerfError("백엔드 API 레지스트리 감사 실패\n" + report)
    readable_posts = {"/api/v1/guest/recommendations", "/api/v1/groups/invite-links/preview"}
    expected = {(item.api_id, item.method, item.path) for item in registry.parse_openapi_metadata(BACKEND)
                if item.method == "GET" or (item.method == "POST" and item.path in readable_posts)}
    actual = {(item["id"], item["method"], item["path"]) for item in operations}
    if len(actual) != len(operations) or len({item["id"] for item in operations}) != len(operations):
        raise PerfError("카탈로그에 중복 API가 있습니다.")
    if expected != actual:
        raise PerfError(f"조회 카탈로그 불일치. 누락 {sorted(expected - actual)}, 추가 {sorted(actual - expected)}")
    dataset_cases = {"ONB.120.000", "ADMIN.010.000", "ADMIN.050.000", "ADMIN.090.000", "ADMIN.160.000"}
    for operation in operations:
        cases = operation["cases"]
        minimum = 1 if operation["kind"] == "health" or operation["id"] in dataset_cases else 2
        if len(cases) < minimum or len({case["id"] for case in cases}) != len(cases):
            raise PerfError(f"중복 케이스 또는 복수 케이스가 없는 API: {operation['id']}")
        if operation["kind"] not in {"http", "health", "oauth", "sse"}:
            raise PerfError(f"지원하지 않는 조회 유형: {operation['id']}")
    return operations
