"""Read and compare archived results without connecting to either PC."""

from collections import defaultdict
import re
from statistics import median

from .common import RESULTS, PerfError, read_json

RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
STAGE_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}-\d{3,4}")


def load_run(run_id):
    if not RUN_ID.fullmatch(run_id):
        raise PerfError("올바른 Run ID를 지정하세요.")
    directory = RESULTS / run_id
    manifest = read_json(directory / "manifest.json")
    return directory, manifest


def list_runs():
    if not RESULTS.exists():
        return
    for directory in sorted(RESULTS.iterdir(), reverse=True):
        if directory.is_dir() and RUN_ID.fullmatch(directory.name) and (directory / "manifest.json").is_file():
            manifest = read_json(directory / "manifest.json")
            print(directory.name, manifest["suite"], manifest["status"], manifest["target"]["dataset"])


def load_stages(directory, manifest):
    found = {}
    for stage in manifest["stages"]:
        if not STAGE_ID.fullmatch(stage["id"]) or not stage["id"].startswith(manifest["run_id"] + "-"):
            raise PerfError("결과의 단계 ID가 올바르지 않습니다.")
        found[stage["id"]] = read_json(directory / stage["id"] / "stage.json")
    # A stage core is written before metrics and before the next manifest update.
    for path in directory.glob("*/stage.json"):
        stage_id = path.parent.name
        if (stage_id not in found and STAGE_ID.fullmatch(stage_id)
                and stage_id.startswith(manifest["run_id"] + "-")):
            found[stage_id] = read_json(path)
    return [found[stage_id] for stage_id in sorted(found)]


def show(run_id):
    directory, manifest = load_run(run_id)
    print(run_id, manifest["status"], manifest["target"]["backend_sha"], manifest["target"]["dataset"])
    for record in load_stages(directory, manifest):
        stage, measurement = record["plan"], record["measurement"]
        metric = (record.get("sse") or {}).get("first_event_ms") or measurement.get("latency_ms", {})
        print(f"{stage['operation_id']}/{stage['case_id']} r{stage['repeat']} "
              f"목표 {stage['rate']}/{stage['rate_unit']} "
              f"실제 {measurement.get('started_per_second', '–')}/s p95 {metric.get('p95', '–')} ms "
              f"{record['execution_status']}/{record['arrival_status']} "
              f"오류 {measurement.get('failed', '–')} "
              f"(상태 {measurement.get('unexpected_statuses', '–')}, 계약 {measurement.get('contract_failures', '–')}) "
              f"{record.get('stop_reason') or ''}")
    for item in manifest.get("skipped", []):
        stage = item["plan"]
        print(f"{stage['operation_id']}/{stage['case_id']} {stage['rate']}/{stage['rate_unit']} "
              f"skipped ({item['reason']})")


def key(record):
    plan = record["plan"]
    return tuple(plan[name] for name in ("operation_id", "case_id", "rate", "rate_unit", "duration", "warmup", "vus", "hold_seconds"))


def valid_groups(records):
    groups = defaultdict(list)
    for record in records:
        if (record["execution_status"] == "completed" and record["arrival_status"] == "attained"
                and record.get("metrics_status") == "collected" and not record.get("stop_reason")):
            groups[key(record)].append(record)
    return groups


def metrics_for(group):
    measurements = [record["measurement"] for record in group]
    latencies = [(record.get("sse") or {}).get("first_event_ms") or record["measurement"].get("latency_ms", {})
                 for record in group]
    fields = {name: [item[name] for item in measurements if isinstance(item.get(name), (int, float))]
              for name in ("started_per_second", "completed_per_second", "error_rate")}
    fields.update({name: [item[name] for item in latencies if isinstance(item.get(name), (int, float))]
                   for name in ("count", "p50", "p95", "p99")})
    return fields


def describe(values):
    if not values:
        return "–"
    points = ", ".join(f"{value:.5g}" for value in values)
    return f"{median(values):.5g} [{min(values):.5g}–{max(values):.5g}] ({points})"


def compare(baseline_id, candidate_id):
    left_dir, baseline = load_run(baseline_id)
    right_dir, candidate = load_run(candidate_id)
    settings = ("schema", "suite", "environment_label", "environment", "k6_image_id", "observer_images", "perf_code_sha256")
    target_settings = ("dataset", "fingerprint", "mysql_image_id", "runtime")
    differences = [name for name in settings if baseline.get(name) != candidate.get(name)]
    differences += [name for name in target_settings if baseline["target"].get(name) != candidate["target"].get(name)]
    if differences:
        raise PerfError("비교 조건 불일치: " + ", ".join(differences))
    left_stages, right_stages = load_stages(left_dir, baseline), load_stages(right_dir, candidate)
    left, right = valid_groups(left_stages), valid_groups(right_stages)
    comparable = set(left) & set(right)
    if not comparable:
        raise PerfError("공통 조건에서 완료된 유효 측정 단계가 없습니다. show로 단계 상태를 확인하세요.")
    print(f"{baseline_id} ({baseline['target']['backend_sha']}) → {candidate_id} ({candidate['target']['backend_sha']})")
    print(f"유효 공통 조건 {len(comparable)}개 · 공통 조건 밖/무효 단계 "
          f"{len(left_stages) - sum(len(left[item]) for item in comparable)}/"
          f"{len(right_stages) - sum(len(right[item]) for item in comparable)}")
    compared = 0
    for group_key in sorted(comparable):
        before, after = metrics_for(left[group_key]), metrics_for(right[group_key])
        if not before["started_per_second"] or not after["started_per_second"]:
            continue
        actual_left, actual_right = median(before["started_per_second"]), median(after["started_per_second"])
        if abs(actual_left - actual_right) > max(actual_left, actual_right) * 0.02:
            print(f"{group_key[0]}/{group_key[1]} {group_key[2]}/{group_key[3]} · 실제 요청률 차이 >2%")
            continue
        compared += 1
        print(f"\n{group_key[0]}/{group_key[1]} {group_key[2]}/{group_key[3]} · "
              f"{len(left[group_key])}/{len(right[group_key])}회 (중앙값 [범위] (반복값))")
        for metric, old in before.items():
            new = after[metric]
            delta = ""
            if old and new and metric in {"p50", "p95", "p99"} and (base := median(old)) > 0:
                delta = f" · {((median(new) / base) - 1) * 100:+.1f}%"
            if old and new and metric == "error_rate":
                delta = f" · {(median(new) - median(old)) * 100:+.2f}%p"
            print(f"  {metric}: {describe(old)} → {describe(new)}{delta}")
    if not compared:
        raise PerfError("실제 요청률 차이가 허용 범위 안인 공통 단계가 없습니다.")
