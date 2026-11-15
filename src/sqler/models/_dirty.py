"""Dirty tracking: the snapshot a model keeps of its row, and what changed since.

A model that was loaded or saved keeps ``_snapshot``: the document it was built from or
written from, without ``_id`` and ``_version``, its lists and dicts copied so that it shares
none with the model. :func:`is_dirty` is False when ``save()`` would write what it would have
written then: the model's own encoding for ``save()`` is compared with the snapshot, which is
the row the database held.
"""

from __future__ import annotations

from typing import Any

_SCALARS = (str, int, float, bool, type(None))


def snapshot(doc: dict) -> dict:
    """The snapshot of ``doc``: the row a model was loaded from, or the payload it was saved
    with. ``_id`` and ``_version`` are left out and each list and dict is copied, so that the
    snapshot shares none with the model. It holds no model: a row and a payload hold none.
    """
    return {
        k: v if type(v) in _SCALARS else _frozen(v)
        for k, v in doc.items()
        if k not in {"_id", "_version"}
    }


def _frozen(value: Any) -> Any:
    t = type(value)
    if t is list:
        return [_frozen(v) for v in value]
    if t is dict:
        return {k: _frozen(v) for k, v in value.items()}
    return value


def is_dirty(self: Any) -> bool:
    """True when ``save()`` would write something other than it would have written when this
    model was loaded or last saved; True for a model never loaded or saved."""
    return self._snapshot is None or bool(get_dirty_fields(self))


def get_dirty_fields(self: Any) -> set[str]:
    """Names of the fields ``save()`` would write differently than when this model was loaded
    or last saved; every field for a model never loaded or saved. A key of the row that the
    class has no field for is named too: ``save()`` writes the whole document and drops it."""
    snap = self._snapshot
    if snap is None:
        return set(type(self).model_fields)
    try:
        now = self._dump_with_relations()
    except ValueError:  # it holds a related model never saved, which no row can hold
        return set(type(self).model_fields)
    return changed_fields(snap, now)


def changed_fields(snap: dict, now: dict) -> set[str]:
    """Names of the keys whose value in the payload ``now`` differs from the snapshot
    ``snap``, and the keys of ``snap`` that ``now`` does not have."""
    # ``is`` first, as a list or dict compares its items: a NaN kept is not a change
    changed = {k for k, v in now.items() if k not in snap or not (snap[k] is v or snap[k] == v)}
    return changed | (snap.keys() - now.keys())
