"""Stand-alone device worker (runs in its own process).

It receives only a device root (its object cache + download log), a bundle
directory containing ``manifest.json`` and object files, and budgets.  It never
opens the publisher's source or delivery database, and it decodes from files
it verified itself.

Usage::

    python -m delivery_system.offline_worker plan    --device D --bundle B --capacity N [--traffic N]
    python -m delivery_system.offline_worker install --device D --bundle B --capacity N [--traffic N] [--crash]
    python -m delivery_system.offline_worker read    --device D
    python -m delivery_system.offline_worker space   --device D
    python -m delivery_system.offline_worker clear-cache --device D

Every command prints one JSON object and exits ``0`` on success.  Domain
failures print ``{"ok": false, "reason": ...}`` and exit ``3``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .offline_bundle import BundleError, read_manifest
from .offline_device import DeviceError, ReaderDevice
from .offline_planner import PlanError


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="offline_worker")
    parser.add_argument("command", choices=["plan", "install", "read", "space", "clear-cache"])
    parser.add_argument("--device", required=True)
    parser.add_argument("--bundle", default=None)
    parser.add_argument("--capacity", type=int, default=None)
    parser.add_argument("--traffic", type=int, default=None)
    parser.add_argument("--max-bytes", type=int, default=None)
    parser.add_argument("--crash", action="store_true", help="simulate power loss before switch")
    args = parser.parse_args(argv)

    device = ReaderDevice(args.device)
    try:
        if args.command == "plan":
            manifest = read_manifest(_manifest_path(args))
            plan = device.plan(
                manifest,
                total_capacity=_capacity(args),
                traffic_limit=args.traffic,
                max_bytes_for_planning=args.max_bytes,
            )
            _emit({"ok": True, "plan": plan.to_dict()})
            return 0
        if args.command == "install":
            manifest = read_manifest(_manifest_path(args))
            report = device.install(
                manifest,
                args.bundle,
                total_capacity=_capacity(args),
                traffic_limit=args.traffic,
                simulate_crash=args.crash,
                manifest_source=_manifest_path(args),
            )
            _emit({"ok": True, "report": report})
            return 0
        if args.command == "read":
            _emit({"ok": True, "book": device.read()})
            return 0
        if args.command == "space":
            _emit({"ok": True, "space": device.space_report(), "download_total": device.download_total()})
            return 0
        if args.command == "clear-cache":
            device.clear_cache()
            _emit({"ok": True, "space": device.space_report()})
            return 0
    except (DeviceError, PlanError) as exc:
        reason = getattr(exc, "reason", "device_error")
        _emit({"ok": False, "reason": reason, "error": str(exc)})
        return 3
    except (BundleError, OSError, ValueError, KeyError) as exc:
        _emit({"ok": False, "reason": "bad_bundle", "error": str(exc)})
        return 3
    parser.error("unreachable")  # pragma: no cover
    return 2


def _manifest_path(args: argparse.Namespace) -> str:
    if not args.bundle:
        raise DeviceError("--bundle is required for this command")
    import os

    return os.path.join(args.bundle, "manifest.json")


def _capacity(args: argparse.Namespace) -> int:
    if args.capacity is None:
        raise DeviceError("--capacity is required for this command")
    return args.capacity


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
