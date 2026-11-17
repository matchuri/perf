"""Provision fixture rows in the local, dedicated Matchuri performance database."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from dataset import (
    ACTORS, EXPIRY_GROUP_SESSION, EXPIRY_PERSONAL_REQUEST, FUTURE, PRESETS, SEED,
    Dataset, Reference, login_id,
)


PERF = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND = PERF.parent / "backend"
TARGET_COMPOSE = PERF / "docker-compose.target.yml"
CATALOG = PERF / "k6" / "catalog.json"
ENVIRONMENT = "matchuri-perf"
DATABASE = "matchuri_perf"
REFERENCE_TABLES = {
    "attribute_categories", "ingredients", "menu_items", "menu_attribute_categories",
    "menu_ingredients", "image_assets", "preset_profile_images", "perf_environment", "perf_fixture_manifest",
}
MYSQL_COMMAND = (
    'export MYSQL_PWD="$(cat /run/secrets/mysql-app)"; '
    'exec mysql --protocol=socket --user=matchuri_perf --database=matchuri_perf '
    '--batch --raw --skip-column-names --default-character-set=utf8mb4'
)
MANIFEST_SQL = """
CREATE TABLE IF NOT EXISTS perf_fixture_manifest (
    id INT PRIMARY KEY,
    dataset VARCHAR(32) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    backend_sha VARCHAR(64) NOT NULL,
    anchor DATETIME NOT NULL
) ENGINE=InnoDB;
"""


class SeedError(Exception):
    pass


def run(args, *, sql=None, label, timeout=45):
    result = subprocess.run(args, input=sql, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        # The client can echo SQL text from failed statements. Only expose its code and line.
        mysql_error = re.search(r"^ERROR (\d+) \(([A-Z0-9]+)\)(?: at line (\d+))?", result.stderr, re.MULTILINE)
        if mysql_error and sql is not None:
            code, state, line = mysql_error.groups()
            raise SeedError(f"{label} failed (MySQL {code}, SQLSTATE {state}, input line {line or '?'}).")
        raise SeedError(f"{label} failed (exit code {result.returncode}).")
    return result.stdout.strip()


def environment(container):
    return dict(value.split("=", 1) for value in container["Config"]["Env"] if "=" in value)


def compose_container(compose, project, service):
    ids = run([*compose, "ps", "-q", service], label=f"Find target {service}").splitlines()
    if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
        raise SeedError(f"Expected one running {service} container in the dedicated Compose project.")
    container, = json.loads(run(["docker", "inspect", ids[0]], label=f"Inspect target {service}"))
    labels = container["Config"].get("Labels", {})
    if (labels.get("com.docker.compose.project") != project or
            labels.get("com.docker.compose.service") != service or
            not container["State"]["Running"]):
        raise SeedError(f"The running {service} container does not match the target project.")
    return container


def verify_target(args, backend_sha):
    context = run(["docker", "context", "show"], label="Find Docker context")
    endpoint = run(["docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"],
                   label="Inspect Docker endpoint")
    # The generator runs on PC B, against the local daemon only.
    if any(not value.startswith(("npipe://", "unix://")) for value in (endpoint, os.getenv("DOCKER_HOST") or endpoint)):
        raise SeedError("Use the local Docker context on PC B.")

    compose = ["docker", "compose", "--file", str(TARGET_COMPOSE), "--project-name", args.project_name]
    if args.env_file:
        compose.extend(["--env-file", str(args.env_file.resolve())])
    mysql = compose_container(compose, args.project_name, "mysql")
    app = compose_container(compose, args.project_name, "app")
    mysql_env = environment(mysql)
    if (mysql_env.get("MYSQL_DATABASE") != DATABASE or mysql_env.get("MYSQL_USER") != DATABASE or
            mysql["HostConfig"].get("PortBindings") or mysql["HostConfig"].get("PublishAllPorts") or
            mysql["HostConfig"].get("NetworkMode") == "host"):
        raise SeedError("MySQL must use the private matchuri_perf schema and account with no published ports.")

    data_mounts = [mount for mount in mysql["Mounts"] if mount["Destination"] == "/var/lib/mysql"]
    if len(data_mounts) != 1 or data_mounts[0]["Type"] != "volume":
        raise SeedError("MySQL must use the named Compose volume for the dedicated dataset.")
    volume_name = data_mounts[0]["Name"]
    volume, = json.loads(run(["docker", "volume", "inspect", volume_name], label="Inspect target MySQL volume"))
    if (not volume["Name"].startswith(f"{args.project_name}_") or
            (volume.get("Labels") or {}).get("com.docker.compose.project") != args.project_name):
        raise SeedError("MySQL volume ownership does not match the dedicated Compose project.")

    app_env = environment(app)
    if (app_env.get("SPRING_PROFILES_ACTIVE") != "perf" or
            app_env.get("SPRING_DATASOURCE_URL", "").split("?", 1)[0] != f"jdbc:mysql://mysql:3306/{DATABASE}" or
            app_env.get("SPRING_DATASOURCE_USERNAME") != DATABASE or
            app["Config"]["Labels"].get("org.opencontainers.image.revision") != backend_sha):
        raise SeedError("The app must use the perf profile, the isolated MySQL database and the expected backend image revision.")

    network_name = f"{args.project_name}_target"
    if any(set(container["NetworkSettings"]["Networks"]) != {network_name} for container in (mysql, app)):
        raise SeedError("The app and MySQL must be attached only to the dedicated Compose target network.")
    network, = json.loads(run(["docker", "network", "inspect", network_name], label="Inspect dedicated network"))
    if not network["Internal"] or (network.get("Labels") or {}).get("com.docker.compose.project") != args.project_name:
        raise SeedError("The Compose target network must be internal and project-owned.")
    return mysql, app


def mysql_exec(container_id, sql, label, timeout=45):
    return run(["docker", "exec", "-i", container_id, "sh", "-ec", MYSQL_COMMAND],
               sql=sql, label=label, timeout=timeout)


def query_rows(query, sql):
    return [line.split("\t") for line in query(sql).splitlines() if line]


def load_backend_sources(backend):
    seed_dir = backend / "src/main/resources/seed"
    reference = json.loads((seed_dir / "reference-data.json").read_text(encoding="utf-8"))
    presets = json.loads((seed_dir / "preset-profile-images.json").read_text(encoding="utf-8"))["presetProfileImages"]
    version_file = backend / ("src/main/java/matchuri/backend/domain/member/support/agreement/"
                              "RequiredAgreementVersions.java")
    agreement_versions = dict(re.findall(r'\b(TERMS_OF_SERVICE|PRIVACY_POLICY)_REQUIRED_VERSION\s*=\s*"([^"\n]+)"',
                                         version_file.read_text(encoding="utf-8")))
    if set(agreement_versions) != {"TERMS_OF_SERVICE", "PRIVACY_POLICY"}:
        raise SeedError("Could not derive current agreement versions from the backend checkout.")
    return reference, presets, agreement_versions


def load_reference(query, source, presets):
    menus = {code: int(row_id) for code, row_id in query_rows(query, "SELECT code,id FROM menu_items WHERE is_active=1")}
    categories = {(kind, code): int(row_id) for kind, code, row_id in query_rows(
        query, "SELECT category_type,code,id FROM attribute_categories WHERE is_active=1")}
    ingredients = {code: int(row_id) for code, row_id in query_rows(query, "SELECT code,id FROM ingredients WHERE is_active=1")}
    assets = {key: int(asset_id) for key, asset_id in query_rows(query, """
        SELECT a.object_key,a.id FROM preset_profile_images p JOIN image_assets a ON a.id=p.image_asset_id
        WHERE p.is_deleted=0 AND a.object_key NOT LIKE 'perf/%'
    """)}
    if (set(menus) != {entry["code"] for entry in source["menuItems"]} or
            set(categories) != {(entry["categoryType"], entry["code"]) for entry in source["attributeCategories"]} or
            set(ingredients) != {entry["code"] for entry in source["ingredients"]} or
            set(assets) != {entry["objectKey"] for entry in presets}):
        raise SeedError("Database references differ from the current backend seed. Wait for app readiness or use a fresh target volume.")

    categories_by_menu = defaultdict(set)
    for menu, category in query_rows(query, "SELECT menu_id,attribute_category_id FROM menu_attribute_categories"):
        categories_by_menu[int(menu)].add(int(category))
    ingredients_by_menu = defaultdict(set)
    for menu, ingredient in query_rows(query, "SELECT menu_id,ingredient_id FROM menu_ingredients"):
        ingredients_by_menu[int(menu)].add(int(ingredient))
    expected_categories = {(menus[row["menuCode"]], categories[row["categoryType"], row["categoryCode"]])
                           for row in source["menuAttributeCategories"]}
    expected_ingredients = {(menus[row["menuCode"]], ingredients[row["ingredientCode"]])
                            for row in source["menuIngredients"]}
    if (expected_categories != {(menu, value) for menu, values in categories_by_menu.items() for value in values} or
            expected_ingredients != {(menu, value) for menu, values in ingredients_by_menu.items() for value in values}):
        raise SeedError("Database reference mappings differ from the current backend seed.")
    source_digest = sha256(json.dumps([source, presets], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return Reference(menus, categories, ingredients, categories_by_menu, ingredients_by_menu,
                     tuple(sorted(assets.values())), source_digest)


def sql_literal(value, anchor, password_hash, tokens):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, timedelta):
        value = anchor + value
    if isinstance(value, datetime):
        value = value.strftime("%Y-%m-%d %H:%M:%S")
    if value == "ACCOUNT_PASSWORD_HASH":
        value = password_hash
    elif value.startswith("INVITE_TOKEN_"):
        value = tokens[int(value.removeprefix("INVITE_TOKEN_"))]
    return "'" + value.replace("'", "''") + "'"


def insert_sql(dataset, fingerprint, backend_sha, anchor, password_hash, tokens):
    def row_sql(values):
        return "(" + ",".join(sql_literal(value, anchor, password_hash, tokens) for value in values) + ")"

    statements = [
        "SET SESSION time_zone = '+00:00';", "SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_BACKSLASH_ESCAPES');",
        "START TRANSACTION;",
        "INSERT INTO perf_fixture_manifest (id,dataset,fingerprint,backend_sha,anchor) VALUES "
        + row_sql((1, dataset.name, fingerprint, backend_sha, anchor)) + ";",
    ]
    for table, (columns, rows) in dataset.tables.items():
        for index in range(0, len(rows), 400):
            values = ",\n".join(row_sql(row) for row in rows[index:index + 400])
            statements.append(f"INSERT INTO {table} ({','.join(columns)}) VALUES\n{values};")

    for table, selection in (
        ("personal_recommendations", "SELECTED"), ("group_recommendations", "FINALIZED"),
    ):
        candidate_table = table.removesuffix("s") + "_candidates"
        foreign_key = table.removesuffix("s") + "_id"
        statements.append(f"""
            UPDATE {table} r JOIN {candidate_table} c ON c.{foreign_key}=r.id AND c.rank_no=1
            SET r.selected_candidate_id=c.id WHERE r.status='{selection}';
        """)
    statements.append("COMMIT;")
    return "\n".join(statements)


def check_counts(query, expected):
    actual = dict(query_rows(query, "\nUNION ALL\n".join(
        f"SELECT '{table}', COUNT(*) FROM {table}" for table in expected
    )))
    for table, count in expected.items():
        if actual.get(table) != str(count):
            raise SeedError(f"Unexpected row count in {table}. Use a fresh dedicated volume.")


def require_empty_application(query, baseline_counts):
    schema_tables = query_rows(query, "SELECT TABLE_NAME,ENGINE FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE()")
    for table, engine in schema_tables:
        if not re.fullmatch(r"[a-z_]+", table) or engine.lower() != "innodb":
            raise SeedError("Unexpected table or storage engine in target database.")
        if table not in REFERENCE_TABLES and query(f"SELECT 1 FROM {table} LIMIT 1;"):
            raise SeedError(f"Target table {table} already contains data. Use a fresh dedicated volume.")
    check_counts(query, baseline_counts)


def utc_iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def mysql_utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def publish_json(path, content, mode=0o600):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(content, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
    if os.name != "nt":
        temporary.chmod(mode)
    temporary.replace(path)


def validate_catalog(cases):
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    expected = {case["fixture"] for operation in catalog["operations"] for case in operation["cases"] if "fixture" in case}
    missing = expected - cases.keys()
    if missing:
        raise SeedError(f"Missing k6 fixture cases: {', '.join(sorted(missing))}")


def first_expiration_available(query):
    available = []
    for case, table, identifier, time_column in (
        ("REC.050.000/first-expiration", "personal_recommendations", EXPIRY_PERSONAL_REQUEST, "requested_at"),
        ("GREC.050.000/first-expiration", "group_recommendations", EXPIRY_GROUP_SESSION, "created_at"),
    ):
        status = query(f"SELECT status FROM {table} WHERE id={identifier} "
                       f"AND {time_column} < UTC_TIMESTAMP() - INTERVAL 24 HOUR;")
        if status == "OPEN":
            available.append(case)
        elif status != "EXPIRED":
            raise SeedError("A first-expiration fixture has an unexpected state. Use a fresh dedicated volume.")
    return available


def runtime_metadata(mysql, app):
    metadata = {
        "app_memory_bytes": int(app["HostConfig"]["Memory"]),
        "mysql_memory_bytes": int(mysql["HostConfig"]["Memory"]),
    }
    for option in mysql["Config"]["Cmd"]:
        if re.fullmatch(r"--innodb-buffer-pool-size=\d+[KMG]?", option, flags=re.IGNORECASE):
            metadata["mysql_innodb_buffer_pool_size"] = option.split("=", 1)[1]
    java_options = environment(app).get("JAVA_TOOL_OPTIONS", "")
    safe_options = [option for option in java_options.split() if re.fullmatch(
        r"-XX:(?:Initial|Max)RAMPercentage=\d+(?:\.\d+)?|-Xm[sx]\d+[kKmMgG]?|-Duser\.timezone=UTC", option
    )]
    if safe_options:
        metadata["java_memory_options"] = safe_options
    return metadata


def seed(args):
    if not re.fullmatch(r"matchuri-perf-b(?:-[a-z0-9][a-z0-9-]{0,39})?", args.project_name):
        raise SeedError("Use matchuri-perf-b or matchuri-perf-b-<suffix> as the dedicated Compose project name.")
    password = os.getenv("MATCHURI_PERF_ACCOUNT_PASSWORD", "").encode("utf-8")
    if not 16 <= len(password) <= 72:
        raise SeedError("Set MATCHURI_PERF_ACCOUNT_PASSWORD to an isolated 16–72 byte fixture password.")
    try:
        import bcrypt
    except ImportError as error:
        raise SeedError("Install the seed dependency with python -m pip install -r fixtures/requirements.txt.") from error

    backend = args.backend.resolve()
    backend_sha = run(["git", "-C", str(backend), "rev-parse", "HEAD"], label="Resolve backend revision")
    if run(["git", "-C", str(backend), "status", "--porcelain"], label="Verify backend checkout"):
        raise SeedError("The backend checkout must be clean so the Git revision identifies its seed and image.")
    source, presets, agreement_versions = load_backend_sources(backend)
    mysql, app = verify_target(args, backend_sha)

    def query(sql):
        return mysql_exec(mysql["Id"], sql + "\n", "Check dedicated MySQL fixtures")

    if query("SELECT CONCAT(DATABASE(),':',environment) FROM perf_environment WHERE id=1;") != f"{DATABASE}:{ENVIRONMENT}":
        raise SeedError("Required dedicated MySQL identity marker is absent or unexpected.")

    reference = load_reference(query, source, presets)
    dataset = Dataset(args.dataset, reference, agreement_versions)
    fingerprint = dataset.fingerprint()
    query(MANIFEST_SQL)
    rows = query_rows(query, "SELECT dataset,fingerprint,backend_sha,anchor FROM perf_fixture_manifest WHERE id=1;")

    if rows:
        (stored_dataset, stored_fingerprint, stored_sha, stored_anchor), = rows
        if (stored_dataset, stored_fingerprint, stored_sha) != (dataset.name, fingerprint, backend_sha):
            raise SeedError("Existing dataset identity differs. Use a fresh Compose project and volume.")
        anchor = mysql_utc(stored_anchor)
        now = mysql_utc(query("SELECT UTC_TIMESTAMP();"))
        if anchor + FUTURE <= now + timedelta(minutes=5):
            raise SeedError("Existing active fixtures are too old. Use a fresh Compose project and volume.")
        hash_value = query(f"SELECT password_hash FROM members WHERE id={ACTORS['typical']};")
        if not hash_value.startswith(("$2a$", "$2b$", "$2y$")) or not bcrypt.checkpw(password, hash_value.encode("ascii")):
            raise SeedError("The fixture account password differs from the existing target dataset.")
        tokens = {int(row_id): value for row_id, value in query_rows(query, "SELECT id,token FROM group_invite_links;")}
        if set(tokens) != {1, 2, 3}:
            raise SeedError("Existing invite-link fixture differs. Use a fresh Compose project and volume.")
        cases = dataset.cases(tokens)
        validate_catalog(cases)
    else:
        require_empty_application(query, {
            "menu_items": len(reference.menus), "attribute_categories": len(reference.categories),
            "ingredients": len(reference.ingredients), "image_assets": len(set(reference.preset_asset_ids)),
            "preset_profile_images": len(reference.preset_asset_ids),
        })
        anchor = mysql_utc(query("SELECT UTC_TIMESTAMP();"))
        tokens = {index: str(uuid4()) for index in range(1, 4)}
        cases = dataset.cases(tokens)
        validate_catalog(cases)
        password_hash = bcrypt.hashpw(password, bcrypt.gensalt(rounds=10)).decode("ascii")
        sql = insert_sql(dataset, fingerprint, backend_sha, anchor, password_hash, tokens)
        mysql_exec(mysql["Id"], sql, "Insert dedicated MySQL fixtures", timeout=300)

    counts = dataset.counts()
    check_counts(query, counts)
    for check in (
        "SELECT COUNT(*) FROM personal_recommendations WHERE member_id=3 AND status='SELECTED'",
        "SELECT COUNT(*) FROM group_room_members m JOIN group_rooms r ON m.room_id=r.id "
        "WHERE m.member_id=8 AND m.status='ACTIVE' AND r.status='ACTIVE'",
        "SELECT COUNT(*) FROM group_invites WHERE target_member_id=9 AND status='PENDING' AND expires_at>UTC_TIMESTAMP()",
        "SELECT COUNT(*) FROM group_recommendations WHERE room_id=2",
    ):
        if int(query(check)) < 300:
            raise SeedError("A deep-page fixture has fewer than 300 qualifying rows. Use a fresh dedicated volume.")
    identity = {
        "environment": ENVIRONMENT, "dataset": dataset.name, "fingerprint": fingerprint,
        "backend_sha": backend_sha, "anchor": utc_iso(anchor), "valid_until": utc_iso(anchor + FUTURE),
        "app_image_id": app["Image"], "mysql_image_id": mysql["Image"],
        "seed": SEED, "counts": counts, "first_expiration_available": first_expiration_available(query),
        "runtime": runtime_metadata(mysql, app),
    }
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    publish_json(output_dir / "fixtures.json", {
        **identity, "users": {alias: {"loginId": login_id(member)} for alias, member in ACTORS.items()}, "cases": cases,
    })
    identity_dir = output_dir / "identity"
    identity_dir.mkdir(exist_ok=True)
    if os.name != "nt":
        identity_dir.chmod(0o755)
    publish_json(identity_dir / "identity.json", identity, 0o644)
    print(f"Prepared {dataset.name} ({fingerprint[:12]}) and validated fixture counts.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", default="matchuri-perf-b")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dataset", choices=PRESETS, default="mvp-v1")
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND)
    parser.add_argument("--output-dir", type=Path, default=PERF / "runtime" / "b")
    try:
        seed(parser.parse_args())
    except SeedError as error:
        parser.exit(1, f"{error}\n")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        parser.exit(1, "Fixture preparation failed. Check the dedicated Compose services, backend checkout and output path.\n")


if __name__ == "__main__":
    main()
