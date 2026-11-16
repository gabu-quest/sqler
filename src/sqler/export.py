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


def _deserialize_value(value: str, field_type: Any) -> Any:
    """Deserialize a string value based on field type.

    An empty cell is ``None``. A string value loses the one leading ``'`` that
    the CSV export adds (see :func:`_csv_escape_string`).
    """
    if value == "" or value is None:
        return None

    # Handle Optional types
    origin = getattr(field_type, "__origin__", None)
    if origin is Union or isinstance(field_type, types.UnionType):
        args = field_type.__args__
        # Get non-None type
        for arg in args:
            if arg is not type(None):
                field_type = arg
                break

    if field_type is int:
        return int(value)
    if field_type is float:
        return float(value)
    if field_type is bool:
        return value.lower() in ("true", "1", "yes")
    if field_type is datetime:
        return datetime.fromisoformat(value)
    if field_type is date:
        return date.fromisoformat(value)
    if field_type in (list, dict) or origin in (list, dict):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, str):
        return _csv_unescape_string(value)
    return value


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
    given, else in the class's database."""
    if transform:
        row = transform(row)

    record_id = row.pop("_id", None)
    if record_id:
        record_id = int(record_id)

    for field_name, value in list(row.items()):
        if field_name in model_class.model_fields:
            field_info = model_class.model_fields[field_name]
            row[field_name] = _deserialize_value(value, field_info.annotation)

    if update_existing and record_id:
        existing = model_class.from_id(record_id, db=db)
        if existing:
            for key, value in row.items():
                setattr(existing, key, value)
            existing.save(db=db)
        else:
            instance = model_class(**row)
            instance.save(db=db)
    else:
        instance = model_class(**row)
        instance.save(db=db)


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

            if update_existing and record_id:
                existing = model_class.from_id(record_id, db=db)
                if existing:
                    for key, value in record.items():
                        setattr(existing, key, value)
                    existing.save(db=db)
                else:
                    instance = model_class(**record)
                    instance.save(db=db)
            else:
                instance = model_class(**record)
                instance.save(db=db)

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


def _jsonl_apply_record(existing: Any, record: dict) -> None:
    """Copy an imported record onto an existing instance (before it is saved)."""
    for key, value in record.items():
        setattr(existing, key, value)


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

                if update_existing and record_id:
                    existing = model_class.from_id(record_id, db=db)
                    if existing:
                        _jsonl_apply_record(existing, record)
                        existing.save(db=db)
                    else:
                        instance = model_class(**record)
                        instance.save(db=db)
                else:
                    instance = model_class(**record)
                    instance.save(db=db)

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

            if update_existing and record_id:
                existing = model_class.from_id(record_id, db=db)
                if existing:
                    for key, value in record.items():
                        setattr(existing, key, value)
                    existing.save(db=db)
                else:
                    instance = model_class(**record)
                    instance.save(db=db)
            else:
                instance = model_class(**record)
                instance.save(db=db)

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

            if update_existing and record_id:
                existing = model_class.from_id(record_id, db=db)
                if existing:
                    for key, value in record.items():
                        setattr(existing, key, value)
                    existing.save(db=db)
                else:
                    instance = model_class(**record)
                    instance.save(db=db)
            else:
                instance = model_class(**record)
                instance.save(db=db)

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

                if update_existing and record_id:
                    existing = await model_class.from_id(record_id, db=db)
                    if existing:
                        _jsonl_apply_record(existing, record)
                        await existing.save(db=db)
                    else:
                        instance = model_class(**record)
                        await instance.save(db=db)
                else:
                    instance = model_class(**record)
                    await instance.save(db=db)

                succeeded += 1

            except Exception as e:
                errors.append(_jsonl_error(line_num, line, e))

                if not skip_errors:
                    break
    finally:
        await asyncio.to_thread(f.close)

    return _jsonl_result(total, succeeded, errors)
