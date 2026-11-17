"""Offline regression checks for case coverage and deep-page fixture data."""

from collections import defaultdict
from datetime import datetime, timezone
import unittest

from dataset import ACTORS, EXPIRY_GROUP, EXPIRY_GROUP_SESSION, EXPIRY_PERSONAL_REQUEST, Dataset, Reference
from seed import DEFAULT_BACKEND, first_expiration_available, insert_sql, load_backend_sources, validate_catalog


def reference_from_source():
    source, presets, agreements = load_backend_sources(DEFAULT_BACKEND)
    menus = {entry["code"]: index for index, entry in enumerate(source["menuItems"], start=1)}
    categories = {(entry["categoryType"], entry["code"]): index
                  for index, entry in enumerate(source["attributeCategories"], start=1)}
    ingredients = {entry["code"]: index for index, entry in enumerate(source["ingredients"], start=1)}
    menu_categories = defaultdict(set)
    for entry in source["menuAttributeCategories"]:
        menu_categories[menus[entry["menuCode"]]].add(categories[entry["categoryType"], entry["categoryCode"]])
    menu_ingredients = defaultdict(set)
    for entry in source["menuIngredients"]:
        menu_ingredients[menus[entry["menuCode"]]].add(ingredients[entry["ingredientCode"]])
    preset_ids = tuple(range(1, len(presets) + 1))
    return Reference(menus, categories, ingredients, menu_categories, menu_ingredients, preset_ids, "test"), agreements


class DatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference, cls.agreements = reference_from_source()
        cls.dataset = Dataset("smoke-v1", cls.reference, cls.agreements)

    def rows(self, table):
        columns, rows = self.dataset.tables[table]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def test_every_runtime_case_has_fixture_and_valid_actor(self):
        cases = self.dataset.cases({1: "small", 2: "large", 3: "expired"})
        validate_catalog(cases)
        for case in cases.values():
            if "actor" in case:
                self.assertIn(case["actor"], ACTORS)
        self.assertEqual([2, 3, 4], [variant["params"]["groupId"] for variant in cases["RT.020.000/large"]["variants"]])

    def test_deep_pages_have_qualifying_rows(self):
        self.assertGreaterEqual(sum(row["status"] == "SELECTED" and row["member_id"] == ACTORS["heavy"]
                                    for row in self.rows("personal_recommendations")), 300)
        self.assertGreaterEqual(sum(row["room_id"] == 2 for row in self.rows("group_recommendations")), 300)
        active_groups = {row["id"] for row in self.rows("group_rooms") if row["status"] == "ACTIVE"}
        self.assertEqual(300, sum(row["member_id"] == ACTORS["deep-groups"] and row["room_id"] in active_groups
                                  for row in self.rows("group_room_members")))
        self.assertEqual(300, sum(row["target_member_id"] == ACTORS["deep-invites"] and row["status"] == "PENDING"
                                  and row["expires_at"].total_seconds() > 0 for row in self.rows("group_invites")))
        self.assertEqual(8, sum(row["target_member_id"] == ACTORS["typical"] and row["status"] == "ACCEPTED"
                                for row in self.rows("group_invites")))

    def test_data_is_deterministic_and_variant_changes_counts(self):
        second = Dataset("smoke-v1", self.reference, self.agreements)
        self.assertEqual(self.dataset.fingerprint(), second.fingerprint())
        expanded = Dataset("mvp-v1-expanded", self.reference, self.agreements)
        self.assertNotEqual(self.dataset.fingerprint(), expanded.fingerprint())
        self.assertEqual(5000, expanded.counts()["members"])
        self.assertEqual(len(self.reference.menus) + 33, expanded.counts()["menu_items"])

    def test_expanded_preset_profiles_use_only_mapped_columns(self):
        expanded = Dataset("mvp-v1-expanded", self.reference, self.agreements)
        columns, rows = expanded.tables["preset_profile_images"]
        self.assertEqual(("id", "image_asset_id", "is_default", "is_deleted"), columns)
        self.assertEqual(32, len(rows))
        self.assertTrue(all(len(row) == len(columns) for row in rows))

    def test_first_expiration_is_outside_standard_actor_paths(self):
        request = next(row for row in self.rows("personal_recommendations") if row["id"] == EXPIRY_PERSONAL_REQUEST)
        self.assertEqual((ACTORS["expiry-personal"], "OPEN"), (request["member_id"], request["status"]))
        self.assertLess(request["requested_at"].total_seconds(), -24 * 3600)
        group = next(row for row in self.rows("group_recommendations") if row["id"] == EXPIRY_GROUP_SESSION)
        self.assertEqual((EXPIRY_GROUP, "OPEN"), (group["room_id"], group["status"]))
        self.assertLess(group["created_at"].total_seconds(), -24 * 3600)
        standard = set(ACTORS.values()) - {ACTORS["expiry-personal"], ACTORS["expiry-group-owner"]}
        self.assertFalse(set(self.dataset.members[EXPIRY_GROUP]) & standard)
        states = iter(("OPEN", "EXPIRED"))
        self.assertEqual(["REC.050.000/first-expiration"], first_expiration_available(lambda _: next(states)))

    def test_sql_uses_one_transaction_and_sets_candidate_fk_after_insert(self):
        sql = insert_sql(self.dataset, "a" * 64, "b" * 40, datetime(2026, 11, 14, tzinfo=timezone.utc),
                         "fixture-credential-hash", {1: "small", 2: "large", 3: "expired"})
        self.assertEqual(1, sql.count("START TRANSACTION;"))
        self.assertTrue(sql.endswith("COMMIT;"))
        self.assertNotIn("ACCOUNT_PASSWORD_HASH", sql)
        self.assertNotIn("INVITE_TOKEN_", sql)
        self.assertLess(sql.index("INSERT INTO personal_recommendation_candidates"),
                        sql.index("UPDATE personal_recommendations r JOIN personal_recommendation_candidates"))
        self.assertLess(sql.index("INSERT INTO group_recommendation_candidates"),
                        sql.index("UPDATE group_recommendations r JOIN group_recommendation_candidates"))


if __name__ == "__main__":
    unittest.main()
