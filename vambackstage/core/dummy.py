"""Dummy packages: empty .var files that satisfy a dependency reference.

VaM reports a missing package on every load until the user digs into its
package manager and ignores it per package. When the real package cannot be
had (gone from the Hub, paid and renamed on a version bump, never existed),
an empty package with the referenced name makes the reference resolve. The
dummy carries a marker in meta.json so Backstage recognises its own."""

from __future__ import annotations

import io
import json
import zipfile

from .graph import DepRef

DUMMY_META_KEY = "vamBackstageDummy"
DUMMIES_SUBDIR = "Dummies"
ALIASES_SUBDIR = "Aliases"


def dummy_version_for_ref(parsed: DepRef) -> int:
    if parsed.version == "latest":
        return 1
    if parsed.version == "min":
        return max(1, int(parsed.min_version or 1))
    return int(parsed.version)


def dummy_filename_for_ref(parsed: DepRef) -> str:
    return f"{parsed.package_name}.{dummy_version_for_ref(parsed)}.var"


def build_dummy_var(parsed: DepRef, ref: str, depends_on: tuple[str, ...] = ()) -> bytes:
    creator, _, package = parsed.package_name.partition(".")
    meta = {
        "licenseType": "CC BY",
        "creatorName": creator,
        "packageName": package,
        "standardReferenceVersionOption": "Latest",
        "scriptReferenceVersionOption": "Latest",
        "description": (
            f"Dummy package created by VaM Backstage to satisfy the dependency reference {ref}. "
            "It contains no content; it only exists so VaM stops reporting the reference as missing."
        ),
        "credits": "",
        "instructions": "",
        "promotionalLink": "",
        "programVersion": "1.22.0.3",
        "contentList": [],
        "dependencies": {d: {"licenseType": "CC BY", "dependencies": {}} for d in depends_on},
        "hadReferenceIssues": "false",
        "referenceIssues": [],
        "customOptions": {},
        DUMMY_META_KEY: True,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, indent=3))
    return buf.getvalue()
