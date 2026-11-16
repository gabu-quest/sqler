"""Data export and import utilities for SQLer.

Supports CSV, JSON, and JSONL formats for data interchange,
migration, and backup purposes.

Usage::

    from sqler.export import export_csv, export_jsonl, import_csv

    # Export query results
    users = User.query().filter(F.active == True)
    export_csv(users, "active_users.csv")
    export_jsonl(users, "active_users.jsonl")

    # Export entire table
    export_json(User, "all_users.json", indent=2)

    # Import data
    imported = import_csv(User, "users.csv")
    print(f"Imported {len(imported)} users")
"""

import asyncio
import csv
import io
import json
import types
import typing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterator,
    Literal,
    Optional,
    Type,
    Union,
)

from sqler import _stored
from sqler.models._compat import field_types

if TYPE_CHECKING:
    from sqler.db.async_db import AsyncSQLerDB
    from sqler.db.sqler_db import SQLerDB
    from sqler.models import AsyncSQLerModel, SQLerModel
    from sqler.models.async_queryset import AsyncSQLerQuerySet
    from sqler.models.queryset import SQLerQuerySet

    # a queryset of a model, sync or async
    ModelQuerySet = Union[SQLerQuerySet[Any], AsyncSQLerQuerySet[Any]]


@dataclass
class ExportResult:
    """Result of an export operation."""

    path: str
    format: str
    count: int
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "count": self.count,
            "size_bytes": self.size_bytes,
        }


@dataclass
class ImportResult:
    """Result of an import operation."""

    count: int
    succeeded: int
    failed: int
    errors: list[dict[str, Any]]

    @property
    def success_count(self) -> int:
        """Alias for succeeded."""
        return self.succeeded

    @property
    def error_count(self) -> int:
        """Alias for failed."""
        return self.failed

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.count if self.count > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": self.success_rate,
            "errors": self.errors,
        }


# A CSV cell that starts with one of these is read as a formula by a spreadsheet
_CSV_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _csv_looks_escaped(value: str) -> bool:
    """True if ``value`` is what :func:`_csv_escape_string` produces from some string.

    That is one or more apostrophes followed by nothing or by a formula character.
    The one rule both directions use: export adds an apostrophe to a string that
    already looks escaped, import removes one only from a cell that does.
    """
    stripped = value.lstrip("'")
    if stripped == value:
        return False
    return stripped == "" or stripped[0] in _CSV_FORMULA_STARTS


def _csv_escape_string(value: str) -> str:
    """Make a string safe to write into a CSV cell, reversibly.

    A leading ``'`` is added to the empty string (so it is not read back as a
    missing value), to a string that starts with a formula character, and to a
    string that already looks escaped (see :func:`_csv_looks_escaped`). Any other
    string, ``'90s`` and ``it's`` among them, is written as it is.
    """
    if value == "" or value[0] in _CSV_FORMULA_STARTS or _csv_looks_escaped(value):
        return "'" + value
    return value


def _csv_unescape_string(value: str) -> str:
    """Undo :func:`_csv_escape_string`: remove one leading ``'`` from a cell that looks escaped."""
    return value[1:] if _csv_looks_escaped(value) else value


