"""Shared paths, sanitized HTTP probes, and Compose configuration."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "backend"
RESULTS = ROOT / "results"
CATALOG = ROOT / "k6" / "catalog.json"
IDENTITY_PATH = "/__perf/identity"
PERF_NAME = "matchuri-perf"
LAN_NETWORKS = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
SHA = re.compile(r"[0-9a-f]{40}")
JOB_NAMES = ("matchuri-backend", "mysql", "host-a", "host-b", "prometheus")
JAVA_OPTION = re.compile(r"-XX:(?:Initial|Max)RAMPercentage=\d+(?:\.\d+)?|-Xm[sx]\d+[kKmMgG]?|-Duser\.timezone=UTC")


class PerfError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def get_json(url, label, timeout=4):
    try:
        with OPENER.open(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("response exceeds limit")
        return json.loads(payload)
    except (URLError, OSError, ValueError) as exc:
        raise PerfError(f"{label} 요청 실패 ({type(exc).__name__})") from None


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PerfError(f"{path.name} 읽기 실패 ({type(exc).__name__})") from None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def port(value):
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise PerfError("포트는 1–65535 범위의 정수여야 합니다.")
    return int(value)


def lan_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        raise PerfError("대상 주소에 사설 LAN IPv4를 지정하세요.") from None
    if not any(ip in network for network in LAN_NETWORKS):
        raise PerfError("대상 주소에 사설 LAN IPv4를 지정하세요.")
    return str(ip)


def exporter_target(value, allow_host_alias=False):
    host, sep, suffix = value.rpartition(":")
    if not sep:
        raise PerfError("호스트 exporter 주소는 IP:port 형식이어야 합니다.")
    if not (allow_host_alias and host == "host.docker.internal"):
        host = lan_ip(host)
    return f"{host}:{port(suffix)}"


class Config:
    def __init__(self, role, env_file, project_suffix=None):
        self.role = role
        self.slot = role if project_suffix is None else f"{role}-{project_suffix}"
        self.project = f"matchuri-perf-{self.slot}"
        self.env_file = env_file.resolve()
        self.values = {}
        try:
            lines = self.env_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            raise PerfError(f"{self.env_file.name}을(를) 예제 파일에서 준비하세요.") from None
        for index, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            valid_value = re.fullmatch(r"[-a-zA-Z0-9._:/]*", value)
            if key == "PERF_JAVA_TOOL_OPTIONS":
                valid_value = bool(value.split()) and all(JAVA_OPTION.fullmatch(option) for option in value.split())
            if not sep or not re.fullmatch(r"PERF_[A-Z_]+", key) or not valid_value:
                raise PerfError(f"{self.env_file.name} {index}행의 KEY=value 형식이 올바르지 않습니다.")
            if key in self.values:
                raise PerfError(f"{self.env_file.name}에 중복 설정이 있습니다.")
            self.values[key] = value

    def value(self, key, default=""):
        return self.values.get(key) or default

    @property
    def runtime(self):
        return ROOT / "runtime" / self.slot

    @property
    def api_port(self):
        return port(self.value("PERF_API_PORT", "18080"))

    @property
    def app_metrics_port(self):
        return port(self.value("PERF_MANAGEMENT_PORT", "18081"))

    @property
    def mysql_metrics_port(self):
        return port(self.value("PERF_MYSQL_METRICS_PORT", "19104"))

    @property
    def target_ip(self):
        return lan_ip(self.value("PERF_TARGET_IP"))

    @property
    def origin(self):
        return f"http://{self.target_ip}:{self.api_port}"

    @property
    def prom(self):
        return f"http://127.0.0.1:{port(self.value('PERF_PROMETHEUS_PORT', '19090'))}"

    def compose(self):
        file = "docker-compose.target.yml" if self.role == "b" else "docker-compose.yml"
        return ["docker", "compose", "--env-file", str(self.env_file), "-f", str(ROOT / file),
                "--project-name", self.project]

    def env(self, **overrides):
        # Shell overrides can silently change Compose binds, images, and secrets.
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("PERF_", "K6_", "COMPOSE_", "MATCHURI_", "DOCKER_CONTEXT", "DOCKER_HOST"))}
        if self.role == "b":
            env.update(PERF_SECRET_DIR=f"./runtime/{self.slot}/secrets",
                       PERF_IDENTITY_DIR=f"./runtime/{self.slot}/identity")
        if self.role == "a":
            env["PERF_GRAFANA_PASSWORD_FILE"] = "./runtime/a/grafana-password"
        return env | overrides


def query(config, expression, start=None, end=None, step=10):
    path = "/api/v1/query"
    args = {"query": expression}
    if start is not None:
        path += "_range"
        args.update(start=start, end=end, step=step)
    payload = get_json(config.prom + path + "?" + urlencode(args), "Prometheus", timeout=8)
    if not isinstance(payload, dict) or payload.get("status") != "success" or payload.get("warnings"):
        raise PerfError("Prometheus 질의 결과를 검증하지 못했습니다.")
    return payload["data"]["result"]


def git(repo, *args):
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        raise PerfError("Git 저장소 정보를 읽을 수 없습니다.") from None


def git_state(repo):
    return {"sha": git(repo, "rev-parse", "HEAD"), "dirty": bool(git(repo, "status", "--porcelain"))}


def code_fingerprint():
    digest = hashlib.sha256()
    paths = [ROOT / "perf.py", *ROOT.glob("docker-compose*.yml")]
    for directory in ("k6", "tools", "fixtures", "prometheus", "grafana", "backend", "exporters"):
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith("test_"))
    for path in sorted(paths):
        contents = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + contents + b"\0")
    return digest.hexdigest()


def call(command, config, label, **kwargs):
    try:
        result = subprocess.run(command, cwd=ROOT, env=config.env(**kwargs), check=False)
    except OSError:
        raise PerfError(f"{label} 실행 파일을 찾을 수 없습니다.") from None
    if result.returncode:
        raise PerfError(f"{label} 실패 (종료 코드 {result.returncode})")


def local_docker(config):
    try:
        host = subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            cwd=ROOT, env=config.env(), stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        raise PerfError("Docker 로컬 컨텍스트를 확인할 수 없습니다.") from None
    if not host.startswith(("npipe://", "unix://")):
        raise PerfError("이 실행기는 현재 PC의 Docker 로컬 컨텍스트만 지원합니다.")
