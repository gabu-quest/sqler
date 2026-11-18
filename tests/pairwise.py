"""All pairs of three or more ``parametrize`` axes, in place of their full product.

``@pytest.mark.parametrize(*all_pairs(("way", WAYS), ("table,rid", BAD), ("key", KEYS)))``
gives cases in which every value of every axis meets every value of every other axis at
least once. An axis is ``(argnames, values)`` or ``(argnames, values, ids)``; a value may be
a ``pytest.param``, whose id and marks every case that holds it keeps. A case that is a
promise of its own (one combination of three values) is added to the returned list by hand.

The cases are built in a fixed order (IPOG: the product of the two largest axes, then each
next axis spread over those rows, and the pairs still missing given rows of their own);
nothing in it is random. It checks its own pairs, so a wrong list fails at collection.
"""

from itertools import combinations

import pytest

_PARAM = type(pytest.param())


def _value(argnames: str, value, index: int, given) -> tuple:
    """``(values, id, marks)`` of one value of an axis, the id as pytest would make it."""
    if isinstance(value, _PARAM):
        values, given, marks = value.values, value.id or given, tuple(value.marks)
    else:
        values, marks = (tuple(value) if "," in argnames else (value,)), ()
    if given is None:
        parts = [v.encode("unicode_escape").decode() if isinstance(v, str) else v for v in values]
        if all(isinstance(v, (str, int, float, bool, type(None))) for v in parts):
            given = "-".join(map(str, parts))
        else:
            given = f"{argnames.split(',')[0].strip()}{index}"
    return values, given, marks


def all_pairs(*axes) -> tuple[str, list]:
    cols = []
    for names, values, *ids in axes:
        ids = ids[0] if ids else [None] * len(values)
        cols.append(
            [_value(names, v, i, g) for i, (v, g) in enumerate(zip(values, ids, strict=True))]
        )
    size = [len(col) for col in cols]
    order = sorted(range(len(cols)), key=lambda a: -size[a])
    rows = [
        {order[0]: i, order[1]: j} for i in range(size[order[0]]) for j in range(size[order[1]])
    ]
    for pos, k in enumerate(order[2:], 2):
        need = {(a, v, w) for a in order[:pos] for v in range(size[a]) for w in range(size[k])}
        for row in rows:
            row[k] = max(
                range(size[k]), key=lambda w: (sum((a, row[a], w) in need for a in row), -w)
            )
            need -= {(a, row[a], row[k]) for a in row if a != k}
        for a, v, w in sorted(need):
            free = next((r for r in rows if r.get(k) == w and a not in r), None)
            if free is None:
                rows.append({a: v, k: w})
            else:
                free[a] = v
    cases = [tuple(row.get(a, 0) for a in range(len(cols))) for row in rows]
    for a, b in combinations(range(len(cols)), 2):
        assert {(c[a], c[b]) for c in cases} == {
            (i, j) for i in range(size[a]) for j in range(size[b])
        }
    params = []
    for case in cases:
        held = [cols[a][i] for a, i in enumerate(case)]
        values = [x for h in held for x in h[0]]
        params.append(
            pytest.param(
                *values, id="-".join(h[1] for h in held), marks=[m for h in held for m in h[2]]
            )
        )
    return ",".join(names for names, *_ in axes), params
