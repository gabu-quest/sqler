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

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterator,
    Optional,
    Type,
    Union,
)

if TYPE_CHECKING:
    from sqler.models import SQLerModel
    from sqler.models.queryset import ModelQuerySet


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


def _serialize_value(value: Any, for_csv: bool = False) -> Any:
    """Serialize a value for export.

    Args:
        value: Value to serialize
        for_csv: If True, serialize lists/dicts to JSON strings (for CSV)
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if for_csv and isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _deserialize_value(value: str, field_type: Any) -> Any:
    """Deserialize a string value based on field type."""
    if value == "" or value is None:
        return None

    # Handle Optional types
    origin = getattr(field_type, "__origin__", None)
    if origin is Union:
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
    return value


def _get_model_fields(model_class: Type["SQLerModel"]) -> list[str]:
    """Get exportable field names from a model."""
    fields = []
    for name, field_info in model_class.model_fields.items():
        if not name.startswith("_"):
            fields.append(name)
    return fields


def _model_to_dict(
    model: "SQLerModel",
    fields: Optional[list[str]] = None,
    include_id: bool = True,
    for_csv: bool = False,
) -> dict[str, Any]:
    """Convert a model instance to a dictionary.

    Args:
        model: Model instance to convert
        fields: Fields to include (default: all)
        include_id: Include _id field
        for_csv: If True, serialize complex types to JSON strings
    """
    data = {}
    if include_id and model._id is not None:
        data["_id"] = model._id

    model_fields = fields or _get_model_fields(type(model))
    for field in model_fields:
        value = getattr(model, field, None)
        data[field] = _serialize_value(value, for_csv=for_csv)

    return data


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
    quoting: int = csv.QUOTE_MINIMAL,
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

    # Get items to export
    if hasattr(source, "all"):
        # It's a queryset
        items = source.all()
        model_class = source._model
    else:
        # It's a model class
        items = source.query().all()
        model_class = source

    # Determine fields
    if fields is None:
        fields = _get_model_fields(model_class)

    fieldnames = (["_id"] if include_id else []) + fields

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter, quoting=quoting)
        writer.writeheader()

        for item in items:
            row = _model_to_dict(item, fields=fields, include_id=include_id, for_csv=True)
            writer.writerow(row)
            count += 1

    return ExportResult(
        path=str(path),
        format="csv",
        count=count,
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

    if hasattr(source, "all"):
        items = source.all()
        model_class = source._model
    else:
        items = source.query().all()
        model_class = source

    if fields is None:
        fields = _get_model_fields(model_class)

    fieldnames = (["_id"] if include_id else []) + fields

    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=delimiter)
    writer.writeheader()

    for item in items:
        row = _model_to_dict(item, fields=fields, include_id=include_id, for_csv=True)
        writer.writerow(row)

    return output.getvalue()


def import_csv(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    delimiter: str = ",",
    skip_errors: bool = False,
    update_existing: bool = False,
    batch_size: int = 100,
    transform: Optional[Callable[[dict], dict]] = None,
) -> ImportResult:
    """Import CSV data into model table.

    Args:
        model_class: Model class to import into
        path: CSV file path
        delimiter: CSV delimiter
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        batch_size: Commit every N records (for memory efficiency)
        transform: Optional function to transform each row before import

    Returns:
        ImportResult with import statistics
    """
    path = Path(path)
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=delimiter)

        for row_num, row in enumerate(reader, start=2):  # Start at 2 (after header)
            total += 1

            try:
                # Transform if provided
                if transform:
                    row = transform(row)

                # Handle _id
                record_id = row.pop("_id", None)
                if record_id:
                    record_id = int(record_id)

                # Deserialize values based on model field types
                for field_name, value in list(row.items()):
                    if field_name in model_class.model_fields:
                        field_info = model_class.model_fields[field_name]
                        row[field_name] = _deserialize_value(value, field_info.annotation)

                # Create or update
                if update_existing and record_id:
                    existing = model_class.from_id(record_id)
                    if existing:
                        for key, value in row.items():
                            setattr(existing, key, value)
                        existing.save()
                    else:
                        instance = model_class(**row)
                        instance.save()
                else:
                    instance = model_class(**row)
                    instance.save()

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

    if hasattr(source, "all"):
        items = source.all()
        model_class = source._model
    else:
        items = source.query().all()
        model_class = source

    if fields is None:
        fields = _get_model_fields(model_class)

    data = [_model_to_dict(item, fields=fields, include_id=include_id) for item in items]

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
    if hasattr(source, "all"):
        items = source.all()
        model_class = source._model
    else:
        items = source.query().all()
        model_class = source

    if fields is None:
        fields = _get_model_fields(model_class)

    data = [_model_to_dict(item, fields=fields, include_id=include_id) for item in items]
    return json.dumps(data, indent=indent, ensure_ascii=False)


def import_json(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
) -> ImportResult:
    """Import JSON data into model table.

    Args:
        model_class: Model class to import into
        path: JSON file path (array of objects)
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each record

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
                existing = model_class.from_id(record_id)
                if existing:
                    for key, value in record.items():
                        setattr(existing, key, value)
                    existing.save()
                else:
                    instance = model_class(**record)
                    instance.save()
            else:
                instance = model_class(**record)
                instance.save()

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

    if hasattr(source, "all"):
        items = source.all()
        model_class = source._model
    else:
        items = source.query().all()
        model_class = source

    if fields is None:
        fields = _get_model_fields(model_class)

    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            record = _model_to_dict(item, fields=fields, include_id=include_id)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    return ExportResult(
        path=str(path),
        format="jsonl",
        count=count,
        size_bytes=path.stat().st_size,
    )


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
    if hasattr(source, "all"):
        items = source.all()
        model_class = source._model
    else:
        items = source.query().all()
        model_class = source

    if fields is None:
        fields = _get_model_fields(model_class)

    for item in items:
        record = _model_to_dict(item, fields=fields, include_id=include_id)
        yield json.dumps(record, ensure_ascii=False)


