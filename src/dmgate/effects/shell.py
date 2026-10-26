"""Shell effect resolver (DESIGN.md §6.2).

Parses a command with tree-sitter-bash and maps every command it could run to effects. Literal
variables and for-loops over literal lists are expanded. Anything that depends on runtime values
(command substitution, unbound variables, globs without a filesystem snapshot, stdin) becomes an
unresolved selector, so the effect is marked inexact instead of guessed. Programs we cannot see
into (interpreters, build tools, unknown binaries) yield an opaque EXEC effect; the transactional
sandbox (§6.3) is what observes those.

Resource CLIs follow their real semantics (git, scancel, kubectl, aws s3), so the resolver
over-approximates rather than under-approximates what a restricted shim would do.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from dmgate.effects.model import Effect, Resolution, Verb
from dmgate.effects.registry import Registry, Resource

_PARSER = Parser(Language(tree_sitter_bash.language()))
_MAX_DEPTH = 8  # nesting of bash -c, eval, xargs, find -exec
_MAX_LOOP_ITEMS = 256


@dataclass(frozen=True)
class Arg:
    text: str | None  # None when the value depends on runtime state
    raw: str
    glob: bool = False

    @property
    def resolved(self) -> bool:
        return self.text is not None and not self.glob


_STDIN = Arg(None, "<stdin>")


@dataclass
class _Ctx:
    registry: Registry
    cwd: str | None
    home: str | None
    user: str | None
    wrappers: list[tuple[tuple[str, ...], int]]
    bindings: dict[str, str] = field(default_factory=dict)
    effects: list[Effect] = field(default_factory=list)
    flows: list[tuple[int, int]] = field(default_factory=list)
    parse_error: bool = False
    depth: int = 0


def resolve_shell(command: str, environment: Mapping[str, Any] | None = None) -> Resolution:
    env = environment or {}
    ctx = _Ctx(
        registry=Registry.from_environment(env),
        cwd=env.get("cwd"),
        home=env.get("home"),
        user=env.get("user"),
        wrappers=[(tuple(w["argv_prefix"]), int(w.get("skip", 0))) for w in env.get("command_wrappers", ())],
    )
    _run_source(ctx, command)
    return Resolution(tuple(ctx.effects), tuple(ctx.flows), ctx.parse_error)


# ---------------------------------------------------------------------------------------------
# Tree walking


def _run_source(ctx: _Ctx, source: str) -> None:
    tree = _PARSER.parse(source.encode("utf-8"))
    if tree.root_node.has_error:
        ctx.parse_error = True
    _walk(ctx, tree.root_node)


_SEQUENCE_NODES = {
    "program", "list", "compound_statement", "do_group", "if_statement", "elif_clause",
    "else_clause", "while_statement", "case_statement", "case_item", "negated_command",
    "function_definition", "c_style_for_statement",
}


def _walk(ctx: _Ctx, node: Node) -> None:
    t = node.type
    if t in _SEQUENCE_NODES:
        for child in node.named_children:
            _walk(ctx, child)
    elif t == "subshell":
        saved_cwd, saved_bindings = ctx.cwd, dict(ctx.bindings)
        for child in node.named_children:
            _walk(ctx, child)
        ctx.cwd, ctx.bindings = saved_cwd, saved_bindings
    elif t == "pipeline":
        _pipeline(ctx, node)
    elif t == "redirected_statement":
        _redirected(ctx, node)
    elif t == "command":
        _command(ctx, node)
    elif t == "declaration_command":
        for child in node.named_children:
            if child.type == "variable_assignment":
                _assign(ctx, child)
    elif t == "variable_assignment":
        _assign(ctx, node)
    elif t == "unset_command":
        for child in node.named_children:
            ctx.bindings.pop(_text(child), None)
    elif t == "for_statement":
        _for(ctx, node)
    elif t in ("comment", "heredoc_body", "heredoc_start", "heredoc_end"):
        pass
    else:
        _scan_substitutions(ctx, node)


def _scan_substitutions(ctx: _Ctx, node: Node) -> None:
    """Run command substitutions nested anywhere under `node` (they execute too)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("command_substitution", "process_substitution"):
            for child in n.named_children:
                _walk(ctx, child)
            continue
        stack.extend(n.named_children)


def _pipeline(ctx: _Ctx, node: Node) -> None:
    ranges = []
    for stage in node.named_children:
        start = len(ctx.effects)
        _walk(ctx, stage)
        ranges.append(range(start, len(ctx.effects)))
    for left, right in zip(ranges, ranges[1:]):
        ctx.flows.extend((i, j) for i in left for j in right)


def _redirected(ctx: _Ctx, node: Node) -> None:
    body = node.child_by_field_name("body")
    start = len(ctx.effects)
    if body is not None:
        _walk(ctx, body)
    body_range = range(start, len(ctx.effects))
    redirects = node.children_by_field_name("redirect") or [c for c in node.named_children if c.type.endswith("redirect")]
    for r in redirects:
        idx = _redirect(ctx, r)
        if idx is not None and ctx.effects[idx].verb is Verb.WRITE:
            ctx.flows.extend((i, idx) for i in body_range)


_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}


def _redirect(ctx: _Ctx, node: Node) -> int | None:
    if node.type != "file_redirect":
        _scan_substitutions(ctx, node)  # heredoc / herestring bodies may hold substitutions
        return None
    op = next((c.type for c in node.children if not c.is_named), "")
    dest = node.child_by_field_name("destination")
    if dest is None:
        named = [c for c in node.named_children if c.type != "file_descriptor"]
        dest = named[-1] if named else None
    if dest is None or op in (">&", "<&"):
        return None
    arg = _eval(ctx, dest)
    if arg.text in _DEVICES:
        return None
    if op in _WRITE_REDIRECTS:
        return _add(ctx, _file_effect(ctx, Verb.WRITE, [arg], op))
    if op == "<":
        return _add(ctx, _file_effect(ctx, Verb.READ, [arg], op))
    return None


def _assign(ctx: _Ctx, node: Node) -> None:
    name_node = node.child_by_field_name("name")
    value_node = node.child_by_field_name("value")
    value = _eval(ctx, value_node) if value_node is not None else Arg("", "")
    if name_node is None:
        return
    name = _text(name_node)
    if value.text is None:
        ctx.bindings.pop(name, None)
    else:
        ctx.bindings[name] = value.text


def _for(ctx: _Ctx, node: Node) -> None:
    var_node = node.child_by_field_name("variable")
    body = node.child_by_field_name("body")
    values = node.children_by_field_name("value")
    name = _text(var_node) if var_node is not None else ""
    items: list[str] = []
    literal = bool(values)
    for v in values:
        arg = _eval(ctx, v)
        if not arg.resolved:
            literal = False
            break
        items.extend([arg.text] if v.type in ("string", "raw_string") else arg.text.split())
    if literal and len(items) <= _MAX_LOOP_ITEMS:
        for item in items:
            ctx.bindings[name] = item
            if body is not None:
                _walk(ctx, body)
    else:
        ctx.bindings.pop(name, None)
        if body is not None:
            _walk(ctx, body)