def _serialize_value(value: Any, for_csv: bool = False) -> Any:
    """Serialize a value for export.

    Args:
        value: Value to serialize
        for_csv: If True, serialize lists/dicts to JSON strings AND escape
            strings so a spreadsheet shows no formula: a string that starts
            with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return gets a
            leading single quote (the standard spreadsheet escape), and so do
            the empty string and a string that already looks escaped, so
            ``import_csv`` returns every string unchanged.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if for_csv and isinstance(value, (list, dict)):
        return json.dumps(value)
    if for_csv and isinstance(value, str):
        return _csv_escape_string(value)
    return value


def _admits_none(field_type: Any) -> bool:
    """Whether a field of ``field_type`` can hold ``None``: ``Any``, ``None``, ``object``, a
    union with ``None`` in it, and ``Annotated`` of one of these."""
    if field_type in (Any, None, type(None), object):
        return True
    origin = typing.get_origin(field_type)
    args = typing.get_args(field_type)
    if origin is typing.Annotated:
        return _admits_none(args[0])
    if origin is Union or origin is types.UnionType:
        return any(_admits_none(a) for a in args)
    return False


def _deserialize_value(value: Optional[str], field_type: Any) -> Any:
    """The value of a field of type ``field_type`` that the CSV cell ``value`` holds.

    An empty cell is ``None``. ``int``, ``float`` and ``bool`` are read from their text; a
    field whose stored form is a list, a dict or another number from the JSON text the
    export writes; any other cell is text and loses the one leading ``'`` that the CSV
    export adds (see :func:`_csv_escape_string`). The value is then the type the field
    declares, as a load gives it (``_stored.loaded``: a ``datetime`` from its ISO text).
    """
    if value == "" or value is None:
        return None
    kind = _stored.one_type(field_type)
    if kind is bool:
        return value.lower() in ("true", "1", "yes")
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if _stored.as_json(kind):
        try:
            return _stored.loaded(json.loads(value), field_type)
        except json.JSONDecodeError:
            pass  # not JSON: the cell is the text it holds
    return _stored.loaded(_csv_unescape_string(value), field_type)


def _typed(model_class: Any, record: dict) -> dict:
    """``record``, a row as it was exported, with each value of a field as the type the
    field declares (``_stored.loaded``), on every model base."""
    hints = field_types(model_class)
    return {k: _stored.loaded(v, hints[k]) if k in hints else v for k, v in record.items()}


def _new(model_class: Any, record: dict) -> Any:
    """A new instance of ``model_class`` holding ``record``, a row as it was exported.

    A pydantic class validates it as keyword arguments. A lite or msgspec class is built as
    a load builds it (``model_validate``): each value as the type its field declares, the
    fields a mixin gives a msgspec class set too (they are not ``__init__`` arguments there),
    and a key that is no field left out, as pydantic leaves it out.
    """
    if hasattr(model_class, "__pydantic_validator__"):
        return model_class(**record)
    return model_class.model_validate(record)


def _set_record(existing: Any, record: dict) -> None:
    """Copy an imported record onto an existing instance (before it is saved), each value
    of a field as the type the field declares."""
    for key, value in _typed(type(existing), record).items():
        setattr(existing, key, value)


def _save_record(
    model_class: Any, record_id: Any, record: dict, update_existing: bool, db: Optional["SQLerDB"]
) -> None:
    """Save one imported record: onto the row ``record_id`` when ``update_existing`` and
    that row exists, else as a new row; in ``db`` when given, else in the class's database."""
    if update_existing and record_id:
        existing = model_class.from_id(record_id, db=db)
        if existing:
            _set_record(existing, record)
            existing.save(db=db)
            return
    _new(model_class, record).save(db=db)


def _get_model_fields(model_class: Union[Type["SQLerModel"], Type["AsyncSQLerModel"]]) -> list[str]:
    """Get exportable field names from a model."""
    fields = []
    for name, field_info in model_class.model_fields.items():
        if not name.startswith("_"):
            fields.append(name)
    return fields


# =============================================================================
# Raw-row helpers (bypass Pydantic hydration for export performance)
# =============================================================================


def _get_query_and_model(
    source: Union["ModelQuerySet", Type["SQLerModel"], Type["AsyncSQLerModel"]],
):
    """Extract the underlying query object and model class from an export source."""
    if isinstance(source, type):  # model class
        return source.query()._query, source
    return source._query, source._model_cls  # queryset (sync or async)


# =============================================================================
# CSV Export/Import
# =============================================================================