def import_jsonl(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
) -> ImportResult:
    """Import JSONL data into model table.

    Args:
        model_class: Model class to import into
        path: JSONL file path
        skip_errors: Continue on validation errors
        update_existing: Update records with matching _id
        transform: Optional function to transform each record

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
                record = json.loads(line)

                if transform:
                    record = transform(record)

                record_id = record.pop("_id", None)

                if update_existing and record_id:
                    existing = model_class.from_id(record_id)
                    if existing:
                        for key, value in record.items():
                            setattr(existing, key, value)
                        existing.save()
                    else:
                        instance = model_class(**record)
                        instance.save()
                else:
                    instance = model_class(**record)
                    instance.save()

                succeeded += 1

            except Exception as e:
                error_info = {
                    "line": line_num,
                    "data": line[:200],  # Truncate for large lines
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
    source: Union["ModelQuerySet", Type["SQLerModel"]],
    path: Union[str, Path],
    *,
    fields: Optional[list[str]] = None,
    include_id: bool = True,
) -> ExportResult:
    """Async export to JSONL file."""
    import aiofiles

    path = Path(path)

    if hasattr(source, "all"):
        items = await source.all()
        model_class = source._model
    else:
        items = await source.query().all()
        model_class = source

    if fields is None:
        fields = _get_model_fields(model_class)

    count = 0
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        for item in items:
            record = _model_to_dict(item, fields=fields, include_id=include_id)
            await f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    return ExportResult(
        path=str(path),
        format="jsonl",
        count=count,
        size_bytes=path.stat().st_size,
    )


async def async_import_jsonl(
    model_class: Type["SQLerModel"],
    path: Union[str, Path],
    *,
    skip_errors: bool = False,
    update_existing: bool = False,
    transform: Optional[Callable[[dict], dict]] = None,
) -> ImportResult:
    """Async import JSONL data."""
    import aiofiles

    path = Path(path)
    errors: list[dict[str, Any]] = []
    succeeded = 0
    total = 0

    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        line_num = 0
        async for line in f:
            line_num += 1
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
                    existing = await model_class.afrom_id(record_id)
                    if existing:
                        for key, value in record.items():
                            setattr(existing, key, value)
                        await existing.asave()
                    else:
                        instance = model_class(**record)
                        await instance.asave()
                else:
                    instance = model_class(**record)
                    await instance.asave()

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
