# Unified category rules

Public bank sources substantiate source categories and MCCs. `unified_category` is a repository-curated title inherited under the rules below. Choosing a new title, changing an existing title, or removing one requires maintainer review.

Titles are stored only in the source JSON files' `unified_category` fields. Each run rebuilds an index of those values; it does not generate names. There is no separately maintained dictionary.

- For `include_mcc`, inherit a title only from an exactly equal MCC set in another bank with a stored `unified_category`. All populated titles for that set must agree, including rules in the target bank. Text similarity, subsets, and the 90% heuristic are not used.
- Platform-ecosystem MCCs 3990 (Yandex), 3991 (Sber), and 3992 are ignored when comparing `include_mcc` sets: sets are compared after removing them, so a 5-code set matches the same 5 codes plus any of 3990–3992. A rule whose set consists only of ecosystem MCCs does not participate in matching and can neither inherit nor donate a title.
- For `exclude_mcc`, use `Все покупки` regardless of the exclusion list.
- Without an established title, leave the field absent. A new exact cross-bank cluster can receive a title once through review, recorded in the relevant JSON files. Rules without a shared cluster do not need an invented title.
- Different MCC sets may share a unified title. Titles are not MCC-set identities.
- Both active and pending banks participate; each bank directory represents a separate bank. The script never overwrites stored values. When changing MCCs, check that the existing title still fits.

Commands for agents and maintainers, from the repository root:

```bash
python3 scripts/unify_categories.py --bank 5044
python3 scripts/unify_categories.py --bank 5044 --write
python3 scripts/unify_categories.py --catalog
```

Without `--write`, the command prints only a JSON plan: `changes` with title-source paths, `conflicts` with contradictory rules, and `unresolved` rules lacking an established cross-bank match. `--bank` accepts an ID or a full bank directory name and is repeatable; omitting it selects all banks. Donors always come from the whole repository. Use a directory name for a pending bank, e.g. `--bank 'New bank-ru_'`.

`--write` only adds missing fields in selected banks. A conflict involving selected rules prevents all writes in that invocation. Source `category` aliases, MCCs, selector modes, and filenames are preserved. Unresolved categories are skipped. Rerunning leaves populated rules unchanged.

`--catalog` prints all stored titles grouped by MCC set and source bank rules, keeping `include_mcc` and `exclude_mcc` separate. It cannot be combined with `--bank` or `--write`; it is a generated view, not another editable source of truth.

Exit codes: `0` for a successful preview/write/catalog (`unresolved` may remain), `1` for loading or validation errors, `2` for title conflicts or invalid CLI arguments. After changes, run `python3 scripts/cashbacks.py validate` and inspect the diff; current CI validates the data format but does not require every `unified_category` to be populated.