def export_csv(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    path: Union[str, Path],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
    delimiter: str = ",",
    quoting: Literal[0, 1, 2, 3, 4, 5] = csv.QUOTE_MINIMAL,
) -> ExportResult:
    """Export query results or model table to CSV.

    Args:
        source: A ModelQuerySet or Model class to export
        path: Output file path
        fields: Specific fields to export (default: all)
        include_id: Include _id column
        delimiter: CSV delimiter character
        quoting: CSV quoting style

    Returns:
        ExportResult with export statistics
    """
    path = Path(path)
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)

    fieldnames = (["_id"] if include_id else []) + fields

    rows = query._rows(include_id=include_id)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter, quoting=quoting)
        writer.writeheader()

        for row in rows:
            if include_id:
                obj = json.loads(row[1])
                record: dict[str, Any] = {"_id": row[0]}
                record.update({k: _serialize_value(obj.get(k), for_csv=True) for k in fields})
            else:
                obj = json.loads(row[0])
                record = {k: _serialize_value(obj.get(k), for_csv=True) for k in fields}
            writer.writerow(record)

    return ExportResult(
        path=str(path),
        format="csv",
        count=len(rows),
        size_bytes=path.stat().st_size,
    )


def export_csv_string(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
    delimiter: str = ",",
) -> str:
    """Export to CSV string (for API responses).

    Returns:
        CSV content as string
    """
    output = io.StringIO()
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)

    fieldnames = (["_id"] if include_id else []) + fields

    rows = query._rows(include_id=include_id)

    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=delimiter)
    writer.writeheader()

    for row in rows:
        if include_id:
            obj = json.loads(row[1])
            record: dict[str, Any] = {"_id": row[0]}
            record.update({k: _serialize_value(obj.get(k), for_csv=True) for k in fields})
        else:
            obj = json.loads(row[0])
            record = {k: _serialize_value(obj.get(k), for_csv=True) for k in fields}
        writer.writerow(record)

    return output.getvalue()


def _import_csv_row(
    model_class: Type["SQLerModel"],
    row: dict,
    transform: Optional[Callable[[dict], dict]],
    update_existing: bool,
    db: Optional["SQLerDB"] = None,
) -> None:
    """Save one CSV row: transform, ``_id``, deserialize, create or update, in ``db`` when
    given, else in the class's database.

    An empty cell is a missing value: ``None`` in a field that can hold it, and left out of
    any other field, which then keeps its default (or, updated, its value)."""
    if transform:
        row = transform(row)

    record_id = row.pop("_id", None)
    if record_id:
        record_id = int(record_id)

    hints = field_types(model_class)
    for field_name, value in list(row.items()):
        if field_name in hints:
            hint = hints[field_name]
            if (value == "" or value is None) and not _admits_none(hint):
                del row[field_name]
            else:
                row[field_name] = _deserialize_value(value, hint)

    _save_record(model_class, record_id, row, update_existing, db)