def _command(ctx: _Ctx, node: Node) -> None:
    name_node = node.child_by_field_name("name")
    args: list[Arg] = []
    redirects: list[Node] = []
    for child in node.named_children:
        if child.type == "variable_assignment":
            value = child.child_by_field_name("value")
            if value is not None:
                _eval(ctx, value)  # runs any substitution; prefix assignments do not persist
        elif child.type == "command_name":
            continue
        elif child.type.endswith("redirect"):
            redirects.append(child)
        else:
            args.append(_eval(ctx, child))
    if name_node is None:
        return
    head = name_node.named_children[0] if name_node.named_children else name_node
    start = len(ctx.effects)
    _dispatch(ctx, [_eval(ctx, head), *args])
    body_range = range(start, len(ctx.effects))
    for r in redirects:
        idx = _redirect(ctx, r)
        if idx is not None and ctx.effects[idx].verb is Verb.WRITE:
            ctx.flows.extend((i, idx) for i in body_range)


# ---------------------------------------------------------------------------------------------
# Argument evaluation


def _text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_GLOB = re.compile(r"(?<!\\)[*?\[]")


def _eval(ctx: _Ctx, node: Node) -> Arg:
    raw = _text(node)
    t = node.type
    if t == "word":
        return Arg(_ESCAPE.sub(r"\1", raw), raw, glob=bool(_GLOB.search(raw)))
    if t == "number":
        return Arg(raw, raw)
    if t == "raw_string":
        return Arg(raw[1:-1], raw)
    if t == "ansi_c_string":
        return Arg(raw[2:-1], raw)
    if t == "string":
        parts: list[str] = []
        resolved = True
        for child in node.named_children:
            if child.type == "string_content":
                parts.append(_text(child))
            else:
                sub = _eval(ctx, child)
                if sub.text is None:
                    resolved = False
                else:
                    parts.append(sub.text)
        return Arg("".join(parts) if resolved else None, raw)
    if t == "concatenation":
        parts, resolved, glob = [], True, False
        for child in node.children:
            if not child.is_named:
                parts.append(_text(child))
                continue
            sub = _eval(ctx, child)
            glob = glob or sub.glob
            if sub.text is None:
                resolved = False
            else:
                parts.append(sub.text)
        return Arg("".join(parts) if resolved else None, raw, glob)
    if t == "simple_expansion":
        return Arg(_lookup(ctx, raw[1:]), raw)
    if t == "expansion":
        inner = raw[2:-1]
        if inner.isidentifier():
            return Arg(_lookup(ctx, inner), raw)
        _scan_substitutions(ctx, node)
        return Arg(None, raw)
    if t in ("command_substitution", "process_substitution"):
        for child in node.named_children:
            _walk(ctx, child)
        return Arg(None, raw)
    _scan_substitutions(ctx, node)
    return Arg(None, raw)


def _lookup(ctx: _Ctx, name: str) -> str | None:
    if name in ctx.bindings:
        return ctx.bindings[name]
    return {"HOME": ctx.home, "USER": ctx.user, "PWD": ctx.cwd}.get(name)


def _path(ctx: _Ctx, arg: Arg) -> str | None:
    if not arg.resolved:
        return None
    p = arg.text
    if p.startswith("~"):
        if ctx.home is None:
            return None
        p = ctx.home + p[1:]
    if not p.startswith("/") and ctx.cwd is not None:
        p = posixpath.join(ctx.cwd, p)
    return posixpath.normpath(p)


def _positionals(args: list[Arg], value_flags: frozenset[str] = frozenset()) -> list[Arg]:
    out: list[Arg] = []
    i = 0
    after_dashdash = False
    while i < len(args):
        a = args[i]
        if after_dashdash or a.text is None or not a.text.startswith("-") or a.text == "-":
            out.append(a)
        elif a.text == "--":
            after_dashdash = True
        elif a.text in value_flags:
            i += 1
        i += 1
    return out


def _flag_value(args: list[Arg], *names: str) -> list[Arg]:
    """Values of flags given as `-x v`, `--long v` or `--long=v`."""
    out = []
    for i, a in enumerate(args):
        if a.text is None:
            continue
        for n in names:
            if a.text == n and i + 1 < len(args):
                out.append(args[i + 1])
            elif n.startswith("--") and a.text.startswith(n + "="):
                value = a.text[len(n) + 1 :]
                out.append(Arg(value, value))
    return out


def _has_flag(args: list[Arg], *names: str) -> bool:
    for a in args:
        if a.text is None or not a.text.startswith("-"):
            continue
        if a.text in names:
            return True
        if not a.text.startswith("--"):  # short-flag cluster like -rf
            letters = set(a.text[1:])
            if any(len(n) == 2 and n[0] == "-" and n[1] in letters for n in names):
                return True
    return False


# ---------------------------------------------------------------------------------------------
# Effect construction


def _add(ctx: _Ctx, effect: Effect | None) -> int | None:
    if effect is None:
        return None
    ctx.effects.append(effect)
    return len(ctx.effects) - 1


def _file_effect(ctx: _Ctx, verb: Verb, args: list[Arg], cmd: str, note: str = "") -> Effect | None:
    if not args:
        return None
    targets, unresolved = set(), []
    for a in args:
        p = _path(ctx, a)
        if p is None:
            unresolved.append(a.raw)
        else:
            targets.add(p)
    return Effect(verb, "file", frozenset(targets), tuple(unresolved), command=cmd, note=note)


def _opaque(prog: str, args: list[Arg], note: str = "") -> Effect:
    raw = " ".join(a.raw for a in args)
    return Effect(Verb.EXEC, "process", frozenset({prog}) if prog else frozenset(), opaque=True,
                  command=prog, note=note or raw[:200])


def _registry_effect(ctx: _Ctx, verb: Verb, kind: str, names: list[Arg], cmd: str,
                     to_id=lambda name: name, note: str = "") -> Effect | None:
    """Names checked against the registry. Unknown names stay unresolved (the call would fail)."""
    if not names:
        return None
    targets, unresolved = set(), []
    for a in names:
        if not a.resolved:
            unresolved.append(a.raw)
            continue
        rid = to_id(a.text)
        if ctx.registry.knows(kind) and ctx.registry.get(kind, rid) is None:
            unresolved.append(a.raw)
        else:
            targets.add(rid)
    return Effect(verb, kind, frozenset(targets), tuple(unresolved), command=cmd, note=note)


# ---------------------------------------------------------------------------------------------
# Dispatch


