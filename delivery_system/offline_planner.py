"""Device install planner: pick one representation per block and one dict set.

For every block the device may choose its pure-literal representation or a
dictionary representation.  Dictionary objects are **shared** by all blocks
that use them: their cost is paid once, not once per block.  A plan that
charges the dictionary to every block can pick the wrong encoding, so the
planner enumerates dictionary subsets and charges each dictionary object at
most once.

Constraints, in order:

1. Every object needed by the chosen representations must fit, together with
   the still-protected objects of the currently active book, in the device's
   total capacity.  Unprotected old cache may be deleted; protected objects
   stay until the new version is fully verified and switched.
2. Among capacity-feasible plans, minimise downloaded bytes (objects already
   present with a correct digest are reused and cost no traffic).
3. Finally the download total must not exceed this session's traffic limit.

On identity reuse: a cached object is reusable only when its digest is
correct *and* its identity includes the same dictionary dependency.  Identical
instruction bytes under another dictionary are a different object.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional


class PlanError(Exception):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class Plan:
    choices: dict[str, dict[str, object]]
    download_objects: list[str] = field(default_factory=list)
    reusable_objects: list[str] = field(default_factory=list)
    download_bytes: int = 0
    total_bytes: int = 0
    protected_bytes: int = 0
    used_dictionaries: list[str] = field(default_factory=list)
    capacity: Optional[int] = None
    traffic_limit: Optional[int] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "choices": {
                block: {
                    "object_id": rep["object_id"],
                    "kind": rep["kind"],
                    "dictionary_id": rep.get("dictionary_id"),
                    "dictionary_object_id": rep.get("dictionary_object_id"),
                    "size": rep["size"],
                }
                for block, rep in self.choices.items()
            },
            "download_objects": list(self.download_objects),
            "reusable_objects": list(self.reusable_objects),
            "download_bytes": self.download_bytes,
            "total_bytes": self.total_bytes,
            "protected_bytes": self.protected_bytes,
            "used_dictionaries": list(self.used_dictionaries),
            "capacity": self.capacity,
            "traffic_limit": self.traffic_limit,
        }


def _usable(object_id: str, cached: dict[str, dict[str, object]]) -> bool:
    entry = cached.get(object_id)
    return bool(entry and entry.get("verified"))


def plan_install(
    manifest: dict[str, object],
    *,
    cached: dict[str, dict[str, object]],
    total_capacity: int,
    traffic_limit: Optional[int],
    protected: Optional[dict[str, int]] = None,
    max_bytes_for_planning: Optional[int] = None,
) -> Plan:
    """Return the best capacity-feasible plan, then enforce the traffic limit.

    ``protected`` maps every object id still held by the active book to its
    real on-disk size.  Those objects may belong to an *older* manifest, so
    their sizes cannot be read from ``manifest``; the device supplies them.
    """

    dictionaries = {str(d["object_id"]): d for d in manifest["dictionaries"]}
    dict_ids = sorted(dictionaries)
    sizes = {oid: int(d["size"]) for oid, d in dictionaries.items()}
    for block in manifest["blocks"]:
        for rep in block["representations"]:
            sizes[str(rep["object_id"])] = int(rep["size"])

    protected_sizes: dict[str, int] = {
        str(oid): int(size) for oid, size in (protected or {}).items()
    }
    protected_set = set(protected_sizes)
    protected_bytes = sum(protected_sizes.values())

    blocks = manifest["blocks"]
    best_plan: Optional[Plan] = None
    best_key: Optional[tuple[int, int, tuple[str, ...]]] = None

    # Enumerate which dictionary objects are available to blocks.  A dictionary
    # brought into the store costs once; an already-cached/protected one costs
    # nothing.  Literal representation is always available.
    for r in range(len(dict_ids) + 1):
        for subset in itertools.combinations(dict_ids, r):
            subset_set = set(subset)
            choices: dict[str, dict[str, object]] = {}
            feasible = True
            for block in blocks:
                block_id = str(block["block_id"])
                best_rep = None
                best_rep_key = None
                for rep in block["representations"]:
                    dep = rep.get("dictionary_object_id")
                    if rep["kind"] == "dictionary" and dep not in subset_set:
                        continue
                    oid = str(rep["object_id"])
                    free = _usable(oid, cached)
                    download = 0 if free else int(rep["size"])
                    # Prefer lower download, then a dictionary encoding on ties
                    # (an installed dictionary should actually be used), then
                    # the lower object id for determinism.
                    key = (
                        download,
                        0 if rep["kind"] == "dictionary" else 1,
                        oid,
                    )
                    if best_rep_key is None or key < best_rep_key:
                        best_rep_key = key
                        best_rep = rep
                if best_rep is None:
                    feasible = False
                    break
                choices[block_id] = best_rep
            if not feasible:
                continue

            needed: list[str] = []
            seen: set[str] = set()
            used_dicts = sorted(
                {
                    str(rep["dictionary_object_id"])
                    for rep in choices.values()
                    if rep.get("dictionary_object_id") is not None
                }
            )
            for dep in used_dicts:
                if dep not in seen:
                    seen.add(dep)
                    needed.append(dep)
            for rep in choices.values():
                oid = str(rep["object_id"])
                if oid not in seen:
                    seen.add(oid)
                    needed.append(oid)

            final = protected_set | set(needed)
            total_bytes = sum(
                protected_sizes.get(oid, sizes.get(oid, 0)) for oid in final
            )
            if total_bytes > total_capacity:
                continue

            download = [oid for oid in needed if not _usable(oid, cached)]
            download_bytes = sum(sizes[oid] for oid in download)
            if max_bytes_for_planning is not None and download_bytes > max_bytes_for_planning:
                continue

            plan = Plan(
                choices=choices,
                download_objects=download,
                reusable_objects=[oid for oid in needed if _usable(oid, cached)],
                download_bytes=download_bytes,
                total_bytes=total_bytes,
                protected_bytes=protected_bytes,
                used_dictionaries=used_dicts,
                capacity=total_capacity,
                traffic_limit=traffic_limit,
            )
            key = (download_bytes, total_bytes, tuple(sorted(choices)))
            if best_key is None or key < best_key:
                best_key = key
                best_plan = plan

    if best_plan is None:
        raise PlanError(
            "no plan fits the device capacity while keeping the active book "
            "protected (atomic update has insufficient space)",
            "capacity_exceeded",
        )

    if traffic_limit is not None and best_plan.download_bytes > traffic_limit:
        raise PlanError(
            f"best plan downloads {best_plan.download_bytes} bytes but this "
            f"session allows {traffic_limit}",
            "traffic_limit_exceeded",
        )
    return best_plan
