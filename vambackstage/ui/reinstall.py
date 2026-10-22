from __future__ import annotations

import re

from gi.repository import Adw, Gtk, Pango

from ..core.paths import LOCAL_PACKAGE_DISPLAY_NAME, is_local_package
from .models import human_size
from .widgets import chip, copy_button, hbox, label, vbox

MAX_PER_SECTION = 200

_VERSION_RE = re.compile(r"\.(\d+)\.var$", re.IGNORECASE)

_REASONS = {
    "not_on_hub": "the Hub does not have this version",
    "no_download": "the Hub lists it without a download (paid, or hosted elsewhere)",
    "lookup_failed": "the Hub did not answer; try again later",
    "invalid": "not a package reference",
    "unpacked": "unpacked, so your edits stay; reinstall it on its own to replace the folder",
}

SECTIONS = (
    (
        "requested",
        "Versions you are missing",
        "Installed next to the versions you have, so each gets the one it names.",
    ),
    (
        "substitute",
        "Another version instead",
        "The Hub no longer has the version named and you have none of the package: the Hub's version is installed.",
    ),
    ("unavailable", "Not available from the Hub", "Whatever stands in for these now stays as it is."),
    ("kept", "Kept as they are", "Installed, but they cannot be downloaded again."),
    (
        "again",
        "Downloaded again",
        "Each replaces its file once the download verifies; folder, state and settings stay.",
    ),
)


def _version(filename: str | None) -> str:
    m = _VERSION_RE.search(filename or "")
    return f"V{m.group(1)}" if m else (filename or "")


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def describe_ref(row: dict, files: dict[str, dict]) -> tuple[str, str, str]:
    stand_in = row.get("stand_in")
    if row["action"] == "install":
        v = _version(row["filename"])
        if row["kind"] == "substitute":
            return f"{v} INSTEAD", "warn", f"the Hub has {v.lower()}, not the version named"
        if row.get("stand_in_kind") == "placeholder":
            if stand_in.lower() == row["filename"].lower():
                return f"INSTALL {v}", "success", f"replaces the stand-in {stand_in}"
            return f"INSTALL {v}", "success", f"used instead of the stand-in {stand_in}, which stays"
        if stand_in:
            return f"INSTALL {v}", "success", f"{_version(stand_in).lower()} is used instead now; it stays for the rest"
        return f"INSTALL {v}", "success", "not installed now"
    chip_text, style = {
        "no_download": ("NO DOWNLOAD", "warn"),
        "lookup_failed": ("NO ANSWER", "warn"),
        "invalid": ("INVALID", "error"),
    }.get(row.get("reason"), ("NOT ON THE HUB", "error"))
    why = _REASONS.get(row.get("reason"), _REASONS["not_on_hub"])
    if row.get("reason") == "not_on_hub" and row["ref"].lower().endswith(".latest"):
        why = "the Hub does not have this package"
    if row.get("offers"):
        why += f" (it has {_version(row['offers']).lower()})"
    if row.get("stand_in_kind") == "fallback":
        again = (files.get(stand_in) or {}).get("action") == "reinstall"
        then = f"{_version(stand_in).lower()} stays in use" + (" and is reinstalled" if again else "")
    elif stand_in:
        then = f"the stand-in {stand_in} stays"
    else:
        then = "it stays missing"
    return chip_text, style, f"{why}; {then}"


def describe_file(row: dict) -> tuple[str, str, str]:
    if row["action"] == "reinstall":
        if row.get("is_placeholder"):
            return "FETCH", "success", "the real package replaces the stand-in"
        return "REINSTALL", "muted", ""
    return "KEPT", "muted", _REASONS.get(row.get("reason"), _REASONS["not_on_hub"])


def plan_sections(plan: dict) -> dict[str, list[dict]]:
    refs = sorted(plan["refs"], key=lambda r: r["ref"].lower())
    installs = [r for r in refs if r["action"] == "install"]
    fetched = [f for f in plan["files"] if f["action"] == "reinstall"]
    covered = {r["filename"].lower() for r in installs}
    stand_ins = [f for f in fetched if f["is_placeholder"] and f["filename"].lower() not in covered]
    return {
        "requested": [r for r in installs if r["kind"] == "requested"] + stand_ins,
        "substitute": [r for r in installs if r["kind"] == "substitute"],
        "unavailable": [r for r in refs if r["action"] == "unavailable"],
        "kept": sorted((f for f in plan["files"] if f["action"] == "keep"), key=lambda f: f["filename"].lower()),
        "again": [f for f in fetched if not f["is_placeholder"]],
    }


