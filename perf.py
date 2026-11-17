#!/usr/bin/env python3
"""Command-line entry point for the Matchuri performance test environment."""

import argparse
from pathlib import Path
import re
import sys

from tools.common import ROOT, Config, PerfError
from tools.results import compare, list_runs, show
from tools.run import planned_seconds, run, stage_plan
from tools.setup import DATASETS, audit_catalog, fixture_data, preflight, prepare, seed, stop, up


def bounded_int(low, high):
    def convert(value):
        if not value.isdecimal() or not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f"{low}–{high} 범위의 정수를 사용하세요.")
        return int(value)
    return convert


def rates(limit):
    def convert(value):
        numbers = [bounded_int(1, limit)(item) for item in value.split(",")]
        if numbers != sorted(set(numbers)):
            raise argparse.ArgumentTypeError("부하 단계는 중복 없는 오름차순으로 지정하세요.")
        return numbers
    return convert


def selector(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise argparse.ArgumentTypeError("영문·숫자·점·하이픈·밑줄을 사용하세요.")
    return value


def suffix(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value):
        raise argparse.ArgumentTypeError("소문자·숫자·하이픈을 사용하세요.")
    return value


def plan_args(parser):
    parser.add_argument("--suite", required=True, choices=("smoke", "load", "stress"))
    parser.add_argument("--api", default="all", type=selector, metavar="ID")
    parser.add_argument("--case", type=selector)
    parser.add_argument("--repeat", type=bounded_int(1, 10), default=None)
    parser.add_argument("--rates", type=rates(50), metavar="RPS,RPS")
    parser.add_argument("--sse-rates", type=rates(30), metavar="PER_MIN,PER_MIN")
    parser.add_argument("--duration", type=bounded_int(15, 600), metavar="SECONDS")
    parser.add_argument("--warmup", type=bounded_int(0, 180), metavar="SECONDS")
    parser.add_argument("--vus", type=bounded_int(1, 200))


def parser():
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="command", required=True)
    commands.add_parser("audit", help="조회 카탈로그와 백엔드 레지스트리를 대조")
    commands.add_parser("list", help="저장된 실행 목록")
    commands.add_parser("plan", help="API/케이스별 실행 계획을 네트워크 없이 확인")
    plan_args(commands.choices["plan"])
    for name in ("prepare", "up", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--role", choices=("a", "b"), required=True)
        command.add_argument("--env-file", type=Path)
        command.add_argument("--project-suffix", type=suffix)
    for name in ("seed", "preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("--env-file", type=Path)
        command.add_argument("--dataset", choices=DATASETS)
    plan_args(commands.choices["run"])
    commands.choices["seed"].add_argument("--project-suffix", type=suffix)
    commands.add_parser("show").add_argument("run_id")
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("baseline_id")
    compare_parser.add_argument("candidate_id")
    return main


def main():
    args = parser().parse_args()
    if args.command == "audit":
        operations = audit_catalog()
        print(f"조회 API {len(operations)}개 · 케이스 {sum(len(operation['cases']) for operation in operations)}개")
        return
    if args.command in ("list", "show", "compare"):
        {"list": list_runs, "show": show, "compare": compare}[args.command](
            *[getattr(args, name) for name in ("run_id", "baseline_id", "candidate_id") if hasattr(args, name)]
        )
        return
    if args.command in ("run", "plan"):
        if args.repeat is None:
            args.repeat = 3 if args.suite == "load" else 1
        plan = stage_plan(audit_catalog(), args)
        if args.command == "plan":
            for stage in plan:
                print(f"{stage['operation_id']}/{stage['case_id']} {stage['rate']}/{stage['rate_unit']} "
                      f"{stage['warmup']}s warmup + {stage['duration']}s · repeat {stage['repeat']}")
            print(f"예상 상한 점검 구간 {planned_seconds(plan)}초 ({len(plan)}단계)")
            return
    role = getattr(args, "role", None) or ("b" if args.command == "seed" else "a")
    project_suffix = getattr(args, "project_suffix", None)
    if role == "a" and project_suffix:
        raise PerfError("--project-suffix는 PC B의 독립 볼륨에 사용합니다.")
    config = Config(role, args.env_file or ROOT / f"perf-{role}.env", project_suffix)
    if args.command == "prepare":
        prepare(config)
    elif args.command == "up":
        up(config)
    elif args.command == "stop":
        stop(config)
    elif args.command == "seed":
        seed(config, args.dataset or "mvp-v1")
    else:
        folder, fixtures = fixture_data(config)
        if args.dataset and args.dataset != fixtures.get("dataset"):
            raise PerfError("요청한 데이터셋과 fixture가 일치하지 않습니다.")
        if args.command == "preflight":
            identity = preflight(config, fixtures)
            print(f"PC B 확인: {identity['backend_sha']} / {identity['dataset']}")
        else:
            run(config, args, plan, folder, fixtures)


if __name__ == "__main__":
    try:
        main()
    except PerfError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError) as exc:
        print(f"실행 설정 또는 파일을 검증하지 못했습니다 ({type(exc).__name__}).", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
