from __future__ import annotations

import codecs
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field

from .lenient_json import loads_lenient
from .varfile import walk_unpacked

KIND_JSON, KIND_TEXT, KIND_BINARY = "json", "text", "binary"

JSON_SUFFIXES = frozenset({".json", ".vap", ".vaj", ".vam", ".vmi", ".uiap"})
TEXT_SUFFIXES = frozenset(
    {".cs", ".cslist", ".txt", ".md", ".shader", ".cginc", ".hlsl", ".glsl", ".cfg", ".ini", ".csv", ".xml", ".html"}
)
BINARY_SUFFIXES = frozenset(
    {
        ".vab",
        ".vmb",
        ".assetbundle",
        ".dll",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".tga",
        ".bmp",
        ".psd",
        ".ogg",
        ".mp3",
        ".wav",
        ".mp4",
        ".zip",
        ".vac",
        ".ttf",
        ".otf",
        ".dsf",
        ".duf",
        ".obj",
        ".fbx",
        ".exr",
        ".hdr",
    }
)

JsonPath = tuple[str | int, ...]


def file_kind(path: str, head: bytes | None = None) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in JSON_SUFFIXES:
        return KIND_JSON
    if ext in TEXT_SUFFIXES:
        return KIND_TEXT
    if ext in BINARY_SUFFIXES or not head:
        return KIND_BINARY
    body = head[len(codecs.BOM_UTF8) :] if head.startswith(codecs.BOM_UTF8) else head
    if b"\0" in body:
        return KIND_BINARY
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as err:
        if err.start < len(body) - 3:
            return KIND_BINARY
    return KIND_JSON if body.lstrip()[:1] in (b"{", b"[") else KIND_TEXT


def file_kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in JSON_SUFFIXES or ext in TEXT_SUFFIXES or ext in BINARY_SUFFIXES:
        return file_kind(path)
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return KIND_BINARY
    return file_kind(path, head)


@dataclass(frozen=True)
class PackageFile:
    path: str
    size: int
    mtime: float
    kind: str


def list_package_files(folder: str | os.PathLike) -> list[PackageFile]:
    return [PackageFile(rel, size, mtime, file_kind_of(full)) for rel, full, size, mtime in walk_unpacked(folder)]


@dataclass
class JsonDocument:
    path: str
    data: object
    bom: bool = False


def load_json_document(path: str | os.PathLike) -> JsonDocument:
    path = os.fspath(path)
    with open(path, "rb") as f:
        raw = f.read()
    try:
        data = loads_lenient(raw)
    except ValueError as err:
        raise ValueError(f"{os.path.basename(path)} is not JSON: {err}") from err
    return JsonDocument(path, data, raw.startswith(codecs.BOM_UTF8))


def save_json_document(doc: JsonDocument) -> int:
    payload = (codecs.BOM_UTF8 if doc.bom else b"") + dumps_vam(doc.data).encode("utf-8")
    write_atomic(doc.path, payload)
    return len(payload)


def write_atomic(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".edit-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dumps_vam(value) -> str:
    out: list[str] = []
    _write(value, out, "")
    return "".join(out)


def _write(v, out: list[str], prefix: str) -> None:
    if isinstance(v, dict):
        out.append("{ ")
        inner = prefix + "   "
        for i, (k, item) in enumerate(v.items()):
            if i:
                out.append(", ")
            out.append("\n" + inner + json.dumps(str(k), ensure_ascii=False) + " : ")
            _write(item, out, inner)
        out.append("\n" + prefix + "}")
    elif isinstance(v, (list, tuple)):
        out.append("[ ")
        inner = prefix + "   "
        for i, item in enumerate(v):
            if i:
                out.append(", ")
            out.append("\n" + inner)
            _write(item, out, inner)
        out.append("\n" + prefix + "]")
    elif isinstance(v, str):
        out.append(json.dumps(v, ensure_ascii=False))
    elif v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, (int, float)):
        out.append(json.dumps(v))
    else:
        raise TypeError(f"cannot write a {type(v).__name__} as JSON")


def get_at(data, path: JsonPath):
    node = data
    for key in path:
        node = node[key]
    return node


def _container_and_key(data, path: JsonPath):
    if not path:
        raise ValueError("the root has no parent")
    parent = get_at(data, path[:-1])
    key = path[-1]
    if isinstance(parent, dict):
        if not isinstance(key, str) or key not in parent:
            raise KeyError(key)
    elif isinstance(parent, list):
        if not isinstance(key, int) or not 0 <= key < len(parent):
            raise IndexError(key)
    else:
        raise TypeError("not inside an object or array")
    return parent, key


def set_at(data, path: JsonPath, value):
    if not path:
        return value
    parent, key = _container_and_key(data, path)
    parent[key] = value
    return data