def _dispatch(ctx: _Ctx, argv: list[Arg]) -> None:
    if ctx.depth > _MAX_DEPTH:
        _add(ctx, _opaque("", argv, "nesting too deep"))
        return
    argv = _unwrap(ctx, argv)
    if not argv:
        return
    head = argv[0]
    if head.text is None:
        _add(ctx, _opaque("", argv, "program name not static"))
        return
    prog = posixpath.basename(head.text)
    rest = argv[1:]
    handler = _HANDLERS.get(prog)
    if handler is not None:
        handler(ctx, prog, rest)
    elif prog in _NOOP:
        return
    elif prog in _READERS:
        _read_files(ctx, prog, rest)
    else:  # interpreters, build tools, unknown programs
        _add(ctx, _opaque(prog, argv, "interpreter or build tool" if prog in _INTERPRETERS else ""))


def _unwrap(ctx: _Ctx, argv: list[Arg]) -> list[Arg]:
    changed = True
    while changed and argv:
        changed = False
        texts = [a.text for a in argv]
        for prefix, skip in ctx.wrappers:
            n = len(prefix)
            if len(argv) > n + skip and texts[0] is not None and posixpath.basename(texts[0]) == prefix[0] \
                    and tuple(texts[1:n]) == prefix[1:]:
                argv = argv[n + skip :]
                changed = True
                break
        if changed or argv[0].text is None:
            continue
        stripped = _strip_generic_wrapper(posixpath.basename(argv[0].text), argv[1:])
        if stripped is not None:
            argv = stripped
            changed = True
    return argv


_SUDO_VALUE_FLAGS = frozenset({"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-D"})


def _strip_generic_wrapper(prog: str, rest: list[Arg]) -> list[Arg] | None:
    def skip_flags(args: list[Arg], value_flags: frozenset[str] = frozenset()) -> list[Arg]:
        i = 0
        while i < len(args) and args[i].text is not None and args[i].text.startswith("-"):
            if args[i].text == "--":
                return args[i + 1 :]
            i += 2 if args[i].text in value_flags else 1
        return args[i:]

    if prog in ("nohup", "command", "exec", "time", "builtin"):
        if prog == "command" and rest and rest[0].text in ("-v", "-V"):
            return []
        return skip_flags(rest)
    if prog == "sudo":
        return skip_flags(rest, _SUDO_VALUE_FLAGS)
    if prog == "env":
        args = skip_flags(rest, frozenset({"-u", "-C", "-S"}))
        while args and args[0].text is not None and "=" in args[0].text and not args[0].text.startswith("-"):
            args = args[1:]
        return args
    if prog == "nice":
        return skip_flags(rest, frozenset({"-n"}))
    if prog == "timeout":
        args = skip_flags(rest, frozenset({"-s", "-k", "--signal", "--kill-after"}))
        return args[1:]  # drop the duration
    if prog in ("stdbuf", "ionice", "chrt", "taskset"):
        return skip_flags(rest, frozenset({"-c", "-n", "-p"}))
    return None


# ---------------------------------------------------------------------------------------------
# Generic file commands

_NOOP = frozenset({
    "echo", "printf", "pwd", "true", "false", "test", "[", "[[", "which", "type", "hash", "date",
    "whoami", "id", "sleep", "set", "unset", "alias", "history", "clear", "exit", "return",
    "hostname", "uname", "basename", "dirname", "seq", "yes", "tr", "read", "wait", "jobs", "umask",
    "shopt", "ulimit", "trap", "shift", "printenv", "locale", "tput", "stty", ":", "export",
})

_READERS = frozenset({
    "cat", "tac", "head", "tail", "less", "more", "wc", "stat", "file", "diff", "cmp", "sort",
    "uniq", "cut", "nl", "od", "xxd", "hexdump", "strings", "md5sum", "sha1sum", "sha256sum",
    "sha512sum", "base64", "ls", "tree", "du", "readlink", "realpath", "grep", "egrep", "fgrep",
    "rg", "ag", "jq", "yq", "column", "paste", "comm", "join", "fold", "fmt", "awk",
})

_PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "jq", "yq", "awk"})
_PATTERN_FLAGS = ("-e", "-f", "--regexp", "--file")
_DEFAULT_DOT = frozenset({"ls", "tree", "du"})

_READER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c"}),
    "tail": frozenset({"-n", "-c"}),
    "grep": frozenset({"-e", "-f", "-A", "-B", "-C", "-m", "-d", "-D"}),
    "egrep": frozenset({"-e", "-f", "-A", "-B", "-C", "-m"}),
    "fgrep": frozenset({"-e", "-f", "-A", "-B", "-C", "-m"}),
    "rg": frozenset({"-e", "-f", "-g", "-t", "-T", "-A", "-B", "-C", "-m", "-M"}),
    "sort": frozenset({"-k", "-t", "-o", "-S", "-T"}),
    "cut": frozenset({"-d", "-f", "-c", "-b"}),
    "jq": frozenset({"--arg", "--argjson", "--slurpfile", "--rawfile", "--indent"}),
    "awk": frozenset({"-F", "-v", "-f"}),
    "du": frozenset({"-d", "--max-depth"}),
    "tree": frozenset({"-L", "-I", "-P"}),
    "ls": frozenset({"-I", "--hide", "-w"}),
    "wc": frozenset(),
}