def plan_summary(plan: dict) -> str:
    downloads = plan["downloads"]
    if downloads:
        size = human_size(plan["total_size"]) + (" or more" if any(not s["file_size"] for s in downloads) else "")
        text = (
            f"{_count(len(downloads), 'download')}, {size}. Each package gets the version it names where the Hub "
            "still has it; what the Hub no longer has is skipped. A failed download leaves the local file as it is."
        )
    else:
        text = "There is nothing to download."
    if plan.get("lookup_failed"):
        text += f" The Hub did not answer for {_count(plan['lookup_failed'], 'name')}; running this again later picks them up."
    return text


def _line(name: str, chip_text: str, style: str, note: str, needed_by: list[str]) -> Gtk.Widget:
    box = vbox(1)
    box.add_css_class("vb-dep-row")
    top = hbox(8)
    name_lbl = label(name, "vb-mono")
    name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    name_lbl.set_hexpand(True)
    who = [LOCAL_PACKAGE_DISPLAY_NAME if is_local_package(n) else n for n in needed_by]
    if who:
        more = f" and {len(who) - 12} more" if len(who) > 12 else ""
        name_lbl.set_tooltip_text(f"{name}\nNeeded by {', '.join(who[:12])}{more}")
    top.append(name_lbl)
    top.append(copy_button(name))
    c = chip(chip_text, style)
    c.set_valign(Gtk.Align.CENTER)
    top.append(c)
    box.append(top)
    if note:
        box.append(label(note, "vb-dim vb-small", wrap=True))
    return box


def plan_listing(plan: dict) -> Gtk.Widget:
    files = {f["filename"]: f for f in plan["files"]}
    listing = vbox(2)
    listing.set_size_request(700, -1)
    sections = plan_sections(plan)
    for key, title, blurb in SECTIONS:
        rows = sections[key]
        if not rows:
            continue
        listing.append(label(f"{title.upper()} ({len(rows)})", "vb-section-label"))
        listing.append(label(blurb, "vb-dim vb-small", wrap=True))
        for r in rows[:MAX_PER_SECTION]:
            if "ref" in r:
                listing.append(_line(r["ref"], *describe_ref(r, files), r["needed_by"]))
            else:
                listing.append(_line(r["filename"], *describe_file(r), r["needed_by"]))
        if len(rows) > MAX_PER_SECTION:
            listing.append(label(f"… and {len(rows) - MAX_PER_SECTION} more", "vb-dim vb-small"))
    return Gtk.ScrolledWindow(
        child=listing, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=360
    )


def present_plan(window, plan: dict, heading: str) -> None:
    downloads = plan["downloads"]
    missing = sum(1 for s in downloads if not s["reinstall"])
    dialog = Adw.AlertDialog(heading=heading, body=plan_summary(plan))
    if plan["refs"] or plan["files"]:
        dialog.set_extra_child(plan_listing(plan))
    dialog.add_response("cancel", "Cancel" if downloads else "Close")
    if downloads:
        if missing and missing < len(downloads):
            dialog.add_response("missing", f"Only the {missing} missing")
        dialog.add_response("all", f"Download {len(downloads)}")
        dialog.set_response_appearance("all", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("all")
    dialog.set_close_response("cancel")

    def report(res: dict) -> None:
        n = len(res["queued"])
        msg = f"Queued {_count(n, 'download')}" if n else "Nothing new to queue"
        if res["skipped"]:
            msg += f"; {_count(len(res['skipped']), 'file')} already queued or installed"
        if res.get("paused"):
            msg += ". Downloads are paused"
        window.toast(msg, 8, button=("Show", lambda: window.stack.set_visible_child_name("downloads")))

    def on_response(_d, r):
        if r in ("all", "missing"):
            window.run_op(window.backend.downloads.enqueue_reinstall_plan, plan, r == "missing", then=report)

    dialog.connect("response", on_response)
    dialog.present(window)