def import_csv(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    delimiter: str = ",",
    skip_errors: bool = False,
    update_existing: bool = False,
    batch_size: Optional[int] = None,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["SQLerDB"] = None,
) -> ImportResult:
    """Import CSV data into model table.

    Args:
        model_class: Model class to import into
        path: CSV file path
        delimiter: CSV delimiter
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        batch_size: Commit every N rows. Without it (``None``, the default) every
            row is saved and committed on its own, so a failure at row k keeps the
            rows before it. With it, an import that stops at a failing row (no
            ``skip_errors``) keeps the rows of the full batches before it and
            nothing of the batch the failure is in; ``succeeded`` counts only the
            rows kept. With ``skip_errors`` a failing row is skipped and the rest
            of its batch is kept. Inside a transaction of the caller the batches
            are nested in it and commit with it.
        transform: Optional function to transform each row before import
        db: Optional database to import into (overrides class-level binding); the
            rows updated with ``update_existing`` are read from it too.

    Returns:
        ImportResult with import statistics
    """
    if batch_size is not None and batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    path = Path(path)
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    # the batches are transactions of the database the rows go to; none without batch_size
    batch_db = model_class._resolve_binding(db)[0] if batch_size is not None else None
    batch = None  # the open transaction of the current batch
    in_batch = 0  # rows seen in it
    ok_in_batch = 0  # rows of it that were saved

    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=delimiter)

            for row_num, row in enumerate(reader, start=2):  # Start at 2 (after header)
                total += 1
                if batch_db is not None and batch is None:
                    batch = batch_db.transaction()
                    batch.__enter__()
                    in_batch = ok_in_batch = 0

                try:
                    if batch_db is not None:
                        # a failed row undoes itself and leaves the batch usable
                        with batch_db.transaction():
                            _import_csv_row(model_class, row, transform, update_existing, batch_db)
                    else:
                        _import_csv_row(model_class, row, transform, update_existing, db)
                    succeeded += 1
                    ok_in_batch += 1

                except Exception as e:
                    error_info = {
                        "row": row_num,
                        "data": row,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }
                    errors.append(error_info)

                    if not skip_errors:
                        if batch is not None:
                            batch.rollback()
                            batch = None
                            succeeded -= ok_in_batch
                        return ImportResult(
                            count=total,
                            succeeded=succeeded,
                            failed=total - succeeded,
                            errors=errors,
                        )

                in_batch += 1
                if batch is not None and in_batch >= batch_size:  # type: ignore[operator]
                    batch.commit()
                    batch = None

        if batch is not None:
            batch.commit()
            batch = None
    finally:
        if batch is not None:
            batch.rollback()  # an unexpected error: nothing of the open batch is kept

    return ImportResult(
        count=total,
        succeeded=succeeded,
        failed=total - succeeded,
        errors=errors,
    )


# =============================================================================
# JSON Export/Import
# =============================================================================


def export_json(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    path: Union[str, Path],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
    indent: Optional[int] = None,
) -> ExportResult:
    """Export to JSON file.

    Args:
        source: A ModelQuerySet or Model class to export
        path: Output file path
        fields: Specific fields to export
        include_id: Include _id field
        indent: JSON indentation (None for compact)

    Returns:
        ExportResult with export statistics
    """
    path = Path(path)
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)
        all_fields = True
    else:
        all_fields = False

    rows = query._rows(include_id=include_id)

    data = []
    for row in rows:
        if include_id:
            obj = json.loads(row[1])
            obj["_id"] = row[0]
        else:
            obj = json.loads(row[0])
        if not all_fields:
            obj = {k: obj[k] for k in fields if k in obj}
        data.append(obj)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)

    return ExportResult(
        path=str(path),
        format="json",
        count=len(data),
        size_bytes=path.stat().st_size,
    )


def export_json_string(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
    indent: Optional[int] = None,
) -> str:
    """Export to JSON string."""
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)
        all_fields = True
    else:
        all_fields = False

    rows = query._rows(include_id=include_id)

    data = []
    for row in rows:
        if include_id:
            obj = json.loads(row[1])
            obj["_id"] = row[0]
        else:
            obj = json.loads(row[0])
        if not all_fields:
            obj = {k: obj[k] for k in fields if k in obj}
        data.append(obj)

    return json.dumps(data, indent=indent, ensure_ascii=False)


def import_json(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["SQLerDB"] = None,
) -> ImportResult:
    """Import JSON data into model table.

    Args:
        model_class: Model class to import into
        path: JSON file path (array of objects)
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each record
        db: Optional database to import into (overrides class-level binding); the
            rows updated with ``update_existing`` are read from it too.

    Returns:
        ImportResult with import statistics
    """
    path = Path(path)
    errors: list[dict[str, Any]] = []
    succeeded = 0

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = [data]

    total = len(data)

    for idx, record in enumerate(data):
        try:
            if transform:
                record = transform(record)

            record_id = record.pop("_id", None)
            _save_record(model_class, record_id, record, update_existing, db)

            succeeded += 1

        except Exception as e:
            error_info = {
                "index": idx,
                "data": record,
                "error": str(e),
                "error_type": type(e).__name__,
            }
            errors.append(error_info)

            if not skip_errors:
                break

    return ImportResult(
        count=total,
        succeeded=succeeded,
        failed=total - succeeded,
        errors=errors,
    )


