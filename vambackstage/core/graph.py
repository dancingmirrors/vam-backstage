"""Dependency graph: ref parsing, resolution against local packages, and the
cascade / orphan algorithms built on top.

Package rows are plain dicts with at least `filename`, `package_name`,
`version`, `dep_refs` (JSON array string), `is_direct`, `size_bytes` and
`storage_state` — the shape of a `packages` row."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

_DIGITS_RE = re.compile(r"[0-9]+")
_MIN_RE = re.compile(r"min([0-9]+)")


@dataclass(frozen=True)
class DepRef:
    creator: str
    package_name: str
    version: str
    raw: str
    min_version: int | None = None


class Resolution(NamedTuple):
    resolved: str | None
    resolution: str


class DepEdge(NamedTuple):
    ref: str
    resolved: str | None
    resolution: str


def parse_dep_ref(ref) -> DepRef | None:
    if not isinstance(ref, str):
        return None
    parts = ref.split(".")
    if len(parts) < 3:
        return None
    last = parts[-1]
    lower = last.lower()
    package_name = ".".join(parts[:-1])
    if _DIGITS_RE.fullmatch(last):
        return DepRef(parts[0], package_name, last, ref)
    if lower == "latest":
        return DepRef(parts[0], package_name, "latest", ref)
    m = _MIN_RE.fullmatch(lower)
    if m:
        return DepRef(parts[0], package_name, "min", ref, int(m.group(1)))
    return None


def is_flexible_ref(parsed: DepRef | None) -> bool:
    return parsed is not None and parsed.version in ("latest", "min")


def _collect_dep_keys(node, out: dict[str, None]) -> None:
    if isinstance(node, list):
        for entry in node:
            if isinstance(entry, str):
                out[entry] = None
    elif isinstance(node, dict):
        for key, val in node.items():
            out[key] = None
            if isinstance(val, dict) and val.get("dependencies"):
                _collect_dep_keys(val["dependencies"], out)


def extract_dep_refs(meta: dict | None, self_filename: str) -> list[str]:
    if not meta or not meta.get("dependencies"):
        return []
    self_stem = re.sub(r"\.var$", "", self_filename, flags=re.IGNORECASE)
    collected: dict[str, None] = {}
    _collect_dep_keys(meta["dependencies"], collected)
    refs: list[str] = []
    for key in collected:
        parts = key.split(".")
        if len(parts) >= 3:
            lower = parts[-1].lower()
            if lower == "latest" or re.fullmatch(r"min[0-9]+", lower):
                parts[-1] = lower
        normalized = ".".join(parts)
        if normalized != self_stem:
            refs.append(normalized)
    return refs


_PACKAGE_PATH_RE = re.compile(
    rb'(?<=")([^.":/\\\s][^.":/\\]*\.[^.":/\\]+\.(?:[0-9]+|[Ll][Aa][Tt][Ee][Ss][Tt]|[Mm][Ii][Nn][0-9]+)):/'
)


def extract_refs_from_text(data: bytes) -> list[str]:
    seen: dict[str, None] = {}
    for m in _PACKAGE_PATH_RE.finditer(data):
        parts = m.group(1).decode("ascii", "ignore").split(".")
        if len(parts) < 3 or parts[0].upper() == "SELF":
            continue
        lower = parts[-1].lower()
        if lower == "latest" or re.fullmatch(r"min[0-9]+", lower):
            parts[-1] = lower
        seen[".".join(parts)] = None
    return list(seen)


def pick_highest_version(
    filenames: Iterable[str], package_index: Mapping[str, dict], min_version: int = 0
) -> str | None:
    best = None
    best_ver = -1
    for fn in filenames:
        pkg = package_index.get(fn)
        if not pkg:
            continue
        try:
            v = int(pkg["version"])
        except (TypeError, ValueError):
            continue
        if v >= min_version and v > best_ver:
            best_ver = v
            best = fn
    return best


def resolve_ref(ref: str, package_index: Mapping[str, dict], group_index: Mapping[str, list[str]]) -> Resolution:
    parsed = parse_dep_ref(ref)
    if parsed is None:
        return Resolution(None, "invalid")

    if not is_flexible_ref(parsed):
        exact_key = ref + ".var"
        if exact_key in package_index:
            return Resolution(exact_key, "exact")
        if ref in package_index:
            return Resolution(ref, "exact")

    candidates = group_index.get(parsed.package_name) or []
    if not candidates:
        return Resolution(None, "missing")

    if parsed.version == "latest":
        best = pick_highest_version(candidates, package_index)
        return Resolution(best, "latest") if best else Resolution(None, "missing")

    if parsed.version == "min":
        satisfying = pick_highest_version(candidates, package_index, parsed.min_version or 0)
        if satisfying:
            return Resolution(satisfying, "latest")
        best = pick_highest_version(candidates, package_index)
        return Resolution(best, "fallback") if best else Resolution(None, "missing")

    best = pick_highest_version(candidates, package_index)
    return Resolution(best, "fallback") if best else Resolution(None, "missing")


def dep_refs_of(pkg: dict) -> list[str]:
    raw = pkg.get("dep_refs") or "[]"
    if isinstance(raw, list):
        return list(raw)
    try:
        refs = json.loads(raw)
    except ValueError:
        return []
    return [r for r in refs if isinstance(r, str)]


def build_group_index(package_index: Mapping[str, dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for filename, pkg in package_index.items():
        groups.setdefault(pkg["package_name"], []).append(filename)
    return groups


def build_forward_deps(
    package_index: Mapping[str, dict], group_index: Mapping[str, list[str]]
) -> dict[str, list[DepEdge]]:
    forward: dict[str, list[DepEdge]] = {}
    for filename, pkg in package_index.items():
        edges = []
        for ref in dep_refs_of(pkg):
            res = resolve_ref(ref, package_index, group_index)
            edges.append(DepEdge(ref, res.resolved, res.resolution))
        forward[filename] = edges
    return forward


def build_reverse_deps(forward_deps: Mapping[str, list[DepEdge]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for filename, deps in forward_deps.items():
        for dep in deps:
            if dep.resolved:
                reverse.setdefault(dep.resolved, set()).add(filename)
    return reverse


def detect_leaves(package_index: Mapping[str, dict], reverse_deps: Mapping[str, set[str]]) -> set[str]:
    return {fn for fn in package_index if not reverse_deps.get(fn)}


def get_transitive_deps(filename: str, forward_deps: Mapping[str, list[DepEdge]]) -> set[str]:
    visited: set[str] = set()
    stack = [filename]
    while stack:
        current = stack.pop()
        for dep in forward_deps.get(current, ()):
            if dep.resolved and dep.resolved not in visited:
                visited.add(dep.resolved)
                stack.append(dep.resolved)
    return visited


def _is_active(pkg: dict | None) -> bool:
    return bool(pkg) and pkg.get("storage_state") == "enabled"


def _is_archived(pkg: dict | None) -> bool:
    return bool(pkg) and pkg.get("storage_state") == "archived"


def dependent_pins(dependent_pkg: dict | None, dep_filename: str, replaceable_set: set[str]) -> bool:
    if not dependent_pkg:
        return False
    if not _is_archived(dependent_pkg):
        return True
    return dep_filename not in replaceable_set


def compute_cascade_disable(
    filename: str,
    package_index: Mapping[str, dict],
    forward_deps: Mapping[str, list[DepEdge]],
    reverse_deps: Mapping[str, set[str]],
) -> set[str]:
    to_disable: set[str] = set()
    transitive = get_transitive_deps(filename, forward_deps)
    changed = True
    while changed:
        changed = False
        for dep in transitive:
            pkg = package_index.get(dep)
            if dep in to_disable or not _is_active(pkg) or (pkg or {}).get("is_direct"):
                continue
            dependents = reverse_deps.get(dep, set())
            has_other_enabled = any(
                d != filename and d not in to_disable and _is_active(package_index.get(d)) for d in dependents
            )
            if not has_other_enabled:
                to_disable.add(dep)
                changed = True
    return to_disable


def compute_cascade_enable(
    filename: str, package_index: Mapping[str, dict], forward_deps: Mapping[str, list[DepEdge]]
) -> set[str]:
    return {
        dep
        for dep in get_transitive_deps(filename, forward_deps)
        if package_index.get(dep) and not _is_active(package_index.get(dep))
    }


@dataclass
class OrphanResult:
    orphans: set[str]
    direct_orphans: set[str]
    total_size: int


def compute_orphan_cascade(
    package_index: Mapping[str, dict],
    forward_deps: Mapping[str, list[DepEdge]],
    reverse_deps: Mapping[str, set[str]],
    replaceable_set: set[str] | None = None,
) -> OrphanResult:
    replaceable_set = replaceable_set or set()
    direct_orphans: set[str] = set()
    for filename, pkg in package_index.items():
        if pkg.get("is_direct") or _is_archived(pkg):
            continue
        dependents = reverse_deps.get(filename)
        has_pin = bool(dependents) and any(
            dependent_pins(package_index.get(d), filename, replaceable_set) for d in dependents
        )
        if not has_pin:
            direct_orphans.add(filename)

    to_remove = set(direct_orphans)
    changed = True
    while changed:
        changed = False
        for filename, pkg in package_index.items():
            if pkg.get("is_direct") or filename in to_remove or _is_archived(pkg):
                continue
            dependents = reverse_deps.get(filename, set())
            pinning = [d for d in dependents if dependent_pins(package_index.get(d), filename, replaceable_set)]
            if not pinning:
                continue
            if all(d in to_remove for d in pinning):
                to_remove.add(filename)
                changed = True

    total = sum(package_index[fn].get("size_bytes") or 0 for fn in to_remove if fn in package_index)
    return OrphanResult(to_remove, direct_orphans, total)


@dataclass
class RemovableResult:
    removable_filenames: set[str]
    removable_size: int


def compute_removable_deps(
    filename: str,
    package_index: Mapping[str, dict],
    forward_deps: Mapping[str, list[DepEdge]],
    reverse_deps: Mapping[str, set[str]],
    replaceable_set: set[str] | None = None,
) -> RemovableResult:
    replaceable_set = replaceable_set or set()
    to_remove = {filename}
    transitive = get_transitive_deps(filename, forward_deps)
    changed = True
    while changed:
        changed = False
        for dep in transitive:
            if dep in to_remove:
                continue
            pkg = package_index.get(dep)
            if not pkg or pkg.get("is_direct") or _is_archived(pkg):
                continue
            dependents = reverse_deps.get(dep, set())
            pinning = [d for d in dependents if dependent_pins(package_index.get(d), dep, replaceable_set)]
            if all(d in to_remove for d in pinning):
                to_remove.add(dep)
                changed = True
    to_remove.discard(filename)
    size = sum(package_index[fn].get("size_bytes") or 0 for fn in to_remove if fn in package_index)
    return RemovableResult(to_remove, size)