def delete_at(data, path: JsonPath):
    parent, key = _container_and_key(data, path)
    return parent.pop(key)


def insert_at(data, parent_path: JsonPath, key: str | None, value, position: int | None = None):
    parent = get_at(data, parent_path)
    if isinstance(parent, dict):
        if not isinstance(key, str) or not key:
            raise ValueError("an object member needs a key")
        if key in parent:
            raise KeyError(f"“{key}” is already there")
        if position is None or position >= len(parent):
            parent[key] = value
        else:
            items = list(parent.items())
            items.insert(max(0, position), (key, value))
            parent.clear()
            parent.update(items)
        return key
    if isinstance(parent, list):
        pos = len(parent) if position is None else max(0, min(position, len(parent)))
        parent.insert(pos, value)
        return pos
    raise TypeError("only objects and arrays take new entries")


def rename_key(data, path: JsonPath, new_key: str) -> None:
    parent, old = _container_and_key(data, path)
    if not isinstance(parent, dict):
        raise TypeError("only object members have keys")
    if not isinstance(new_key, str) or not new_key:
        raise ValueError("a key cannot be empty")
    if new_key == old:
        return
    if new_key in parent:
        raise KeyError(f"“{new_key}” is already there")
    items = [(new_key if k == old else k, v) for k, v in parent.items()]
    parent.clear()
    parent.update(items)


def index_in_parent(data, path: JsonPath) -> int:
    parent, key = _container_and_key(data, path)
    return list(parent).index(key) if isinstance(parent, dict) else key


def format_path(path: JsonPath) -> str:
    parts: list[str] = []
    for k in path:
        if isinstance(k, int):
            parts.append(f"[{k}]")
        elif k.replace("_", "a").isalnum():
            parts.append(("." if parts else "") + k)
        else:
            parts.append("[" + json.dumps(k, ensure_ascii=False) + "]")
    return "".join(parts) or "(root)"


def iter_nodes(data, path: JsonPath = ()) -> Iterator[tuple[JsonPath, object]]:
    stack: list[tuple[JsonPath, object]] = [(path, data)]
    while stack:
        p, v = stack.pop()
        yield p, v
        if isinstance(v, dict):
            stack.extend(reversed([(p + (k,), item) for k, item in v.items()]))
        elif isinstance(v, list):
            stack.extend(reversed([(p + (i,), item) for i, item in enumerate(v)]))


def find_matches(
    data, needle: str, *, in_keys: bool = True, in_values: bool = True, case_sensitive: bool = False
) -> list[JsonPath]:
    if not needle:
        return []
    n = needle if case_sensitive else needle.lower()

    def hit(s: str) -> bool:
        return n in (s if case_sensitive else s.lower())

    out: list[JsonPath] = []
    for path, value in iter_nodes(data):
        if path and in_keys and isinstance(path[-1], str) and hit(path[-1]):
            out.append(path)
        elif in_values and isinstance(value, str) and hit(value):
            out.append(path)
    return out


def replace_text(data, old: str, new: str, *, in_keys: bool = True, in_values: bool = True) -> tuple[object, int]:
    if not old:
        raise ValueError("nothing to find")
    count = 0

    def walk(node) -> None:
        nonlocal count
        if isinstance(node, dict):
            renamed: set[str] = set()
            for k, v in list(node.items()):
                if isinstance(v, str):
                    if in_values and old in v:
                        count += v.count(old)
                        node[k] = v.replace(old, new)
                else:
                    walk(v)
                if in_keys and old in k:
                    count += k.count(old)
                    renamed.add(k)
            if renamed:
                items = [((k.replace(old, new) if k in renamed else k), v) for k, v in node.items()]
                node.clear()
                node.update(items)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    if in_values and old in v:
                        count += v.count(old)
                        node[i] = v.replace(old, new)
                else:
                    walk(v)

    if isinstance(data, str):
        if in_values and old in data:
            return data.replace(old, new), data.count(old)
        return data, 0
    walk(data)
    return data, count


@dataclass
class ReplaceResult:
    files: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.files.values())


def replace_in_package(
    folder: str | os.PathLike, old: str, new: str, *, in_keys: bool = True, in_values: bool = True
) -> ReplaceResult:
    if not old:
        raise ValueError("nothing to find")
    result = ReplaceResult()
    for rel, full, _size, _mtime in walk_unpacked(folder):
        if file_kind_of(full) != KIND_JSON:
            continue
        try:
            doc = load_json_document(full)
        except (ValueError, OSError) as err:
            result.errors[rel] = str(err)
            continue
        root, n = replace_text(doc.data, old, new, in_keys=in_keys, in_values=in_values)
        if not n:
            continue
        doc.data = root
        try:
            save_json_document(doc)
        except OSError as err:
            result.errors[rel] = str(err)
            continue
        result.files[rel] = n
    return result
