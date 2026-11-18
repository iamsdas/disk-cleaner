"""List every cache location in the catalog, instantly.

Sizing all ~25 directories in one call blocks the page until the slowest one
finishes (and a huge ~/.cache can push that past the runPython limit). So this
file never walks anything: it returns each entry with whether it exists and,
when the per-path size cache (shared with size_dir.py) has a fresh number,
that size. Entries without one come back `pending: True`; the page sizes them
one call each via size_dir.py and fills the rows in as they land.
"""

import os

import catalog


def main(refresh: bool = False, max_age: float = 900.0, cached_only: bool = False):
    entries = []
    for entry in catalog.catalog_for_os():
        out = catalog.fmt_entry(entry)
        abs_path = out["abs"]
        if not abs_path or not os.path.isdir(abs_path):
            out.update(exists=False, size=0, files=0, truncated=False, pending=False)
        else:
            hit = None if refresh else catalog.read_size_cache(
                abs_path, float("inf") if cached_only else max_age)
            if hit:
                out.update(exists=True, size=hit["size"], files=hit["files"],
                           truncated=hit["truncated"], pending=False)
            else:
                out.update(exists=True, size=0, files=0, truncated=False, pending=True)
        entries.append(out)

    # A nested entry's bytes are already inside its ancestor's size; the page
    # skips entries with `nested_under` set when it totals sizes.
    catalog.mark_nested(entries)
    return {"entries": entries, "os": catalog.OS}
