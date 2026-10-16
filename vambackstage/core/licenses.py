"""License labels aligned with Hub / meta.json `licenseType`."""

from __future__ import annotations

import re

CC_LICENSE_LABELS = ("Public Domain", "CC BY", "CC BY-SA", "CC BY-ND", "CC BY-NC", "CC BY-NC-SA", "CC BY-NC-ND")
VAM_LICENSE_LABELS = ("FC", "PC", "PC EA", "Questionable")

LICENSE_DESCRIPTIONS = {
    "Public Domain": "No restrictions. Free to use, modify, and distribute for any purpose.",
    "CC BY": "Distribute, remix, and build upon, even commercially. Must credit the original creator.",
    "CC BY-SA": "Distribute, remix, and build upon, even commercially. Must credit and use the same license for derivatives.",
    "CC BY-ND": "Redistribute commercially or non-commercially, but cannot modify. Must credit the original creator.",
    "CC BY-NC": "Distribute, remix, and build upon non-commercially only. Must credit the original creator.",
    "CC BY-NC-SA": "Distribute, remix, and build upon non-commercially only. Must credit and use the same license for derivatives.",
    "CC BY-NC-ND": "Most restrictive CC license. Share unchanged only, non-commercially, with credit to the original creator.",
    "FC": "Free Content: distribute, remix, and build upon, even commercially. No credit required.",
    "PC": "Paid Content: cannot distribute, remix, tweak, or build upon.",
    "PC EA": "Paid Content Early Access: same restrictions as PC until the EA end date, then the secondary license applies.",
    "Questionable": "Content origin cannot be determined. Should not distribute.",
}

COMMERCIAL_USE_ALLOWED_LICENSE_FILTER = "Commercial use allowed"
NONCOMMERCIAL_USE_ALLOWED_LICENSE_FILTER = "Non-commercial use allowed"
LICENSE_FILTER_OPTIONS = (
    "Any",
    NONCOMMERCIAL_USE_ALLOWED_LICENSE_FILTER,
    COMMERCIAL_USE_ALLOWED_LICENSE_FILTER,
    *CC_LICENSE_LABELS,
    *VAM_LICENSE_LABELS,
)

_ALL_KNOWN = CC_LICENSE_LABELS + VAM_LICENSE_LABELS
_COMMERCIAL_ALLOWED = {"Public Domain", "CC BY", "CC BY-SA", "CC BY-ND", "FC"}
_COMMERCIAL_DENIED = {"PC", "PC EA", "Questionable"}
_CANONICAL_BY_COMPACT = {label.replace(" ", "").lower(): label for label in _ALL_KNOWN}
_CANONICAL_BY_COMPACT.update({"cc0": "Public Domain", "cc-0": "Public Domain", "pcea": "PC EA", "pc-ea": "PC EA"})


def canonicalize_license(raw) -> str | None:
    if raw is None:
        return None
    s = re.sub(r"\s+", " ", str(raw).strip())
    if not s:
        return None
    hit = _CANONICAL_BY_COMPACT.get(s.replace(" ", "").lower())
    if hit:
        return hit
    for label in _ALL_KNOWN:
        if s.lower() == label.lower():
            return label
    return s


def is_commercial_use_allowed(raw) -> bool | None:
    c = canonicalize_license(raw)
    if not c:
        return None
    if c in _COMMERCIAL_ALLOWED:
        return True
    if c in _COMMERCIAL_DENIED:
        return False
    if c.startswith("CC BY-NC") or (c.startswith("CC ") and "-NC" in c):
        return False
    if c in CC_LICENSE_LABELS:
        return "-NC" not in c
    if re.search(r"non-?commercial", c, re.IGNORECASE):
        return False
    return None


def is_noncommercial_use_allowed(raw) -> bool | None:
    c = canonicalize_license(raw)
    if not c:
        return None
    if c in _COMMERCIAL_DENIED:
        return False
    if c in _COMMERCIAL_ALLOWED or c in CC_LICENSE_LABELS:
        return True
    return None


def license_description(raw) -> str | None:
    c = canonicalize_license(raw)
    return LICENSE_DESCRIPTIONS.get(c) if c else None


def hub_resource_license(resource: dict | None) -> str | None:
    if not resource:
        return None
    files = resource.get("hubFiles") or []
    t = (files[0].get("licenseType") if files and isinstance(files[0], dict) else None) or resource.get("licenseType")
    return str(t).strip() if t else None