def _read_files(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    positional = _positionals(args, _READER_VALUE_FLAGS.get(prog, frozenset()))
    if prog in _PATTERN_FIRST and positional and not _flag_value(args, *_PATTERN_FLAGS):
        positional = positional[1:]
    positional = [a for a in positional if a.text != "-"]
    if prog == "sort":
        _add(ctx, _file_effect(ctx, Verb.WRITE, _flag_value(args, "-o", "--output"), prog))
    if not positional and prog in _DEFAULT_DOT:
        positional = [Arg(".", ".")]
    _add(ctx, _file_effect(ctx, Verb.READ, positional, prog))


def _rm(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    recursive = _has_flag(args, "-r", "-R", "--recursive")
    _add(ctx, _file_effect(ctx, Verb.DELETE, _positionals(args), prog, "recursive" if recursive else ""))


def _write_positional(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    value_flags = {"truncate": frozenset({"-s", "-r"}), "mkdir": frozenset({"-m"}),
                   "install": frozenset({"-m", "-o", "-g", "-t"})}.get(prog, frozenset())
    positional = _positionals(args, value_flags)
    if prog in ("chmod", "chown", "chgrp"):
        positional = positional[1:]  # mode or owner
    _add(ctx, _file_effect(ctx, Verb.WRITE, positional, prog))


def _cp_mv(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    target_dir = _flag_value(args, "-t", "--target-directory")
    positional = _positionals(args, frozenset({"-t", "-S", "--suffix"}))
    if target_dir:
        sources, dest = positional, target_dir
    else:
        sources, dest = positional[:-1], positional[-1:]
    read = _add(ctx, _file_effect(ctx, Verb.READ, sources, prog))
    write = _add(ctx, _file_effect(ctx, Verb.WRITE, dest, prog))
    if read is not None and write is not None:
        ctx.flows.append((read, write))
    if prog == "mv":
        _add(ctx, _file_effect(ctx, Verb.DELETE, sources, prog, "moved"))


def _ln(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    positional = _positionals(args, frozenset({"-t", "-S"}))
    _add(ctx, _file_effect(ctx, Verb.WRITE, positional[-1:], prog))


def _tee(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    _add(ctx, _file_effect(ctx, Verb.WRITE, _positionals(args), prog))


def _dd(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    for a in args:
        if a.text and a.text.startswith("if="):
            _add(ctx, _file_effect(ctx, Verb.READ, [Arg(a.text[3:], a.raw)], prog))
        elif a.text and a.text.startswith("of="):
            _add(ctx, _file_effect(ctx, Verb.WRITE, [Arg(a.text[3:], a.raw)], prog))


def _sed(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    in_place = any(a.text is not None and (a.text.startswith("-i") or a.text.startswith("--in-place")) for a in args)
    positional = _positionals(args, frozenset({"-e", "-f", "-l"}))
    if not _flag_value(args, "-e", "-f", "--expression", "--file"):
        positional = positional[1:]  # the script
    _add(ctx, _file_effect(ctx, Verb.WRITE if in_place else Verb.READ, positional, prog))


def _cd(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    positional = _positionals(args)
    if not positional:
        ctx.cwd = ctx.home
    elif positional[0].text == "-":
        ctx.cwd = None
    else:
        ctx.cwd = _path(ctx, positional[0])


_FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")


def _find(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    i = 0
    paths: list[Arg] = []
    while i < len(args) and (args[i].text is None or not args[i].text.startswith(("-", "(", "!"))):
        paths.append(args[i])
        i += 1
    paths = paths or [Arg(".", ".")]
    expr = args[i:]
    texts = [a.text for a in expr]
    _add(ctx, _file_effect(ctx, Verb.READ, paths, prog))
    selector = "find " + " ".join(a.raw for a in args)
    if "-delete" in texts:
        _add(ctx, Effect(Verb.DELETE, "file", unresolved=(selector,), command=prog, note="matches under " +
                         ", ".join(a.raw for a in paths)))
    for flag in ("-fprint", "-fprintf", "-fls"):
        _add(ctx, _file_effect(ctx, Verb.WRITE, _flag_value(expr, flag), prog))
    j = 0
    while j < len(expr):
        if expr[j].text in _FIND_EXEC:
            end = j + 1
            while end < len(expr) and expr[end].text not in (";", "+", "\\;"):
                end += 1
            inner = [Arg(None, "{}") if a.text is not None and "{}" in a.text else a for a in expr[j + 1 : end]]
            ctx.depth += 1
            _dispatch(ctx, inner)
            ctx.depth -= 1
            j = end
        j += 1


_XARGS_VALUE_FLAGS = frozenset({"-n", "-I", "-i", "-L", "-l", "-P", "-d", "-a", "-s", "-E", "-e", "--max-args",
                                "--replace", "--max-lines", "--max-procs", "--delimiter", "--arg-file"})


def _xargs(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    _add(ctx, _file_effect(ctx, Verb.READ, _flag_value(args, "-a", "--arg-file"), prog))
    replace = _flag_value(args, "-I", "-i", "--replace")
    i = 0
    while i < len(args) and args[i].text is not None and args[i].text.startswith("-"):
        i += 2 if args[i].text in _XARGS_VALUE_FLAGS else 1
    inner = args[i:] or [Arg("echo", "echo")]
    if replace and replace[0].text:
        token = replace[0].text
        inner = [Arg(None, a.raw) if a.text is not None and token in a.text else a for a in inner]
    else:
        inner = [*inner, _STDIN]
    ctx.depth += 1
    _dispatch(ctx, inner)
    ctx.depth -= 1


def _shell(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    code = [args[i + 1] for i, a in enumerate(args[:-1])  # -c, or a cluster like -lc / -ec
            if a.text and a.text.startswith("-") and not a.text.startswith("--") and "c" in a.text[1:]][:1]
    if code:
        if code[0].text is None:
            _add(ctx, _opaque(prog, args, "dynamic -c script"))
            return
        ctx.depth += 1
        _run_source(ctx, code[0].text)
        ctx.depth -= 1
        return
    _add(ctx, _opaque(prog, args, "script or stdin"))


def _eval_builtin(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    if args and all(a.text is not None for a in args):
        ctx.depth += 1
        _run_source(ctx, " ".join(a.text for a in args))
        ctx.depth -= 1
    else:
        _add(ctx, _opaque(prog, args, "dynamic eval"))


# ---------------------------------------------------------------------------------------------
# Network commands


def _host_of(text: str) -> str | None:
    candidate = text if "://" in text else "http://" + text
    try:
        return urllib.parse.urlsplit(candidate).hostname
    except ValueError:
        return None


def _egress(ctx: _Ctx, prog: str, hosts: list[Arg], note: str = "") -> int | None:
    if not hosts:
        return None
    targets, unresolved = set(), []
    for a in hosts:
        h = _host_of(a.text) if a.resolved else None
        if h:
            targets.add(h)
        else:
            unresolved.append(a.raw)
    return _add(ctx, Effect(Verb.EGRESS, "host", frozenset(targets), tuple(unresolved), command=prog, note=note))


_CURL_VALUE_FLAGS = frozenset({
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "-F", "--form",
    "-H", "--header", "-X", "--request", "-o", "--output", "-u", "--user", "-T", "--upload-file",
    "-A", "--user-agent", "-e", "--referer", "-b", "--cookie", "-c", "--cookie-jar", "-x", "--proxy",
    "--url", "-K", "--config", "-w", "--write-out", "--resolve", "--connect-to", "-m", "--max-time",
    "--retry", "-r", "--range", "--cacert", "--cert", "--key", "-E", "--json",
})


def _curl(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    uploads: list[Arg] = []
    payload = False
    for value in _flag_value(args, "-d", "--data", "--data-binary", "--data-urlencode", "--data-ascii", "--json"):
        payload = True
        if value.text and value.text.startswith("@") and value.text != "@-":
            uploads.append(Arg(value.text[1:], value.raw))
    for value in _flag_value(args, "-F", "--form"):
        payload = True
        if value.text and "=@" in value.text:
            uploads.append(Arg(value.text.split("=@", 1)[1].split(";")[0], value.raw))
    for value in _flag_value(args, "-T", "--upload-file"):
        payload = True
        if value.text != "-":
            uploads.append(value)
    payload = payload or bool(_flag_value(args, "--data-raw"))
    read = _add(ctx, _file_effect(ctx, Verb.READ, uploads, prog))
    hosts = _positionals(args, _CURL_VALUE_FLAGS) + _flag_value(args, "--url")
    out = _egress(ctx, prog, hosts, "sends data" if payload else "")
    if read is not None and out is not None:
        ctx.flows.append((read, out))
    _add(ctx, _file_effect(ctx, Verb.WRITE, _flag_value(args, "-o", "--output"), prog))


def _wget(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    value_flags = frozenset({"-O", "--output-document", "-o", "--output-file", "-P", "--post-file",
                             "--post-data", "-i", "--input-file", "--header", "-U", "--user-agent"})
    read = _add(ctx, _file_effect(ctx, Verb.READ, _flag_value(args, "--post-file", "-i", "--input-file"), prog))
    payload = bool(_flag_value(args, "--post-file", "--post-data"))
    hosts = _positionals(args, value_flags)
    if _flag_value(args, "-i", "--input-file"):
        hosts.append(Arg(None, "<url list>"))
    out = _egress(ctx, prog, hosts, "sends data" if payload else "")
    if read is not None and out is not None:
        ctx.flows.append((read, out))
    _add(ctx, _file_effect(ctx, Verb.WRITE, _flag_value(args, "-O", "--output-document"), prog))


_REMOTE_SPEC = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):")


def _remote_host(a: Arg) -> str | None:
    if not a.resolved:
        return None
    if a.text.startswith("rsync://"):
        return _host_of(a.text)
    m = _REMOTE_SPEC.match(a.text)
    return m.group(1) if m else None


def _scp_rsync(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    value_flags = frozenset({"-P", "-i", "-o", "-F", "-c", "-l", "-S", "-J", "-e", "--rsh", "-p"} if prog == "scp"
                            else {"-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--port"})
    _add(ctx, _file_effect(ctx, Verb.READ, _flag_value(args, "-i"), prog, "identity file"))
    positional = _positionals(args, value_flags)
    if len(positional) < 2:
        _add(ctx, _opaque(prog, args))
        return
    sources, dest = positional[:-1], positional[-1]
    dest_host = _remote_host(dest)
    if dest_host:
        read = _add(ctx, _file_effect(ctx, Verb.READ, [s for s in sources if not _remote_host(s)], prog))
        out = _add(ctx, Effect(Verb.EGRESS, "host", frozenset({dest_host}), command=prog, note="sends data"))
        if read is not None:
            ctx.flows.append((read, out))
    else:
        remote = [s for s in sources if _remote_host(s)]
        if remote:
            _add(ctx, Effect(Verb.EGRESS, "host", frozenset(h for s in remote if (h := _remote_host(s))), command=prog))
        _add(ctx, _file_effect(ctx, Verb.READ, [s for s in sources if not _remote_host(s)], prog))
        _add(ctx, _file_effect(ctx, Verb.WRITE, [dest], prog))


_SSH_VALUE_FLAGS = frozenset({"-p", "-i", "-l", "-o", "-F", "-L", "-R", "-D", "-J", "-W", "-b", "-c", "-E",
                              "-e", "-m", "-O", "-Q", "-S", "-w"})


def _ssh(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    _add(ctx, _file_effect(ctx, Verb.READ, _flag_value(args, "-i"), prog, "identity file"))
    positional = _positionals(args, _SSH_VALUE_FLAGS)
    if not positional:
        _add(ctx, _opaque(prog, args))
        return
    host = positional[0]
    if host.resolved and "@" in host.text:
        host = Arg(host.text.split("@", 1)[1], host.raw)
    _egress(ctx, prog, [host], "remote command" if len(positional) > 1 else "")
    if len(positional) > 1:
        _add(ctx, _opaque(prog, positional[1:], "remote command"))


def _netcat(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    positional = _positionals(args, frozenset({"-p", "-s", "-w", "-e", "-c", "-x", "-X"}))
    hosts = [a for a in positional[:1]] if positional else [Arg(None, prog)]
    _egress(ctx, prog, hosts, "raw socket")


# ---------------------------------------------------------------------------------------------
# Resource CLIs: git, slurm, kubectl, aws


_GIT_GLOBAL_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"})
_GIT_READ = frozenset({
    "status", "log", "show", "diff", "for-each-ref", "rev-parse", "rev-list", "ls-files", "ls-tree",
    "cat-file", "describe", "blame", "shortlog", "reflog", "show-ref", "grep", "whatchanged",
    "name-rev", "merge-base", "count-objects", "fsck", "help", "version", "var", "check-ignore",
    "symbolic-ref", "show-branch", "range-diff", "cherry",
})
_GIT_REPO_WRITE = frozenset({
    "commit", "add", "tag", "stash", "merge", "rebase", "cherry-pick", "revert", "apply", "am", "init",
    "config", "remote", "notes", "gc", "prune", "worktree", "submodule", "mv",
})
_GIT_WORKTREE_DESTRUCTIVE = frozenset({"reset", "checkout", "switch", "restore"})
_GIT_NETWORK = frozenset({"fetch", "pull", "clone", "ls-remote"})


def _git(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    i = 0
    while i < len(args) and args[i].text is not None and args[i].text.startswith("-"):
        i += 2 if args[i].text in _GIT_GLOBAL_VALUE else 1
    args = args[i:]
    if not args:
        return
    sub, rest = args[0].text, args[1:]
    if sub is None:
        _add(ctx, _opaque(prog, args, "dynamic git subcommand"))
    elif sub == "branch":
        _git_branch(ctx, rest)
    elif sub == "push":
        _git_push(ctx, rest)
    elif sub == "update-ref":
        if _has_flag(rest, "-d"):
            refs = _positionals(rest)[:1]
            _add(ctx, _registry_effect(ctx, Verb.DELETE, "branch", refs, "git", _ref_to_branch))
        else:
            _add(ctx, Effect(Verb.WRITE, "repo", frozenset({"refs"}), command="git", note="update-ref"))
    elif sub in _GIT_READ:
        _add(ctx, Effect(Verb.READ, "repo", frozenset({"repo"}), command="git", note=sub))
    elif sub == "rm":
        _add(ctx, _file_effect(ctx, Verb.DELETE, _positionals(rest), "git rm"))
    elif sub == "clean":
        _add(ctx, Effect(Verb.DELETE, "file", unresolved=("untracked files",), command="git clean"))
    elif sub in _GIT_WORKTREE_DESTRUCTIVE:
        destructive = _has_flag(rest, "--hard", "-f", "--force") or any(a.text in (".", "--") for a in rest)
        verb = Verb.DELETE if destructive else Verb.WRITE
        _add(ctx, Effect(verb, "file", unresolved=("working tree",), command=f"git {sub}"))
    elif sub in _GIT_REPO_WRITE:
        _add(ctx, Effect(Verb.WRITE, "repo", frozenset({"repo"}), command=f"git {sub}"))
    elif sub in _GIT_NETWORK:
        _egress(ctx, f"git {sub}", [Arg(None, "remote")])
        if sub != "ls-remote":
            _add(ctx, Effect(Verb.WRITE, "repo", frozenset({"repo"}), command=f"git {sub}"))
    else:
        _add(ctx, _opaque(prog, args, "unknown git subcommand or alias"))


def _ref_to_branch(ref: str) -> str:
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref.startswith("refs/remotes/"):
        return ref.removeprefix("refs/remotes/")
    return ref


_GIT_BRANCH_VALUE = frozenset({"-u", "--set-upstream-to", "--format", "--sort", "--points-at", "--contains",
                               "--no-contains", "--merged", "--no-merged", "--color", "--column"})


def _git_branch(ctx: _Ctx, rest: list[Arg]) -> None:
    positional = _positionals(rest, _GIT_BRANCH_VALUE)
    if _has_flag(rest, "-d", "-D", "--delete"):
        _add(ctx, _registry_effect(ctx, Verb.DELETE, "branch", positional, "git branch"))
    elif _has_flag(rest, "-m", "-M", "--move", "-c", "-C", "--copy"):
        _add(ctx, _registry_effect(ctx, Verb.WRITE, "branch", positional[:1], "git branch", note="rename/copy"))
    elif positional and not _has_flag(rest, "--list", "-l", "-a", "--all", "-r", "--remotes"):
        _add(ctx, Effect(Verb.WRITE, "branch", frozenset(a.text for a in positional[:1] if a.resolved),
                         tuple(a.raw for a in positional[:1] if not a.resolved), command="git branch", note="create"))
    else:
        _add(ctx, Effect(Verb.READ, "branch", frozenset(r.id for r in ctx.registry.of_kind("branch")),
                         command="git branch", note="list"))


_GIT_PUSH_VALUE = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


def _git_push(ctx: _Ctx, rest: list[Arg]) -> None:
    positional = _positionals(rest, _GIT_PUSH_VALUE)
    remote = positional[0].text if positional and positional[0].resolved else ("origin" if not positional else None)
    refspecs = positional[1:]
    if _has_flag(rest, "-n", "--dry-run"):
        _egress(ctx, "git push", [Arg(remote, remote or "remote")], "dry run")
        return
    _egress(ctx, "git push", [Arg(remote, remote or "remote")], "sends repository data")
    if remote is None:
        _add(ctx, Effect(Verb.WRITE, "branch", unresolved=("dynamic remote",), command="git push"))
        return

    def remote_id(name: str) -> str:
        return f"{remote}/{_ref_to_branch(name)}"

    deleting = _has_flag(rest, "-d", "--delete")
    delete_names: list[Arg] = []
    write_names: list[Arg] = []
    for spec in refspecs:
        if not spec.resolved:
            (delete_names if deleting else write_names).append(spec)
            continue
        text = spec.text.lstrip("+")
        if deleting:
            delete_names.append(Arg(text, spec.raw))
        elif text.startswith(":"):
            delete_names.append(Arg(text[1:], spec.raw))
        else:
            dst = text.split(":", 1)[1] if ":" in text else text
            write_names.append(Arg(None if dst == "HEAD" else dst, spec.raw))
    _add(ctx, _registry_effect(ctx, Verb.DELETE, "branch", delete_names, "git push", remote_id))
    if write_names:
        targets = frozenset(remote_id(a.text) for a in write_names if a.resolved)
        unresolved = tuple(a.raw for a in write_names if not a.resolved)
        _add(ctx, Effect(Verb.WRITE, "branch", targets, unresolved, command="git push",
                         note="force" if _has_flag(rest, "-f", "--force", "--force-with-lease") else ""))
    if _has_flag(rest, "--mirror", "--prune"):
        _add(ctx, Effect(Verb.DELETE, "branch", unresolved=("remote refs without a local counterpart",), command="git push"))
    elif not refspecs and not deleting:
        _add(ctx, Effect(Verb.WRITE, "branch", unresolved=("--all" if _has_flag(rest, "--all") else "current branch",),
                         command="git push"))


_SLURM_ACTIVE = frozenset({"running", "queued", "pending"})


def _job_filter(ctx: _Ctx, args: list[Arg]) -> tuple[dict[str, Arg], bool]:
    filters: dict[str, Arg] = {}
    for attr, names in (("owner", ("-u", "--user")), ("name", ("-n", "--name", "--jobname")),
                        ("partition", ("-p", "--partition")), ("status", ("-t", "--state"))):
        values = _flag_value(args, *names)
        if values:
            filters[attr] = values[0]
    if _has_flag(args, "--me") and ctx.user:
        filters["owner"] = Arg(ctx.user, "--me")
    return filters, bool(filters)


def _job_matches(resource: Resource, filters: dict[str, Arg]) -> bool:
    attrs = resource.attrs
    for attr, value in filters.items():
        allowed = {v.lower() for v in value.text.split(",")}
        if str(attrs.get(attr, "")).lower() not in allowed:
            return False
    return True


_SCANCEL_VALUE = frozenset({"-u", "--user", "-n", "--name", "--jobname", "-p", "--partition", "-t", "--state",
                            "-A", "--account", "-q", "--qos", "-w", "--nodelist", "-s", "--signal", "-M", "--clusters"})


def _scancel(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    ids = _positionals(args, _SCANCEL_VALUE)
    filters, filtered = _job_filter(ctx, args)
    if any(not v.resolved for v in filters.values()):
        _add(ctx, Effect(Verb.CONTROL, "job", unresolved=(" ".join(a.raw for a in args),), command=prog))
        return
    if ids:
        effect = _registry_effect(ctx, Verb.CONTROL, "job", ids, prog, note="cancel")
        if filtered and ctx.registry.knows("job"):
            kept = frozenset(t for t in effect.targets if _job_matches(ctx.registry.get("job", t), filters))
            effect = Effect(Verb.CONTROL, "job", kept, effect.unresolved, command=prog, note="cancel")
        _add(ctx, effect)
    elif filtered:
        if ctx.registry.knows("job"):
            targets = ctx.registry.select("job", lambda r: _job_matches(r, filters) and (
                "status" in filters or str(r.attrs.get("status", "")).lower() in _SLURM_ACTIVE))
            _add(ctx, Effect(Verb.CONTROL, "job", targets, command=prog, note="cancel by filter"))
        else:
            _add(ctx, Effect(Verb.CONTROL, "job", unresolved=(" ".join(a.raw for a in args),), command=prog))


def _squeue(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    filters, _ = _job_filter(ctx, args)
    targets = ctx.registry.select("job", lambda r: _job_matches(r, filters)) if all(v.resolved for v in filters.values()) else frozenset()
    _add(ctx, Effect(Verb.READ, "job", targets, command=prog, note="list"))


def _scontrol(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    positional = _positionals(args)
    action = positional[0].text if positional else None
    if action == "show":
        _add(ctx, _registry_effect(ctx, Verb.READ, "job", positional[2:3], prog) or
             Effect(Verb.READ, "job", command=prog))
    elif action in ("hold", "release", "requeue", "requeuehold", "suspend", "resume", "cancel", "update", "top"):
        names = positional[1:]
        if action == "update":
            names = [Arg(a.text.split("=", 1)[1], a.raw) for a in positional[1:]
                     if a.text and a.text.lower().startswith("jobid=")]
        _add(ctx, _registry_effect(ctx, Verb.CONTROL, "job", names, prog, note=action) or
             Effect(Verb.CONTROL, "job", unresolved=(" ".join(a.raw for a in args),), command=prog))
    else:
        _add(ctx, _opaque(prog, args))


_KUBECTL_VALUE = frozenset({
    "-n", "--namespace", "-l", "--selector", "--context", "--kubeconfig", "-o", "--output", "-f", "--filename",
    "--field-selector", "-c", "--container", "--since", "--tail", "--replicas", "--type", "-p", "--patch",
    "--cluster", "--user", "--timeout", "--grace-period", "--image",
})
_KUBECTL_TYPES = frozenset({"deployment", "deployments", "deploy", "service", "services", "svc", "pod", "pods",
                            "po", "statefulset", "statefulsets", "sts", "daemonset", "daemonsets", "ds"})
_KUBECTL_VERBS = {
    "delete": Verb.DELETE, "scale": Verb.CONTROL, "drain": Verb.CONTROL, "cordon": Verb.CONTROL,
    "uncordon": Verb.CONTROL, "annotate": Verb.WRITE, "label": Verb.WRITE, "patch": Verb.WRITE,
    "edit": Verb.WRITE, "set": Verb.WRITE, "apply": Verb.WRITE, "create": Verb.WRITE, "replace": Verb.WRITE,
    "autoscale": Verb.WRITE, "expose": Verb.WRITE, "run": Verb.WRITE, "get": Verb.READ, "describe": Verb.READ,
    "logs": Verb.READ, "top": Verb.READ, "explain": Verb.READ, "api-resources": Verb.READ, "version": Verb.READ,
}
_ROLLOUT_VERBS = {"restart": Verb.CONTROL, "undo": Verb.CONTROL, "pause": Verb.CONTROL, "resume": Verb.CONTROL,
                  "status": Verb.READ, "history": Verb.READ}


def _kubectl(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    positional = _positionals(args, _KUBECTL_VALUE)
    if not positional or positional[0].text is None:
        _add(ctx, _opaque(prog, args))
        return
    verb_text = positional[0].text
    if verb_text == "rollout" and len(positional) > 1 and positional[1].text in _ROLLOUT_VERBS:
        verb, objects, label = _ROLLOUT_VERBS[positional[1].text], positional[2:], f"rollout {positional[1].text}"
    elif verb_text in _KUBECTL_VERBS:
        verb, objects, label = _KUBECTL_VERBS[verb_text], positional[1:], verb_text
    else:
        _add(ctx, _opaque(prog, args, f"kubectl {verb_text}"))
        return
    if any(a.text and a.text.startswith("--dry-run") and not a.text.endswith("=none") for a in args):
        verb = Verb.READ

    namespaces = _flag_value(args, "-n", "--namespace")
    all_ns = _has_flag(args, "-A", "--all-namespaces")
    ns = namespaces[0] if namespaces else Arg("default", "default")
    selectors = _flag_value(args, "-l", "--selector")

    if verb_text == "apply" or (_flag_value(args, "-f", "--filename") and verb is not Verb.READ):
        _add(ctx, Effect(verb, "service", unresolved=(" ".join(a.raw for a in args),), command=prog, note=label))
        return

    names: list[Arg] = []
    for obj in objects:
        if obj.text in _KUBECTL_TYPES:
            continue
        if obj.text and "/" in obj.text:
            names.append(Arg(obj.text.split("/", 1)[1], obj.raw))
        else:
            names.append(obj)

    def in_namespace(r: Resource) -> bool:
        return all_ns or not ns.resolved or r.attrs.get("namespace") in (None, ns.text)

    if not ns.resolved and not all_ns:
        _add(ctx, Effect(verb, "service", unresolved=(ns.raw,), command=prog, note=label))
        return
    if selectors or _has_flag(args, "--all") or (not names and verb is Verb.READ):
        if selectors and not selectors[0].resolved:
            _add(ctx, Effect(verb, "service", unresolved=(selectors[0].raw,), command=prog, note=label))
            return
        wanted = dict(p.split("=", 1) for p in selectors[0].text.split(",") if "=" in p) if selectors else {}
        targets = ctx.registry.select("service", lambda r: in_namespace(r) and all(
            str(r.attrs.get(k)) == v for k, v in wanted.items()))
        _add(ctx, Effect(verb, "service", targets, command=prog, note=label))
        return
    if not ctx.registry.knows("service"):
        _add(ctx, _registry_effect(ctx, verb, "service", names, prog, note=label))
        return
    targets, unresolved = set(), []
    for a in names:
        if not a.resolved:
            unresolved.append(a.raw)
            continue
        match = [r for r in ctx.registry.of_kind("service")
                 if (r.id == a.text or r.attrs.get("name") == a.text) and in_namespace(r)]
        if match:
            targets.update(r.id for r in match)
        else:
            unresolved.append(a.raw)  # unknown in that namespace: the call would fail
    _add(ctx, Effect(verb, "service", frozenset(targets), tuple(unresolved), command=prog, note=label))


_AWS_GLOBAL_VALUE = frozenset({"--profile", "--region", "--endpoint-url", "--output", "--query", "--color",
                               "--cli-read-timeout", "--cli-connect-timeout", "--ca-bundle"})
_AWS_S3_VALUE = frozenset({"--exclude", "--include", "--acl", "--storage-class", "--sse", "--content-type",
                           "--bucket", "--key", "--prefix", "--delete", "--body", "--expires-in", "--grants",
                           "--policy", "--version-id", "--region", "--profile", "--endpoint-url"})


def _split_s3(text: str) -> tuple[str, str] | None:
    if not text.startswith("s3://"):
        return None
    bucket, _, key = text[5:].partition("/")
    return bucket, key


def _s3_select(ctx: _Ctx, bucket: str, key: str, recursive: bool, filters: list[tuple[str, str]]) -> frozenset[str]:
    def keep(r: Resource) -> bool:
        if r.attrs.get("bucket") not in (None, bucket):
            return False
        obj_key = str(r.attrs.get("key", ""))
        if not recursive:
            return obj_key == key
        if not obj_key.startswith(key):
            return False
        included = True
        relative = obj_key[len(key):]
        for kind, pattern in filters:
            if fnmatch.fnmatchcase(relative, pattern):
                included = kind == "--include"
        return included
    return ctx.registry.select("object", keep)


def _s3_effect(ctx: _Ctx, verb: Verb, uri: Arg, recursive: bool, args: list[Arg], cmd: str) -> Effect:
    filters = [(a.text, b.text) for a, b in zip(args, args[1:]) if a.text in ("--exclude", "--include")]
    parsed = _split_s3(uri.text) if uri.resolved else None
    if parsed is None or any(p is None for _, p in filters):
        return Effect(verb, "object", unresolved=(uri.raw,), command=cmd)
    bucket, key = parsed
    if not ctx.registry.knows("object"):
        return Effect(verb, "object", frozenset({uri.text}), exact=not recursive, command=cmd,
                      note="prefix" if recursive else "")
    return Effect(verb, "object", _s3_select(ctx, bucket, key, recursive, filters), command=cmd,
                  note="prefix" if recursive else "")


def _aws(ctx: _Ctx, prog: str, args: list[Arg]) -> None:
    i = 0
    rest: list[Arg] = []
    while i < len(args):  # drop global options wherever they appear
        a = args[i]
        if a.text in _AWS_GLOBAL_VALUE:
            i += 2
            continue
        if a.text in ("--no-cli-pager", "--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request"):
            i += 1
            continue
        rest.append(a)
        i += 1
    positional = _positionals(rest, _AWS_S3_VALUE)
    if len(positional) < 2 or positional[0].text is None or positional[1].text is None:
        _add(ctx, _opaque(prog, args))
        return
    service, op, operands = positional[0].text, positional[1].text, positional[2:]
    recursive = _has_flag(rest, "--recursive")
    cmd = f"aws {service} {op}"
    if service == "s3":
        if op == "rm" and operands:
            _add(ctx, _s3_effect(ctx, Verb.DELETE, operands[0], recursive, rest, cmd))
        elif op == "ls":
            if operands:
                _add(ctx, _s3_effect(ctx, Verb.READ, operands[0], True, rest, cmd))
            else:
                _add(ctx, Effect(Verb.READ, "object", frozenset(), command=cmd, note="list buckets"))
        elif op in ("cp", "mv", "sync") and len(operands) >= 2:
            src, dst = operands[0], operands[1]
            whole = recursive or op == "sync"
            src_effect = (_s3_effect(ctx, Verb.READ, src, whole, rest, cmd) if src.text and src.text.startswith("s3://")
                          else _file_effect(ctx, Verb.READ, [src], cmd))
            read = _add(ctx, src_effect)
            if dst.text and dst.text.startswith("s3://"):
                write = _add(ctx, Effect(Verb.WRITE, "object", unresolved=(dst.raw,), command=cmd))
            else:
                write = _add(ctx, _file_effect(ctx, Verb.WRITE, [dst], cmd))
            if read is not None and write is not None:
                ctx.flows.append((read, write))
            if op == "mv" and src.text and src.text.startswith("s3://"):
                _add(ctx, _s3_effect(ctx, Verb.DELETE, src, whole, rest, cmd))
            if op == "sync" and _has_flag(rest, "--delete"):
                _add(ctx, Effect(Verb.DELETE, "object", unresolved=(dst.raw,), command=cmd, note="sync --delete"))
        elif op == "rb" and operands:
            bucket = _split_s3(operands[0].text or "")
            if bucket and _has_flag(rest, "--force"):
                _add(ctx, _s3_effect(ctx, Verb.DELETE, Arg(f"s3://{bucket[0]}/", operands[0].raw), True, rest, cmd))
            _add(ctx, Effect(Verb.DELETE, "bucket", frozenset({bucket[0]}) if bucket else frozenset(),
                             () if bucket else (operands[0].raw,), command=cmd))
        elif op == "presign" and operands:
            _add(ctx, _s3_effect(ctx, Verb.READ, operands[0], False, rest, cmd))
            _add(ctx, Effect(Verb.EGRESS, "host", unresolved=("presigned URL",), command=cmd, note="shareable link"))
        else:
            _add(ctx, _opaque(prog, args, cmd))
    elif service == "s3api":
        bucket = _flag_value(rest, "--bucket")
        key = _flag_value(rest, "--key")
        uri = (Arg(f"s3://{bucket[0].text}/{key[0].text}", f"{bucket[0].raw} {key[0].raw}")
               if bucket and key and bucket[0].resolved and key[0].resolved else Arg(None, cmd))
        verbs = {"delete-object": Verb.DELETE, "get-object": Verb.READ, "head-object": Verb.READ,
                 "put-object": Verb.WRITE, "put-object-acl": Verb.WRITE, "get-object-acl": Verb.READ}
        if op in verbs:
            _add(ctx, _s3_effect(ctx, verbs[op], uri, False, rest, cmd))
            if op == "get-object" and operands:
                _add(ctx, _file_effect(ctx, Verb.WRITE, operands[-1:], cmd))
        elif op == "delete-objects":
            _add(ctx, Effect(Verb.DELETE, "object", unresolved=(" ".join(a.raw for a in rest),), command=cmd))
        elif op in ("list-objects", "list-objects-v2"):
            prefix = _flag_value(rest, "--prefix")
            target = Arg(f"s3://{bucket[0].text}/{prefix[0].text if prefix else ''}", cmd) \
                if bucket and bucket[0].resolved and (not prefix or prefix[0].resolved) else Arg(None, cmd)
            _add(ctx, _s3_effect(ctx, Verb.READ, target, True, rest, cmd))
        elif op.startswith("put-bucket") or op.startswith("delete-bucket"):
            _add(ctx, Effect(Verb.WRITE, "bucket", frozenset(b.text for b in bucket if b.resolved),
                             command=cmd, note="bucket configuration"))
        else:
            _add(ctx, _opaque(prog, args, cmd))
    else:
        _add(ctx, _opaque(prog, args, cmd))


# ---------------------------------------------------------------------------------------------

_INTERPRETERS = frozenset({
    "python", "python2", "python3", "node", "ruby", "perl", "php", "deno", "bun", "java", "go", "cargo",
    "make", "cmake", "npm", "npx", "yarn", "pnpm", "pip", "pip3", "pytest", "tox", "nox", "gradle", "mvn",
    "docker", "podman", "osascript", "pwsh", "powershell", "source", ".", "sqlite3", "psql", "mysql",
    "mongosh", "mongo", "redis-cli", "uv", "poetry", "conda",
})

_HANDLERS = {
    "rm": _rm, "rmdir": _rm, "unlink": _rm, "shred": _rm,
    "touch": _write_positional, "mkdir": _write_positional, "chmod": _write_positional,
    "chown": _write_positional, "chgrp": _write_positional, "truncate": _write_positional,
    "install": _write_positional,
    "cp": _cp_mv, "mv": _cp_mv, "ln": _ln, "tee": _tee, "dd": _dd, "sed": _sed,
    "cd": _cd, "pushd": _cd, "find": _find, "xargs": _xargs,
    "bash": _shell, "sh": _shell, "zsh": _shell, "dash": _shell, "ksh": _shell, "eval": _eval_builtin,
    "curl": _curl, "wget": _wget, "scp": _scp_rsync, "rsync": _scp_rsync, "ssh": _ssh,
    "nc": _netcat, "ncat": _netcat, "netcat": _netcat, "socat": _netcat, "telnet": _netcat,
    "ftp": _netcat, "sftp": _netcat,
    "git": _git, "scancel": _scancel, "squeue": _squeue, "scontrol": _scontrol,
    "kubectl": _kubectl, "aws": _aws,
}
