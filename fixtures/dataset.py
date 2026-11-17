"""Deterministic rows and k6 case inputs for the isolated MySQL dataset."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
from random import Random


SEED = 102993
PRESETS = {
    "smoke-v1": (400, 320, 800, 900, 800, 0),
    "mvp-v1": (5000, 500, 30000, 10000, 5000, 0),
    "mvp-v1-expanded": (5000, 500, 30000, 10000, 5000, 32),
}
ACTORS = {
    "empty": 1,
    "typical": 2,
    "heavy": 3,
    "no-consent": 4,
    "admin": 5,
    "small-group-owner": 6,
    "large-group-owner": 7,
    "deep-groups": 8,
    "deep-invites": 9,
    "expiry-personal": 10,
    "expiry-group-owner": 11,
}
SMALL_GROUP = 1
LARGE_GROUP = 2
PREVIEW_GROUP = 3
PREPARING_GROUP = 4
EXPIRY_GROUP = 305
EXPIRY_PERSONAL_REQUEST = 21
EXPIRY_GROUP_SESSION = 345
FRESH = timedelta(minutes=-5)
HISTORICAL = timedelta(days=-30)
FUTURE = timedelta(hours=23, minutes=55)
IMAGE_ID_START = 10001
PROFILE_ID_START = 10001


def login_id(member):
    return f"perf{member:05d}"


def nickname(member):
    return f"perf-member-{member:05d}"


@dataclass(frozen=True)
class Reference:
    menus: dict
    categories: dict
    ingredients: dict
    menu_categories: dict
    menu_ingredients: dict
    preset_asset_ids: tuple
    source_digest: str


class Dataset:
    def __init__(self, name, reference, agreement_versions):
        self.name = name
        self.reference = reference
        self.agreement_versions = agreement_versions
        self.random = Random(SEED)
        self.tables = {}
        self.choices = {}
        self.members = {}
        self.member_count, self.group_count, personal, group, invites, self.extra = PRESETS[name]
        self.menu_ids = sorted(reference.menus.values())
        self.menu_density = sorted(self.menu_ids, key=self.menu_density_key)
        self.location = (37.55, 126.98, 750, "Matchuri perf location")

        self.seed_members()
        self.seed_groups()
        self.seed_invites(invites)
        self.seed_personal_recommendations(personal)
        self.seed_group_recommendations(group)
        self.seed_images_and_extra_reference()

    def add(self, table, columns, values, at=HISTORICAL):
        columns = tuple(columns.split())
        if table != "preset_profile_images":
            columns += ("created_at", "updated_at")
            values += (at, at)
        row_columns, rows = self.tables.setdefault(table, (columns, []))
        if row_columns != columns or len(columns) != len(values):
            raise ValueError(f"Inconsistent fixture columns for {table}")
        rows.append(values)

    def menu_density_key(self, menu):
        return (len(self.reference.menu_categories[menu]) + len(self.reference.menu_ingredients[menu]), menu)

    def seed_members(self):
        for member in range(1, self.member_count + 1):
            self.add("members", "id login_id password_hash nickname nickname_completed email is_social member_role status", (
                member, login_id(member), "ACCOUNT_PASSWORD_HASH", nickname(member), True,
                f"{login_id(member)}@matchuri-perf.invalid", False, "ADMIN" if member == ACTORS["admin"] else "MEMBER", "ACTIVE",
            ))
        for member in range(1, self.member_count + 1):
            for agreement, version in self.agreement_versions.items():
                if member != ACTORS["no-consent"] or agreement == "TERMS_OF_SERVICE":
                    self.add("member_agreements", "member_id agreement_type agreement_version agreed_at", (
                        member, agreement, version, HISTORICAL,
                    ))

        profile_members = [ACTORS["typical"], ACTORS["heavy"], *range(11, self.member_count + 1, 3)]
        category_ids = sorted(self.reference.categories.values())
        ingredient_ids = sorted(self.reference.ingredients.values())
        for member in profile_members:
            self.add("member_taste_profiles", "id member_id profile_version", (member, member, "v1"))
        for member in profile_members:
            preferences = self.random.sample(category_ids, 5)
            restrictions = self.random.sample(ingredient_ids, 2)
            dislikes = self.random.sample(self.menu_ids, 3)
            for category in preferences:
                self.add("member_taste_profile_categories", "profile_id attribute_category_id", (member, category))
            for ingredient in restrictions:
                self.add("member_taste_profile_restriction_ingredients", "profile_id ingredient_id", (member, ingredient))
            for menu in dislikes:
                self.add("member_taste_profile_disliked_menu_items", "profile_id menu_id", (member, menu))
            if member == ACTORS["typical"] or member % 2 == 0:
                self.add("member_locations", "member_id latitude longitude radius_meters address", (member, *self.location))

    def seed_groups(self):
        edge_owners = (6, 7, 7, 7)
        for group in range(1, self.group_count + 1):
            owner = edge_owners[group - 1] if group <= len(edge_owners) else 12 + group % (self.member_count - 11)
            if group == EXPIRY_GROUP:
                owner = ACTORS["expiry-group-owner"]
            status = "CLOSED" if group > 304 and group % 17 == 0 else "ACTIVE"
            self.add("group_rooms", "id name invite_code host_member_id status", (
                group, f"Matchuri perf group {group}", f"perfgroup{group:06d}", owner, status,
            ))
            if group % 3 == 2:
                self.add("group_locations", "group_room_id latitude longitude radius_meters address", (group, *self.location))
            active = [owner]
            if group in (SMALL_GROUP, LARGE_GROUP):
                active += [ACTORS["typical"] if group == SMALL_GROUP else ACTORS["heavy"]]
            if 5 <= group <= 12:
                active.append(ACTORS["typical"])
            if group <= 300:
                active.append(ACTORS["deep-groups"])
            total = 25 if group in (LARGE_GROUP, PREVIEW_GROUP, PREPARING_GROUP) else 3 + group % 4
            pool = [member for member in range(12, self.member_count + 1) if member != owner]
            active += self.random.sample(pool, total - len(active))
            self.members[group] = active
            for member in active:
                self.add("group_room_members", "room_id member_id role status joined_at", (
                    group, member, "OWNER" if member == owner else "MEMBER", "ACTIVE", HISTORICAL,
                ))

    def seed_invites(self, count):
        links = ((SMALL_GROUP, FUTURE), (PREVIEW_GROUP, FUTURE), (SMALL_GROUP, HISTORICAL))
        for link_id, (group, expiry) in enumerate(links, start=1):
            created = FRESH if expiry == FUTURE else expiry - timedelta(days=1)
            self.add("group_invite_links", "id room_id token expires_at", (link_id, group, f"INVITE_TOKEN_{link_id}", expiry), at=created)

        for index in range(count):
            group = 1 + index % self.group_count
            owner = self.members[group][0]
            if index < 300:
                target, status, expires = ACTORS["deep-invites"], "PENDING", FUTURE
            elif index < 308:
                group = index - 295
                owner = self.members[group][0]
                target, status, expires = ACTORS["typical"], "ACCEPTED", FUTURE
            else:
                group_members = set(self.members[group])
                target = self.random.randrange(10, self.member_count + 1)
                while target in group_members:
                    target = self.random.randrange(10, self.member_count + 1)
                status = ("ACCEPTED", "DECLINED", "EXPIRED", "REVOKED")[index % 4]
                expires = HISTORICAL if status == "EXPIRED" else FUTURE
                if status == "ACCEPTED":
                    target = self.members[group][-1]
            responded = FRESH + timedelta(minutes=1) if status in ("ACCEPTED", "DECLINED") else None
            created = FRESH if expires == FUTURE else HISTORICAL - timedelta(days=1)
            self.add("group_invites", "room_id request_member_id target_member_id status expires_at responded_at", (
                group, owner, target, status, expires, responded,
            ), at=created)

    def seed_personal_recommendations(self, count):
        candidates = []
        selected = []
        for request in range(1, count + 1):
            if request <= 20:
                member, status = ACTORS["typical"], "OPEN" if request == 1 else "SELECTED"
            elif request == EXPIRY_PERSONAL_REQUEST:
                member, status = ACTORS["expiry-personal"], "OPEN"
            elif request <= 341:
                member, status = ACTORS["heavy"], "SELECTED"
            else:
                member = self.random.randrange(12, self.member_count + 1)
                status = ("SELECTED", "SELECTED", "SELECTED", "EXPIRED", "REROLLED_WITH_SKIP")[request % 5]
            at = FRESH if request == 1 else HISTORICAL - timedelta(minutes=request)
            closed = None if status == "OPEN" else at + timedelta(hours=24 if status == "EXPIRED" else 1)
            self.add("personal_recommendations", "id member_id status requested_at closed_at context_json", (
                request, member, status, at, closed, '{"mealTime":"LUNCH"}',
            ), at=at)
            number = 1 if request == 1 else 3
            menus = self.random.sample(self.menu_ids, number)
            for rank, menu in enumerate(menus, start=1):
                candidate_id = (request - 1) * 3 + rank
                candidates.append((candidate_id, request, menu, rank, at))
            if status == "SELECTED":
                selected.append((request, (request - 1) * 3 + 1))
        for candidate_id, request, menu, rank, at in candidates:
            self.add("personal_recommendation_candidates", "id personal_recommendation_id menu_id rank_no score", (
                candidate_id, request, menu, rank, 100 - rank * 4,
            ), at=at)
        self.choices["personal_recommendations"] = selected

    def seed_group_recommendations(self, count):
        candidates = []
        selected = []
        categories = []
        for session in range(1, count + 1):
            if session <= 3:
                group, status = (SMALL_GROUP, LARGE_GROUP, PREPARING_GROUP)[session - 1], ("OPEN", "OPEN", "PREPARING")[session - 1]
            elif session <= 24:
                group, status = SMALL_GROUP, "FINALIZED"
            elif session <= 344:
                group, status = LARGE_GROUP, "FINALIZED"
            elif session == EXPIRY_GROUP_SESSION:
                group, status = EXPIRY_GROUP, "OPEN"
            else:
                group = 1 + self.random.randrange(self.group_count)
                status = ("FINALIZED", "FINALIZED", "FINALIZED", "EXPIRED", "REROLLED_WITH_SKIP")[session % 5]
            active = status in ("OPEN", "PREPARING")
            at = FRESH if session <= 3 else HISTORICAL - timedelta(minutes=session)
            started = None if status == "PREPARING" else at + timedelta(minutes=1)
            ended = None if active else at + timedelta(hours=24 if status == "EXPIRED" else 1)
            self.add("group_recommendations", "id room_id status started_at ended_at context_json", (
                session, group, status, started, ended, '{"mealTime":"LUNCH"}',
            ), at=at)
            if started is None:
                continue
            menus = self.random.sample(self.menu_ids, 3)
            for rank, menu in enumerate(menus, start=1):
                candidate_id = (session - 1) * 3 + rank
                candidates.append((candidate_id, session, menu, rank, at))
            for rank, category in enumerate(sorted(self.reference.menu_categories[menus[0]])[:3], start=1):
                categories.append((session, category, rank, at))
            if status == "FINALIZED":
                selected.append((session, (session - 1) * 3 + 1))

        for candidate_id, session, menu, rank, at in candidates:
            self.add("group_recommendation_candidates", "id group_recommendation_id menu_id rank_no score", (
                candidate_id, session, menu, rank, 100 - rank * 4,
            ), at=at)
        self.choices["group_recommendations"] = selected
        for session, category, rank, at in categories:
            self.add("group_recommendation_categories", "group_recommendation_id attribute_category_id rank_no source", (
                session, category, rank, "MENU",
            ), at=at)
        for session, group, total in ((1, SMALL_GROUP, 1), (2, LARGE_GROUP, 20), (3, PREPARING_GROUP, 14)):
            for index, member in enumerate(self.members[group][:total]):
                self.add("group_recommendation_readiness", "group_recommendation_id member_id status", (
                    session, member, "CANCELED" if index == 2 and session == 3 else "READY",
                ), at=FRESH)
                if session == 2:
                    self.add("group_recommendation_votes", "group_recommendation_id candidate_id member_id", (
                        session, (session - 1) * 3 + 1 + index % 3, member,
                    ), at=FRESH)

    def seed_images_and_extra_reference(self):
        # The sparse menu stays unlinked and the densest 33 have synthetic metadata.
        linked = list(reversed(self.menu_density[-33:]))
        total_presets = len(self.reference.preset_asset_ids)
        for offset in range(len(linked) + self.extra):
            image = IMAGE_ID_START + offset
            object_key = f"perf/image-{offset}.png"
            self.add("image_assets", "id storage_provider bucket object_key content_type content_length checksum width height status", (
                image, "CLOUDFLARE_R2", "matchuri-perf", object_key, "image/png", 1024,
                sha256(object_key.encode()).hexdigest(), 300, 300, "ACTIVE",
            ))
        for offset, menu in enumerate(linked):
            self.add("menu_item_images", "menu_id image_asset_id image_role sort_order is_primary", (
                menu, IMAGE_ID_START + offset, "PRIMARY", 0, True,
            ))
        for offset in range(self.extra):
            self.add("preset_profile_images", "id image_asset_id is_default is_deleted", (
                PROFILE_ID_START + offset, IMAGE_ID_START + len(linked) + offset, False, False,
            ))
        for member in [ACTORS["typical"], ACTORS["heavy"], *range(10, self.member_count + 1, 3)]:
            self.add("member_profile_images", "member_id image_asset_id", (
                member, self.reference.preset_asset_ids[member % total_presets],
            ))

        self.inactive_menu = max(self.menu_ids) + 1000
        for index in range(self.extra + 1):
            self.add("menu_items", "id code name description is_active", (
                self.inactive_menu + index, f"PERF_INACTIVE_MENU_{index}", f"Perf archived menu {index}",
                "Synthetic archived menu for administrator queries", False,
            ))
        for index in range(self.extra):
            self.add("attribute_categories", "category_type code name sort_order is_active", (
                "FLAVOR", f"PERF_ARCHIVED_{index}", f"Perf archived category {index}", 1000 + index, False,
            ))
            self.add("ingredients", "code name is_allergen sort_order is_active", (
                f"PERF_ARCHIVED_{index}", f"Perf archived ingredient {index}", False, 1000 + index, False,
            ))

    def fingerprint(self):
        def relative_time(value):
            return value.total_seconds() if isinstance(value, timedelta) else value

        checksum = sha256()
        checksum.update(json.dumps([self.name, SEED, self.reference.source_digest], separators=(",", ":")).encode())
        for table, (columns, rows) in self.tables.items():
            checksum.update(json.dumps([table, columns], separators=(",", ":")).encode())
            for row in rows:
                checksum.update(json.dumps([relative_time(value) for value in row], separators=(",", ":")).encode())
        checksum.update(json.dumps(self.choices, sort_keys=True, separators=(",", ":")).encode())
        return checksum.hexdigest()

    def counts(self):
        rows = {table: len(records) for table, (_, records) in self.tables.items()}
        for table, baseline in (
            ("menu_items", len(self.menu_ids)),
            ("attribute_categories", len(self.reference.categories)),
            ("ingredients", len(self.reference.ingredients)),
            ("preset_profile_images", len(self.reference.preset_asset_ids)),
            ("image_assets", len(set(self.reference.preset_asset_ids))),
        ):
            rows[table] = rows.get(table, 0) + baseline
        return rows

    def cases(self, invite_tokens):
        cases = {}

        def add(operation, case, actor=None, **fields):
            if actor:
                fields["actor"] = actor
            cases[f"{operation}/{case}"] = fields

        def group_case(operation, case, group, session=None):
            params = {"groupId": group}
            if session is not None:
                params["sessionId"] = session
            actor = "small-group-owner" if group == SMALL_GROUP else "large-group-owner"
            add(operation, case, actor, params=params)

        def list_check(length):
            return {"checks": [{"path": "", "length": length}]}

        for actor in ("empty", "typical", "heavy"):
            add("OPS.020.000", actor, actor)
        add("AUTH.030.000", "hit", params={"loginId": login_id(ACTORS["typical"])})
        add("AUTH.040.000", "hit", params={"nickname": nickname(ACTORS["typical"])})
        for operation, choices in (
            ("ONB.010.000", {"complete": "typical", "missing": "no-consent"}),
            ("ONB.030.000", {"with-image": "typical", "without-image": "empty"}),
            ("ONB.060.000", {"empty": "empty", "populated": "typical"}),
            ("ONB.090.000", {"unset": "empty", "set": "typical"}),
            ("GROUP.020.000", {"first": "typical", "deep-page": "deep-groups", "empty": "empty"}),
            ("GROUP.050.500", {"pending": "deep-invites", "none": "empty"}),
            ("RT.010.000", {"member": "typical", "parallel": "heavy"}),
        ):
            for case, actor in choices.items():
                add(operation, case, actor)
        add("ONB.120.000", "baseline", "typical", expected=list_check(len(self.reference.preset_asset_ids) + self.extra))

        dense = self.menu_density[-1]
        sparse = self.menu_density[0]
        add("REF.030.000", "combined", query={
            "attributeCategoryIds": [min(self.reference.menu_categories[dense])],
            "ingredientIds": [min(self.reference.menu_ingredients[dense])],
        })
        for case, menu in (("sparse", sparse), ("dense", dense)):
            add("REF.040.000", case, params={"menuItemId": menu})
            add("ADMIN.135.000", case, "admin", params={"menuItemId": menu})
        add("ADMIN.135.000", "inactive", "admin", params={"menuItemId": self.inactive_menu})
        # Pick restrictions that leave most menus available, plus preferences from distinct types.
        ingredient_usage = Counter(item for ingredients in self.reference.menu_ingredients.values() for item in ingredients)
        restrictions = sorted(self.reference.ingredients.values(), key=lambda item: (ingredient_usage[item], item))[:3]
        category_types = defaultdict(list)
        for (category_type, _), category_id in self.reference.categories.items():
            category_types[category_type].append(category_id)
        add("REC.010.000", "complex", body={
            "attributeCategoryIds": [min(ids) for _, ids in sorted(category_types.items())],
            "restrictionIngredientIds": restrictions,
            "dislikedMenuItemIds": self.menu_ids[:4],
            "contextJson": {"mealTime": "LUNCH", "partySize": 3},
        })
        for operation in ("REC.020.000", "REC.020.500", "GROUP.060.000", "GROUP.060.500"):
            invites = operation.startswith("GROUP")
            add(operation, "first", "deep-invites" if invites else "typical")
            add(operation, "deep-page", "deep-invites" if invites else "heavy")
            add(operation, "empty", "empty")
            if invites:
                add(operation, "accepted", "typical", query={"status": "ACCEPTED"})
        for operation in ("REC.050.000", "REC.060.000"):
            for case, request in (("open", 1), ("selected", 2)):
                add(operation, case, "typical", params={"requestId": request})
        add("REC.050.000", "first-expiration", "expiry-personal", params={"requestId": EXPIRY_PERSONAL_REQUEST})
        for operation in ("GROUP.030.000", "GROUP.030.500", "RT.020.000"):
            group_case(operation, "small", SMALL_GROUP)
            group_case(operation, "large", LARGE_GROUP)
        cases["RT.020.000/large"]["variants"] = [
            {"params": {"groupId": group}} for group in (LARGE_GROUP, PREVIEW_GROUP, PREPARING_GROUP)
        ]
        group_case("GROUP.130.000", "valid", SMALL_GROUP)
        group_case("GROUP.130.000", "absent", LARGE_GROUP)
        for index, case in enumerate(("small", "large", "expired"), start=1):
            add("GROUP.150.000", case, body={"token": invite_tokens[index]})
        group_case("GREC.020.000", "small", SMALL_GROUP, 1)
        group_case("GREC.020.000", "large", PREPARING_GROUP, 3)
        for operation in ("GREC.040.000", "GREC.040.500"):
            group_case(operation, "first", SMALL_GROUP)
            group_case(operation, "deep-page", LARGE_GROUP)
        group_case("GREC.050.000", "preparing", PREPARING_GROUP, 3)
        group_case("GREC.050.000", "open", SMALL_GROUP, 1)
        group_case("GREC.050.000", "finalized", SMALL_GROUP, 4)
        add("GREC.050.000", "first-expiration", "expiry-group-owner", params={
            "groupId": EXPIRY_GROUP, "sessionId": EXPIRY_GROUP_SESSION,
        })
        group_case("GREC.060.000", "open", SMALL_GROUP, 1)
        group_case("GREC.060.000", "with-votes", LARGE_GROUP, 2)
        group_case("GREC.060.000", "closed", SMALL_GROUP, 4)
        for operation, table in (
            ("ADMIN.010.000", "attribute_categories"), ("ADMIN.050.000", "ingredients"),
            ("ADMIN.090.000", "menu_items"), ("ADMIN.160.000", "preset_profile_images"),
        ):
            add(operation, "baseline", "admin", expected=list_check(self.counts()[table]))
        return cases