# =============================================================================
# JSONL (JSON Lines) Export/Import
# =============================================================================


def export_jsonl(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    path: Union[str, Path],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
) -> ExportResult:
    """Export to JSONL (newline-delimited JSON) file.

    JSONL is ideal for streaming and large datasets.

    Args:
        source: A ModelQuerySet or Model class to export
        path: Output file path
        fields: Specific fields to export
        include_id: Include _id field

    Returns:
        ExportResult with export statistics
    """
    path = Path(path)
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)
        all_fields = True
    else:
        all_fields = False

    with open(path, "w", encoding="utf-8") as f:
        # Fast path: raw JSON strings, no parsing needed
        if not include_id and all_fields:
            raw_rows = query.all()
            for raw in raw_rows:
                f.write(raw + "\n")
            count = len(raw_rows)
        else:
            rows = query._rows(include_id=include_id)
            for row in rows:
                f.write(
                    _jsonl_line_from_row(
                        row, include_id=include_id, all_fields=all_fields, fields=fields
                    )
                    + "\n"
                )
            count = len(rows)

    return ExportResult(
        path=str(path),
        format="jsonl",
        count=count,
        size_bytes=path.stat().st_size,
    )


def _jsonl_line_from_row(row: Any, *, include_id: bool, all_fields: bool, fields: list[str]) -> str:
    """Render one query row (``_id, data`` or ``data``) as an exported JSONL line (no newline)."""
    if include_id:
        obj = json.loads(row[1])
        obj["_id"] = row[0]
    else:
        obj = json.loads(row[0])
    if not all_fields:
        obj = {k: obj[k] for k in fields if k in obj}
    return json.dumps(obj, ensure_ascii=False)


def _jsonl_parse_record(
    line: str, transform: Optional[Callable[[dict], dict]]
) -> tuple[Optional[int], dict]:
    """Parse one import line: JSON, ``transform``, then split off ``_id``.

    Touches no database. Raises whatever the parse or the transform raises.
    """
    record = json.loads(line)
    if transform:
        record = transform(record)
    record_id = record.pop("_id", None)
    # Safe models store their row version inside the JSON, so an export carries it. It is
    # not a constructor field (lite models reject it) and must not overwrite the stored
    # version on update, so it is dropped here.
    record.pop("_version", None)
    return record_id, record


def _jsonl_error(line_num: int, line: str, exc: Exception) -> dict[str, Any]:
    """The error record kept for a line that failed to import."""
    return {
        "line": line_num,
        "data": line[:200],  # Truncate for large lines
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _jsonl_result(total: int, succeeded: int, errors: list[dict[str, Any]]) -> ImportResult:
    return ImportResult(
        count=total,
        succeeded=succeeded,
        failed=total - succeeded,
        errors=errors,
    )


def _write_text_lines(path: Path, lines: list[str]) -> None:
    """Write ``lines``, each followed by a newline, as UTF-8 (runs in a worker thread)."""
    with open(path, "w", encoding="utf-8") as f:
        for text in lines:
            f.write(text + "\n")


def export_jsonl_string(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
) -> str:
    """Export to JSONL string.

    Returns:
        JSONL content as string (newline-separated JSON objects)
    """
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)
        all_fields = True
    else:
        all_fields = False

    # Fast path: raw JSON strings, no parsing needed
    if not include_id and all_fields:
        raw_rows = query.all()
        return "\n".join(raw_rows)

    rows = query._rows(include_id=include_id)

    lines = []
    for row in rows:
        if include_id:
            obj = json.loads(row[1])
            obj["_id"] = row[0]
        else:
            obj = json.loads(row[0])
        if not all_fields:
            obj = {k: obj[k] for k in fields if k in obj}
        lines.append(json.dumps(obj, ensure_ascii=False))

    return "\n".join(lines)


