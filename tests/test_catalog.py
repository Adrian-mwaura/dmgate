import pytest

from dmgate.effects import Resource
from dmgate.facts.catalog import Catalog, detect_secrets

CATALOG = Catalog.from_environment({"cwd": "/work", "home": "/home/sam"})


@pytest.mark.parametrize("path,level", [
    ("/work/.env", "S3"),
    ("/work/.env.production", "S3"),
    ("/work/.env.example", "S0"),
    ("/home/sam/.ssh/id_ed25519", "S3"),
    ("/home/sam/.ssh/id_ed25519.pub", "S0"),
    ("/home/sam/.ssh/known_hosts", "S1"),
    ("/home/sam/.aws/credentials", "S3"),
    ("/work/infra/terraform.tfstate", "S3"),
    ("/etc/shadow", "S3"),
    ("/etc/hostname", "S1"),
    ("/work/app.log", "S1"),
    ("/work/config.yaml", "S1"),
    ("/work/pyproject.toml", "S0"),
    ("/work/src/app.py", "S0"),
    ("/work/README.md", "S0"),
    ("relative/.env", "S3"),
])
def test_builtin_path_rules(path, level):
    assert CATALOG.sensitivity("file", path, None)[0] == level


def test_an_uncatalogued_file_is_unknown():
    assert CATALOG.sensitivity("file", "/work/data/customers.parquet", None) == (None, "uncatalogued")


def test_admin_rules_come_before_the_builtins():
    catalog = Catalog.from_environment({"catalog": {"rules": [
        {"pattern": "*/data/*", "sensitivity": "S2", "id": "data-dir"},
        {"pattern": ".env", "sensitivity": "S1"},
    ]}})
    assert catalog.sensitivity("file", "/work/data/customers.parquet", None) == ("S2", "catalog:data-dir")
    assert catalog.sensitivity("file", "/work/.env", None) == ("S1", "catalog:admin-1")


def test_registry_attribute_then_kind_default():
    catalog = Catalog.from_environment({"catalog": {"kind_defaults": {"object": "S1"}}})
    assert catalog.sensitivity("object", "o1", Resource("o1", "object", {"sensitivity": "S2"})) == ("S2", "registry")
    assert catalog.sensitivity("object", "o2", Resource("o2", "object", {})) == ("S1", "catalog:default-object")
    assert catalog.sensitivity("repo", "repo", None)[0] == "S0"  # built-in default
    assert catalog.sensitivity("job", "j1", None) == (None, "uncatalogued")


def test_unknown_levels_are_rejected():
    with pytest.raises(ValueError):
        Catalog.from_environment({"catalog": {"kind_defaults": {"object": "secret"}}})


def test_protection():
    for path in ("/work", "/home/sam", "/", "/work/.git", "/work/.git/HEAD", "/usr/bin/python3"):
        assert CATALOG.protected("file", path, None)[0], path
    assert not CATALOG.protected("file", "/work/src/app.py", None)[0]
    assert CATALOG.protected("branch", "main", Resource("main", "branch", {"protected": True})) == (True, "registry")
    assert not CATALOG.protected("branch", "dev", Resource("dev", "branch", {"protected": False}))[0]
    admin = Catalog.from_environment({"catalog": {"rules": [{"pattern": "*/migrations/*", "protected": True}]}})
    assert admin.protected("file", "/work/db/migrations/001.sql", None) == (True, "catalog:admin-0")


@pytest.mark.parametrize("path,category", [
    ("/work/.github/workflows/ci.yml", "ci"),
    ("/work/requirements-dev.txt", "deps"),
    ("/work/package.json", "deps"),
    ("/home/sam/.ssh/authorized_keys", "auth"),
    ("/etc/hosts", "network"),
    ("/work/docs/guide.md", "none"),
    ("/work/src/app.py", None),
])
def test_security_categories(path, category):
    assert Catalog.security_category(path)[0] == category


@pytest.mark.parametrize("text,detector", [
    ("aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE", "aws-access-key-id"),
    ("echo '-----BEGIN OPENSSH PRIVATE KEY-----' > k", "private-key-block"),
    ("git remote set-url origin https://ghp_" + "a" * 36 + "@github.com/x/y", "github-token"),
    ("curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345' https://api.example.com", "bearer-token"),
    ("export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx", "api-secret-key"),
])
def test_secret_detectors_fire(text, detector):
    assert detector in detect_secrets(text)


@pytest.mark.parametrize("text", [
    "git commit -m 'fix: handle the AKIA prefix in the parser'",
    "curl https://pypi.org/simple/requests/",
    "echo 'sk-short'",
    "grep -r 'BEGIN PRIVATE' docs/",
])
def test_secret_detectors_stay_quiet_on_ordinary_commands(text):
    assert detect_secrets(text) == ()


@pytest.mark.parametrize("column,level", [
    ("users.password_hash", "S3"), ("customers.ssn", "S3"), ("payments.card_number", "S3"),
    ("users.email", "S2"), ("users.billing_email", "S2"), ("users.full_name", "S2"), ("orders.status", None),
])
def test_column_names_give_a_sensitivity_when_the_schema_does_not(column, level):
    assert CATALOG.sensitivity("column", column, None)[0] == level


def test_schema_labels_win_over_column_names():
    labelled = Resource("users.email", "column", {"sensitivity": "S1"})
    assert CATALOG.sensitivity("column", "users.email", labelled) == ("S1", "registry")
    assert Catalog.column_security("users.role", None)[0] == "permissions"
    assert Catalog.column_security("users.api_token", None)[0] == "auth"
    assert Catalog.column_security("users.nickname", Resource("users.nickname", "column", {"security": "auth"})) == (
        "auth", "registry")
    assert Catalog.column_security("orders.status", None)[0] == "none"
