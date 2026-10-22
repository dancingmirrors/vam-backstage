from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vam-backstage", description="Manage a Virt-a-Mate .var library.")
    parser.add_argument("--data-dir", help="where the database and caches live (default: per-user data dir)")
    parser.add_argument("--vam-dir", help="VaM installation to use (skips detection)")
    parser.add_argument("--log-level", default=os.environ.get("VAM_BACKSTAGE_LOG", "INFO"))
    parser.add_argument("--quit-after", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    parser.add_argument("--debug-view", help=argparse.SUPPRESS)
    parser.add_argument("--debug-select", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--debug-fake-hub", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-scroll", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--debug-status", help=argparse.SUPPRESS)
    parser.add_argument("--debug-search", help=argparse.SUPPRESS)
    parser.add_argument("--debug-action", help=argparse.SUPPRESS)
    parser.add_argument(
        "--scan", action="store_true", help="scan the library from the command line and exit (no window)"
    )
    parser.add_argument(
        "--dummy",
        nargs="+",
        metavar="REF",
        help="create empty dummy packages that satisfy these references (e.g. Creator.Package.3) and exit",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify every package (decompress all entries, compare CRC32s) and exit; status 1 if any is corrupted",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from .backend import Backend

    transport = None
    if args.debug_fake_hub:
        from .devtools.fakehub import FakeHub, FakeTransport, populate_demo_hub

        transport = FakeTransport()
        fake_hub = FakeHub(transport)
        populate_demo_hub(transport, fake_hub)
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        backend = Backend(db_path=data_dir / "backstage.db", cache_dir=data_dir / "cache", transport=transport)
    else:
        backend = Backend(transport=transport)
    if args.vam_dir:
        backend.lib.set_vam_dir(str(Path(args.vam_dir).expanduser()))
    if args.debug_fake_hub:
        fake_hub.catalog_source = lambda: dict(backend.lib.store.package_index)
    backend.start()

    if args.scan:
        if not backend.lib.is_configured:
            print("No VaM directory configured; pass --vam-dir.", file=sys.stderr)
            return 2

        def progress(p):
            if p.get("total"):
                print(f"\r[{p['phase']}] {p.get('message', '')[:60]:<60} {p['step']}/{p['total']}", end="", flush=True)

        summary = backend.lib.scan(progress)
        print()
        s = backend.lib.stats()
        print(
            f"scanned={summary.scanned} added={summary.added} removed={summary.removed} unreadable={len(summary.unreadable)}"
        )
        print(
            f"packages={s['total_count']} direct={s['direct_count']} deps={s['dep_count']} content={s['total_content']} missing refs={s['missing_dep_count']}"
        )
        backend.close()
        return 0

    if args.dummy:
        if not backend.lib.is_configured:
            print("No VaM directory configured; pass --vam-dir.", file=sys.stderr)
            return 2
        res = backend.lib.create_dummies(args.dummy)
        for fn in res["created"]:
            print(f"created {fn}")
        for ref, why in res["skipped"]:
            print(f"skipped {ref}: {why}", file=sys.stderr)
        backend.close()
        return 0 if res["created"] or not res["skipped"] else 1

    if args.verify:
        if not backend.lib.is_configured:
            print("No VaM directory configured; pass --vam-dir.", file=sys.stderr)
            return 2
        if not backend.lib.stats()["total_count"]:
            print("Nothing indexed yet; run with --scan first.", file=sys.stderr)
            return 2

        def verify_progress(p):
            if p.get("status") == "checking":
                print(f"\r[{p['step']}/{p['total']}] {p['filename'][:60]:<60}", end="", file=sys.stderr, flush=True)

        res = backend.lib.integrity_check(verify_progress)
        print(file=sys.stderr)
        for fn in res["corrupted_files"]:
            print(f"CORRUPTED {fn}: {res['errors'].get(fn, '')}")
        print(
            f"checked={res['checked']} of {res['total']} corrupted={res['corrupted']} bytes={res['bytes']} seconds={res['seconds']:.1f}"
        )
        backend.close()
        return 1 if res["corrupted"] else 0

    from .ui.app import App

    sys.setswitchinterval(0.0005)
    app = App(
        backend,
        quit_after=args.quit_after,
        screenshot=args.screenshot,
        debug_view=args.debug_view,
        debug_select=args.debug_select,
        debug_scroll=args.debug_scroll,
        debug_status=args.debug_status,
        debug_search=args.debug_search,
        debug_action=args.debug_action,
    )
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