def stream_jsonl(
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
) -> Iterator[str]:
    """Stream JSONL records (for large datasets).

    Yields:
        JSON string for each record (no newline)
    """
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)
        all_fields = True
    else:
        all_fields = False

    # Fast path: raw JSON strings, no parsing needed
    if not include_id and all_fields:
        raw_rows = query.all()
        yield from raw_rows
        return

    for row in query._iter_rows(include_id=include_id):
        if include_id:
            obj = json.loads(row[1])
            obj["_id"] = row[0]
        else:
            obj = json.loads(row[0])
        if not all_fields:
            obj = {k: obj[k] for k in fields if k in obj}
        yield json.dumps(obj, ensure_ascii=False)


def import_jsonl(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["SQLerDB"] = None,
) -> ImportResult:
    """Import JSONL data into model table.

    Args:
        model_class: Model class to import into
        path: JSONL file path
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each record
        db: Optional database to import into (overrides class-level binding); the
            rows updated with ``update_existing`` are read from it too.

    Returns:
        ImportResult with import statistics
    """
    path = Path(path)
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            total += 1

            try:
                record_id, record = _jsonl_parse_record(line, transform)
                _save_record(model_class, record_id, record, update_existing, db)

                succeeded += 1

            except Exception as e:
                errors.append(_jsonl_error(line_num, line, e))

                if not skip_errors:
                    break

    return _jsonl_result(total, succeeded, errors)


# =============================================================================
# String Import Functions
# =============================================================================


def import_csv_string(
    model_class: Type["SQLerModel"],
    csv_string: str,
    *,
    delimiter: str = ",",
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["SQLerDB"] = None,
) -> ImportResult:
    """Import CSV data from a string.

    Args:
        model_class: Model class to import into
        csv_string: CSV content as string
        delimiter: CSV delimiter
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each row
        db: Optional database to import into (overrides class-level binding); the
            rows updated with ``update_existing`` are read from it too.

    Returns:
        ImportResult with import statistics
    """
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    reader = csv.DictReader(io.StringIO(csv_string), delimiter=delimiter)

    for row_num, row in enumerate(reader, start=2):
        total += 1

        try:
            _import_csv_row(model_class, row, transform, update_existing, db)

            succeeded += 1

        except Exception as e:
            error_info = {
                "row": row_num,
                "data": row,
                "error": str(e),
                "error_type": type(e).__name__,
            }
            errors.append(error_info)

            if not skip_errors:
                return ImportResult(
                    count=total,
                    succeeded=succeeded,
                    failed=total - succeeded,
                    errors=errors,
                )

    return ImportResult(
        count=total,
        succeeded=succeeded,
        failed=total - succeeded,
        errors=errors,
    )


def import_json_string(
    model_class: Type["SQLerModel"],
    json_string: str,
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["SQLerDB"] = None,
) -> ImportResult:
    """Import JSON data from a string.

    Args:
        model_class: Model class to import into
        json_string: JSON array as string
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each record
        db: Optional database to import into (overrides class-level binding); the
            rows updated with ``update_existing`` are read from it too.

    Returns:
        ImportResult with import statistics
    """
    errors: list[dict[str, Any]] = []
    succeeded = 0

    data = json.loads(json_string)

    if not isinstance(data, list):
        data = [data]

    total = len(data)

    for idx, record in enumerate(data):
        try:
            if transform:
                record = transform(record)

            record_id = record.pop("_id", None)
            _save_record(model_class, record_id, record, update_existing, db)

            succeeded += 1

        except Exception as e:
            error_info = {
                "index": idx,
                "data": record,
                "error": str(e),
                "error_type": type(e).__name__,
            }
            errors.append(error_info)

            if not skip_errors:
                break

    return ImportResult(
        count=total,
        succeeded=succeeded,
        failed=total - succeeded,
        errors=errors,
    )


