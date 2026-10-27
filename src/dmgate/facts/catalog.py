"""Platform catalogs and detectors (DESIGN.md §5.1, §6.4).

Answers three questions about a target, deterministically:

- **Sensitivity** (S0-S3). In order: the resource's own registry attribute `sensitivity`; the
  environment's catalog rules (admin-owned); the built-in path rules below; the environment's
  default for the kind. Nothing matched means unknown (None). The LLM fact filler may classify it.
- **Protection.** The registry attribute `protected`, or a protected-path rule.
- **Security relevance** of a change to a file: auth, ci, permissions, network or deps.

Path rules are first-match, so exceptions sit above the general rules. A pattern without "/"
matches the file name; one with "/" matches the whole normalized path. Patterns use fnmatch, where
"*" also crosses "/".

The built-in rules are a starting catalog, not a policy: admins add environment rules above them.
They judge by name only. Content is checked at run time (stdout tokenization, §6.3) and, for
literals written into a command, by the secret detectors at the end of this module.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from dmgate.effects import Resource
from dmgate.facts.schema import SENSITIVITY


@dataclass(frozen=True)
class PathRule:
    pattern: str
    value: str  # a sensitivity level, or a security category
    rule_id: str

    def matches(self, path: str) -> bool:
        if "/" in self.pattern:
            full = path if path.startswith("/") else "/" + path
            return fnmatchcase(full, self.pattern)
        return fnmatchcase(posixpath.basename(path), self.pattern)


def _rules(value: str, rule_id: str, *patterns: str) -> list[PathRule]:
    return [PathRule(p, value, rule_id) for p in patterns]


_SOURCE_EXTENSIONS = (
    "py", "pyi", "ipynb", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs", "java", "kt", "scala",
    "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "m", "sh", "bash", "zsh", "ps1",
    "html", "css", "scss", "vue", "svelte", "r", "jl", "lua", "pl", "proto", "graphql",
)

BUILTIN_SENSITIVITY: tuple[PathRule, ...] = (
    # Public by convention. Listed first so the secret rules below do not catch them.
    *_rules("S0", "env-template", ".env.example", ".env.sample", ".env.template", ".env.dist"),
    *_rules("S0", "public-key", "*.pub"),
    *_rules("S1", "ssh-known-hosts", "*/.ssh/known_hosts"),
    *_rules("S0", "project-manifest", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"),
    # Secrets.
    *_rules("S3", "dotenv", ".env", ".env.*", "*.env"),
    *_rules("S3", "key-material", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
            "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*/.ssh/*", "*/.gnupg/*"),
    *_rules("S3", "credential-file", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
            "credentials", "credentials.json", "*/.aws/credentials", "*/.kube/config",
            "*/.docker/config.json", "*/.config/gcloud/*", "*/.config/gh/hosts.yml"),
    *_rules("S3", "secret-store", "secrets.*", "*/secrets/*", "*.tfstate", "*.tfstate.backup"),
    *_rules("S3", "system-secret", "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssl/private/*"),
    # Internal.
    *_rules("S1", "log", "*.log", "/var/log/*"),
    *_rules("S1", "system-config", "/etc/*"),
    *_rules("S1", "config", "*.conf", "*.cfg", "*.ini", "*.toml", "*.yaml", "*.yml", "*/.aws/config"),
    # Ordinary workspace content.
    *_rules("S0", "source", *(f"*.{ext}" for ext in _SOURCE_EXTENSIONS)),
    *_rules("S0", "docs", "*.md", "*.rst", "*.adoc", "*.txt", "LICENSE*", "README*", "CHANGELOG*"),
    *_rules("S0", "build-file", "Makefile", "Dockerfile", "*.lock", "package-lock.json", ".gitignore",
            ".gitattributes", ".editorconfig", ".dockerignore"),
)

BUILTIN_PROTECTED: tuple[PathRule, ...] = (
    *_rules("protected", "git-internals", "*/.git", "*/.git/*"),
    *_rules("protected", "system-path", "/etc", "/etc/*", "/usr", "/usr/*", "/bin", "/bin/*", "/sbin",
            "/sbin/*", "/boot", "/boot/*", "/lib", "/lib/*", "/lib64", "/lib64/*", "/proc/*", "/sys/*",
            "/dev/*", "/var/lib/*"),
)

BUILTIN_SECURITY: tuple[PathRule, ...] = (
    *_rules("auth", "auth-file", "authorized_keys", "authorized_keys2", "*/.ssh/*", "sudoers", "/etc/sudoers.d/*",
            "/etc/pam.d/*", ".htpasswd", "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/ssh/*"),
    *_rules("ci", "ci-config", "*/.github/workflows/*", "*/.github/actions/*", ".gitlab-ci.yml", "Jenkinsfile",
            "*/.circleci/*", "azure-pipelines.yml", ".travis.yml", "*/.buildkite/*", "bitbucket-pipelines.yml",
            "*/.git/hooks/*", ".pre-commit-config.yaml"),
    *_rules("network", "network-config", "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf",
            "/etc/iptables/*", "/etc/nftables.conf", "/etc/nginx/*", "/etc/systemd/resolved.conf",
            "/etc/network/*", "/etc/netplan/*"),
    *_rules("deps", "dependency-manifest", "requirements*.txt", "constraints*.txt", "pyproject.toml", "setup.py",
            "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock", "package.json",
            "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.toml", "Cargo.lock", "go.mod",
            "go.sum", "Gemfile", "Gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts",
            "composer.json", "composer.lock", "Dockerfile", ".npmrc", "pip.conf", "*/.cargo/config*"),
    *_rules("none", "docs", "*.md", "*.rst", "*.adoc", "*.txt", "LICENSE*", "CHANGELOG*"),
)

PERMISSION_COMMANDS = frozenset({"chmod", "chown", "chgrp", "setfacl", "chattr"})

# Kinds whose content is workspace metadata: refs and commit history. Environments may override.
BUILTIN_KIND_DEFAULTS = {"repo": "S0", "branch": "S0"}


def first_match(rules: tuple[PathRule, ...], path: str) -> PathRule | None:
    return next((r for r in rules if r.matches(path)), None)


class Catalog:
    """The platform's catalog for one environment: admin rules first, then the built-in rules."""

    def __init__(self, rules: tuple[PathRule, ...] = (), protected: tuple[PathRule, ...] = (),
                 kind_defaults: Mapping[str, str] | None = None, protected_paths: frozenset[str] = frozenset()):
        self.sensitivity_rules = (*rules, *BUILTIN_SENSITIVITY)
        self.protected_rules = (*protected, *BUILTIN_PROTECTED)
        self.kind_defaults = {**BUILTIN_KIND_DEFAULTS, **(kind_defaults or {})}
        self.protected_paths = protected_paths
        for level in (*self.kind_defaults.values(), *(r.value for r in rules)):
            if level not in SENSITIVITY:
                raise ValueError(f"unknown sensitivity {level!r}")

    @classmethod
    def from_environment(cls, environment: Mapping[str, Any]) -> Catalog:
        """`environment["catalog"]` = {"rules": [{"pattern", "sensitivity"?, "protected"?, "id"?}],
        "kind_defaults": {kind: level}}. The root, the home and the working directory themselves are
        always protected: deleting one of them wipes everything below."""
        spec = environment.get("catalog", {})
        rules, protected = [], []
        for i, r in enumerate(spec.get("rules", ())):
            rule_id = r.get("id", f"admin-{i}")
            if "sensitivity" in r:
                rules.append(PathRule(r["pattern"], r["sensitivity"], rule_id))
            if r.get("protected"):
                protected.append(PathRule(r["pattern"], "protected", rule_id))
        roots = {"/", *(environment[k] for k in ("home", "cwd") if environment.get(k))}
        return cls(tuple(rules), tuple(protected), spec.get("kind_defaults"), frozenset(roots))

    def sensitivity(self, kind: str, target: str | None, resource: Resource | None) -> tuple[str | None, str]:
        """(level or None, where it came from)."""
        if resource is not None and resource.attrs.get("sensitivity") in SENSITIVITY:
            return resource.attrs["sensitivity"], "registry"
        if kind == "file" and target is not None:
            rule = first_match(self.sensitivity_rules, target)
            if rule is not None:
                return rule.value, f"catalog:{rule.rule_id}"
        if kind in self.kind_defaults:
            return self.kind_defaults[kind], f"catalog:default-{kind}"
        return None, "uncatalogued"

    def protected(self, kind: str, target: str, resource: Resource | None) -> tuple[bool, str]:
        if resource is not None and "protected" in resource.attrs:
            return bool(resource.attrs["protected"]), "registry"
        if kind == "file":
            if target in self.protected_paths:
                return True, "catalog:root-directory"
            rule = first_match(self.protected_rules, target)
            if rule is not None:
                return True, f"catalog:{rule.rule_id}"
        return False, "not protected"

    @staticmethod
    def security_category(path: str) -> tuple[str | None, str]:
        """(auth / ci / network / deps / none, or None if no rule knows the file, and the rule id)."""
        rule = first_match(BUILTIN_SECURITY, path)
        if rule is None:
            return None, "no rule"
        return rule.value, f"catalog:{rule.rule_id}"


# ---------------------------------------------------------------------------------------------
# Secret detectors: high-precision patterns for credentials written literally into a command.

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("api-secret-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe-live-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer-token", re.compile(r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)


def detect_secrets(text: str) -> tuple[str, ...]:
    """Ids of the secret detectors that fire on `text`."""
    return tuple(name for name, pattern in SECRET_PATTERNS if pattern.search(text))
