from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.common import PerfError, lan_ip
from tools.results import metrics_for, valid_groups
from tools.run import guard_reason, stage_plan
from tools.setup import audit_catalog, target_identity


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = audit_catalog()

    def plan(self, **overrides):
        args = dict(api="all", case=None, suite="smoke", repeat=1, rates=None, sse_rates=None,
                    warmup=None, duration=None, vus=None)
        return stage_plan(self.catalog, SimpleNamespace(**(args | overrides)))

    def test_all_smoke_covers_every_route_without_expiration_mutations(self):
        stages = self.plan()
        self.assertEqual({operation["id"] for operation in self.catalog},
                         {stage["operation_id"] for stage in stages})
        self.assertFalse(any(stage.get("one_shot") for stage in stages))

    def test_load_uses_constant_compatible_units(self):
        stages = self.plan(suite="load", repeat=3, rates=[5], sse_rates=[8])
        self.assertEqual({(5, "1s"), (8, "1m")},
                         {(stage["rate"], stage["rate_unit"]) for stage in stages})
        self.assertFalse(any(stage["kind"] == "oauth" for stage in stages))

    def test_transition_requires_explicit_single_request(self):
        path = {"api": "REC.050.000", "case": "first-expiration"}
        stage, = self.plan(**path)
        self.assertTrue(stage["one_shot"])
        self.assertEqual((1, "1m", 15, 0, 1),
                         tuple(stage[key] for key in ("rate", "rate_unit", "duration", "warmup", "vus")))
        with self.assertRaises(PerfError):
            self.plan(**path, repeat=2)

    def test_scope_and_watchdog_reject_unsafe_target_and_stop_conditions(self):
        for value in ("https://10.0.0.1", "api.example.com", "127.0.0.1", "169.254.169.254"):
            with self.subTest(value=value), self.assertRaises(PerfError):
                lan_ip(value)
        first = {"app_start": 100, "mysql_start": 200}
        self.assertIsNone(guard_reason({"app_start": 100, "mysql_start": 201, "completed": 19, "server_errors": 19}, first))
        for snapshot, reason in (
            ({"app_start": 101}, "app_restarted"),
            ({"mysql_start": 208}, "mysql_restarted"),
            ({"disk_free": 0.05}, "disk_pressure"),
            ({"memory_free": 0.01}, "memory_pressure"),
            ({"completed": 20, "server_errors": 2}, "server_errors_or_timeouts"),
        ):
            self.assertEqual(guard_reason(snapshot, first), reason)

    def test_identity_sanitizes_metadata_and_requires_validity_through_planned_end(self):
        now = datetime.now(UTC)
        fixture = {"dataset": "mvp-v1", "fingerprint": "a" * 64, "anchor": now.isoformat()}
        identity = fixture | {
            "environment": "matchuri-perf", "backend_sha": "b" * 40,
            "valid_until": (now + timedelta(hours=2)).isoformat(),
            "runtime": {"app_memory_bytes": 1024, "raw_env": "omit",
                        "java_memory_options": ["-XX:MaxRAMPercentage=65", "-Dpassword=omit"]},
            "password": "omit",
        }
        config = SimpleNamespace(origin="http://192.168.50.12:18080")
        with patch("tools.setup.get_json", return_value=identity):
            result = target_identity(config, fixture)
            self.assertEqual({"app_memory_bytes": 1024}, result["runtime"])
            self.assertNotIn("password", result)
            with self.assertRaises(PerfError):
                target_identity(config, fixture | {"anchor": "different"})
            with self.assertRaises(PerfError):
                target_identity(config, fixture, planned_seconds=3 * 3600)

    def test_comparison_excludes_invalid_stages_and_keeps_repeat_values(self):
        stage = self.plan(api="OPS.020.000", case="heavy", suite="load")[0]
        good = {"plan": stage, "execution_status": "completed", "arrival_status": "attained",
                "metrics_status": "collected", "measurement": {"started_per_second": 2,
                                                                "latency_ms": {"count": 360, "p95": 50}}}
        groups = valid_groups([good, good | {"arrival_status": "unattained"},
                               good | {"metrics_status": "partial"}, good])
        group, = groups.values()
        self.assertEqual(metrics_for(group)["p95"], [50, 50])


if __name__ == "__main__":
    unittest.main()
