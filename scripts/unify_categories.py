#!/usr/bin/env python3
"""Reuse stored unified titles for exact cross-bank MCC matches."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Sequence

if __package__:
    from . import cashbacks
else:
    import cashbacks


ALL_PURCHASES = "Все покупки"

#: Platform-ecosystem MCCs ignored when comparing `include_mcc` sets
#: (Yandex 3990, Sber 3991, 3992). See UNIFIED_CATEGORIES.md.
ECOSYSTEM_MCC = frozenset({3990, 3991, 3992})


def comparable_mcc(mcc: Sequence[int]) -> frozenset[int] | None:
    """The include set used for matching, with ecosystem MCCs removed.

    Returns None when nothing remains; such rules do not participate in
    matching, so an ecosystem-only set never matches another one.
    """
    reduced = frozenset(mcc) - ECOSYSTEM_MCC
    return reduced if reduced else None


@dataclass(frozen=True)
class Rule:
    path: Path
    bank: str
    entry: dict[str, Any]
    original: bytes

    @property
    def title(self) -> str | None:
        return self.entry.get("unified_category")

    def evidence(self) -> dict[str, Any]:
        return {
            "bank": self.bank,
            "path": self.path.as_posix(),
            "category": self.entry["category"],
            "unified_category": self.title,
        }


def load_rules(root: Path) -> tuple[list[Rule], dict[str, str]]:
    repository = cashbacks.load_repository(root)
    rules = []
    bank_selectors = {}
    for source in [*repository.banks, *repository.pending_banks]:
        bank = source.path.parent.name
        bank_selectors[bank] = bank
        if isinstance(source, cashbacks.BankSource):
            bank_selectors[str(source.bank_id)] = bank
        for entry in source.entries:
            category = entry["category"]
            first_alias = category if isinstance(category, str) else category[0]
            path = source.path / cashbacks._canonical_category_filename(first_alias)
            original = path.read_bytes()
            if json.loads(original) != entry:
                raise cashbacks.DataError(f"{path}: source changed while loading; rerun")
            rules.append(Rule(path.relative_to(root), bank, entry, original))
    return sorted(rules, key=lambda rule: rule.path.as_posix()), bank_selectors


def plan_changes(rules: list[Rule], banks: set[str]) -> dict[str, Any]:
    by_mcc: dict[frozenset[int], list[Rule]] = defaultdict(list)
    for rule in rules:
        if "include_mcc" in rule.entry:
            key = comparable_mcc(rule.entry["include_mcc"])
            if key is not None:
                by_mcc[key].append(rule)

    conflicts = []
    conflicting_mcc = set()
    for mcc, members in sorted(by_mcc.items(), key=lambda item: sorted(item[0])):
        titles = {member.title for member in members if member.title is not None}
        if len(titles) > 1:
            conflicting_mcc.add(mcc)
            if any(member.bank in banks for member in members):
                conflicts.append({
                    "include_mcc": sorted(mcc),
                    "reason": "different_titles_for_exact_mcc",
                    "rules": [member.evidence() for member in members],
                })

    changes = []
    unresolved = []
    for rule in rules:
        if rule.bank not in banks:
            continue
        if "exclude_mcc" in rule.entry:
            if rule.title is not None and rule.title != ALL_PURCHASES:
                conflicts.append({
                    "reason": "exclude_title_must_be_all_purchases",
                    "expected": ALL_PURCHASES,
                    "rules": [rule.evidence()],
                })
            elif rule.title is None:
                changes.append({
                    "path": rule.path.as_posix(),
                    "unified_category": ALL_PURCHASES,
                    "reason": "exclude_mcc",
                    "sources": [],
                })
            continue

        mcc = comparable_mcc(rule.entry["include_mcc"])
        if mcc is None or mcc in conflicting_mcc or rule.title is not None:
            continue
        donors = [
            member for member in by_mcc[mcc]
            if member.bank != rule.bank and member.title is not None
        ]
        if not donors:
            unresolved.append({
                **rule.evidence(),
                "include_mcc": sorted(mcc),
                "reason": "no_named_cross_bank_match",
                "matches": [
                    member.evidence() for member in by_mcc[mcc]
                    if member.bank != rule.bank
                ],
            })
            continue
        changes.append({
            "path": rule.path.as_posix(),
            "unified_category": donors[0].title,
            "reason": "exact_cross_bank_mcc",
            "sources": [donor.path.as_posix() for donor in donors],
        })
    return {"changes": changes, "conflicts": conflicts, "unresolved": unresolved}


def build_catalog(rules: list[Rule]) -> list[dict[str, Any]]:
    """A generated view of stored titles, not another source of truth."""
    titles: dict[str, dict[tuple[str, tuple[int, ...]], list[Rule]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for rule in rules:
        if rule.title is None:
            continue
        selector = "include_mcc" if "include_mcc" in rule.entry else "exclude_mcc"
        titles[rule.title][(selector, tuple(rule.entry[selector]))].append(rule)
    return [
        {
            "unified_category": title,
            "groups": [
                {selector: list(mcc), "rules": [rule.evidence() for rule in members]}
                for (selector, mcc), members in sorted(groups.items())
            ],
        }
        for title, groups in sorted(titles.items())
    ]


def write_changes(root: Path, rules: list[Rule], changes: list[dict[str, Any]]) -> None:
    # Check the entire snapshot before writing; donors must still match the report.
    for rule in rules:
        path = root / rule.path
        if path.is_symlink() or path.read_bytes() != rule.original:
            raise cashbacks.DataError(f"{rule.path}: source changed since planning; rerun")
    by_path = {rule.path.as_posix(): rule for rule in rules}
    for change in changes:
        rule = by_path[change["path"]]
        entry = {
            "category": rule.entry["category"],
            "unified_category": change["unified_category"],
            **{key: value for key, value in rule.entry.items() if key != "category"},
        }
        (root / rule.path).write_text(
            json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    cashbacks.load_repository(root)


def main(argv: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bank", action="append", default=[], metavar="ID_OR_DIRECTORY",
        help="limit targets by bank ID or directory name; repeatable; default: all banks",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply missing titles if targets have no conflicts")
    mode.add_argument("--catalog", action="store_true", help="print all stored titles grouped by MCC and bank")
    args = parser.parse_args(argv)
    if args.catalog and args.bank:
        parser.error("--catalog describes the whole repository; omit --bank")
    root = cashbacks.ROOT if root is None else Path(root)
    try:
        rules, selectors = load_rules(root)
        unknown = sorted(set(args.bank) - selectors.keys())
        if unknown:
            raise cashbacks.DataError(f"unknown bank selector(s): {', '.join(unknown)}")
        if args.catalog:
            print(json.dumps(build_catalog(rules), ensure_ascii=False, indent=2))
            return 0
        banks = {selectors[value] for value in args.bank} if args.bank else set(selectors.values())
        report = plan_changes(rules, banks)
        written = 0
        if args.write and not report["conflicts"]:
            write_changes(root, rules, report["changes"])
            written = len(report["changes"])
        print(json.dumps({
            "mode": "write" if args.write else "dry_run",
            "banks": sorted(banks),
            "written": written,
            **report,
        }, ensure_ascii=False, indent=2))
        return 2 if report["conflicts"] else 0
    except (cashbacks.DataError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