def import_jsonl_string(
    model_class: Type["SQLerModel"],
    jsonl_string: str,
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["SQLerDB"] = None,
) -> ImportResult:
    """Import JSONL data from a string.

    Args:
        model_class: Model class to import into
        jsonl_string: JSONL content as string
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each record
        db: Optional database to import into (overrides class-level binding); the
            rows updated with ``update_existing`` are read from it too.

    Returns:
        ImportResult with import statistics
    """
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    for line_num, line in enumerate(jsonl_string.strip().split("\n"), start=1):
        line = line.strip()
        if not line:
            continue

        total += 1

        try:
            record = json.loads(line)

            if transform:
                record = transform(record)

            record_id = record.pop("_id", None)
            _save_record(model_class, record_id, record, update_existing, db)

            succeeded += 1

        except Exception as e:
            error_info = {
                "line": line_num,
                "data": line[:200],
                "error": str(e),
                "error_type": type(e).__name__,
            }
            errors.append(error_info)

            if not skip_errors:
                break

    return ImportResult(
        count=total,
        succeeded=succeeded,
        failed=total - succeeded,
        errors=errors,
    )


# =============================================================================
# Async Variants
# =============================================================================


async def async_export_jsonl(
    source: Union["AsyncSQLerQuerySet[Any]", Type["AsyncSQLerModel"]],
    path: Union[str, Path],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
) -> ExportResult:
    """Async export to JSONL file (same bytes as :func:`export_jsonl`).

    The file is written in a worker thread, so the event loop is not blocked.
    """
    path = Path(path)
    query, model_class = _get_query_and_model(source)

    if fields is None:
        fields = _get_model_fields(model_class)
        all_fields = True
    else:
        all_fields = False

    # Fast path: raw JSON strings, no parsing needed
    if not include_id and all_fields:
        lines = await query.all()
    else:
        rows = await query._rows(include_id=include_id)
        lines = [
            _jsonl_line_from_row(row, include_id=include_id, all_fields=all_fields, fields=fields)
            for row in rows
        ]

    await asyncio.to_thread(_write_text_lines, path, lines)
    size_bytes = (await asyncio.to_thread(path.stat)).st_size

    return ExportResult(
        path=str(path),
        format="jsonl",
        count=len(lines),
        size_bytes=size_bytes,
    )


async def async_import_jsonl(
    model_class: Type["AsyncSQLerModel"],
    path: Union[str, Path],
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
    db: Optional["AsyncSQLerDB"] = None,
) -> ImportResult:
    """Async import JSONL data (same rule per line as :func:`import_jsonl`).

    Lines are read in a worker thread; the model's ``from_id`` and ``save`` are awaited,
    with ``db`` when given (overrides class-level binding).
    """
    path = Path(path)
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    f = await asyncio.to_thread(open, path, "r", encoding="utf-8")
    try:
        line_num = 0
        while True:
            line = await asyncio.to_thread(f.readline)
            if not line:
                break
            line_num += 1
            line = line.strip()
            if not line:
                continue

            total += 1

            try:
                record_id, record = _jsonl_parse_record(line, transform)

                existing = None
                if update_existing and record_id:
                    existing = await model_class.from_id(record_id, db=db)
                if existing:
                    _set_record(existing, record)
                    await existing.save(db=db)
                else:
                    await _new(model_class, record).save(db=db)

                succeeded += 1

            except Exception as e:
                errors.append(_jsonl_error(line_num, line, e))

                if not skip_errors:
                    break
    finally:
        await asyncio.to_thread(f.close)

    return _jsonl_result(total, succeeded, errors)
