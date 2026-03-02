# Hydration Alternatives: Pydantic vs msgspec vs Raw Dicts

Design analysis for sqler's model hydration layer. Triggered by the export optimization
that proved Pydantic hydration is the dominant cost in bulk read paths (2.8x → 1.0x by
bypassing it).

## The Problem

Every `queryset.all()` call hydrates rows through Pydantic:

```
SQLite row → json.loads() → dict → model_validate() → Model instance
```

At 50K rows, `model_validate()` alone accounts for ~60% of wall time. At 1M rows, it adds
seconds of pure Python overhead. For read paths where the caller wants JSON/dicts back (exports,
API serialization, streaming), this is a round-trip to nowhere.

## Options Evaluated

### Option A: Status Quo (Pydantic v2 model_validate)

**How it works today:**
```python
# queryset.all() materializes every row
for doc in raw_dicts:
    instance = model_cls.model_validate(doc)  # ~1.1μs per row
    results.append(instance)
```

| Metric | Value |
|--------|-------|
| Per-row cost | ~1,100 ns |
| 50K rows overhead | ~55ms |
| 1M rows overhead | ~1.1s |
| Validators | Full — @field_validator, @model_validator, coercion |
| Type safety | Full — datetime, nested models, unions |
| Schema drift protection | Yes — defaults filled for new fields |
| External mutation protection | Yes — raises ValidationError on bad data |

**Verdict:** Correct default. Keep for `queryset.all()` and any path where callers need
typed model instances.

### Option B: model_construct() (Skip Validation)

**The surprise finding:** In Pydantic v2, `model_construct()` is **slower** than
`model_validate()`.

```
model_validate():   ~1,100 ns  (Rust-compiled validation)
model_construct():  ~2,540 ns  (Python-side attribute setting)
```

Confirmed by Pydantic maintainers in [issue #10536](https://github.com/pydantic/pydantic/issues/10536).
The v2 Rust core makes the validated path faster than the unvalidated Python path.

**Verdict:** Dead end. Do not use as an optimization. It's slower AND less safe.

### Option C: Raw Dicts (json.loads only)

**What the export optimization does:**
```python
sql, params = query._build_query(include_id=True)
rows = adapter.execute(sql, params).fetchall()
for row in rows:
    obj = json.loads(row[1])   # ~200ns per row
    obj["_id"] = row[0]
    # use obj directly — no model
```

| Metric | Value |
|--------|-------|
| Per-row cost | ~200 ns (json.loads only) |
| Speedup vs Pydantic | ~5.5x |
| Validators | None |
| Type safety | None — everything is str/int/float/list/dict |
| Schema drift protection | None — missing fields stay missing |
| External mutation protection | None — garbage in, garbage out |

**When it's safe:**
- Terminal operations (export, streaming, API responses as JSON)
- Data written by sqler (validated on write)
- No external mutation of the SQLite file
- No schema drift (no new fields with defaults added after data was stored)

**When it's dangerous:**
- Multi-writer databases
- Schema evolution with new default fields
- Downstream code expecting typed objects (datetime, nested models)

**Verdict:** Already proven safe for exports. Could be exposed as `queryset.as_dicts()`
for bulk API serialization. Must document tradeoffs clearly.

### Option D: msgspec Structs

**What it offers:**
```python
import msgspec

class User(msgspec.Struct):
    name: str
    email: str | None = None
    score: float = 0.0

# 8-9x faster than Pydantic v2 for JSON decode + validate
user = msgspec.json.decode(b'{"name":"Alice"}', type=User)

# dict → struct (equivalent of model_validate)
user = msgspec.convert({"name": "Alice"}, User)
```

| Metric | Value |
|--------|-------|
| Per-row cost (JSON decode) | ~145 ns (vs 1,100 ns Pydantic) |
| Speedup vs Pydantic | **~8x** |
| Validators | `__post_init__` only (whole-object, no per-field) |
| Type safety | Full — coercion, nested structs, unions |
| Schema drift protection | Yes — defaults filled |
| JSON schema | Yes — `msgspec.json.schema()` |
| Memory | ~25x less than Pydantic for bulk operations |

#### What maps cleanly to sqler

| sqler pattern | Pydantic | msgspec |
|---------------|----------|---------|
| Deserialize from DB | `model_validate(doc)` | `msgspec.convert(doc, cls)` |
| Serialize to JSON | `model_dump()` | `msgspec.to_builtins(instance)` |
| JSON decode | `model_validate_json(b)` | `msgspec.json.decode(b, type=cls)` |
| Config: ignore extra | `model_config extra='ignore'` | Default behavior |
| Config: frozen | `model_config frozen=True` | `Struct(frozen=True)` |
| Generic models | `BaseModel[T]` | `Struct, Generic[T]` |
| Field introspection | `cls.model_fields` (dict) | `msgspec.structs.fields(cls)` (tuple) |
| Schema generation | Pydantic JSON schema | `msgspec.json.schema(cls)` |

#### Blockers for sqler

**1. PrivateAttr (HARD BLOCKER)**

sqler's `SQLerModel` uses Pydantic's `PrivateAttr` for internal state:

```python
class SQLerModel(BaseModel):
    _id: Optional[int] = PrivateAttr(default=None)
    _snapshot: Optional[dict] = PrivateAttr(default=None)
```

msgspec Structs have no equivalent. Every field must be declared and typed. You can't
have per-instance private state that lives outside the struct's declared fields.

Workarounds:
- **Wrapper class**: Wrap the Struct in a regular class that adds `_id` and `_snapshot`.
  Loses ergonomic benefit, adds indirection.
- **`__dict__` injection**: Use `object.__setattr__(self, '_id', value)` on a non-frozen
  Struct. Fragile — relies on CPython implementation details.
- **Declare as regular fields**: Make `_id` and `_snapshot` regular optional fields with
  `omit_defaults=True` so they're excluded from serialization. Changes the API surface.
- **Redesign**: Store `_id` outside the model entirely (e.g., as a tuple `(id, model)`
  returned by the queryset). Most principled but largest API change.

**2. Field validators (MEDIUM BLOCKER)**

msgspec only has `__post_init__` — a single hook that runs after construction. No per-field
validators, no `mode='before'` preprocessing. sqler doesn't use field validators internally,
but user models might. Any `@field_validator` in user code would break.

**3. model_fields introspection (MEDIUM BLOCKER)**

Used in ~20 call sites across the codebase. `cls.model_fields` returns a dict of
`{name: FieldInfo}`. msgspec's `msgspec.structs.fields(cls)` returns a tuple of FieldInfo
objects with `.name`, `.type`, `.default`. All call sites need rewriting.

**4. Computed fields**

msgspec has no `@computed_field`. Properties work but don't appear in serialization output.
sqler doesn't use computed fields internally, but user models might.

#### Migration paths

**Path 1: Parallel model base (SQLerMsgspecModel)**

Add `SQLerMsgspecModel` alongside `SQLerModel`. Users opt in per model:

```python
from sqler import SQLerMsgspecModel

class FastUser(SQLerMsgspecModel):
    name: str
    email: str
```

- Pro: No breaking changes, gradual adoption
- Con: Two code paths to maintain, doubles test surface

**Path 2: Internal-only optimization**

Keep Pydantic for the public API. Use msgspec (or raw dicts) only in internal hot paths
where models are constructed and immediately serialized:

- Export functions (already done with raw dicts)
- FTS rebuild (should use single SQL, not Python objects at all)
- `queryset.as_dicts()` (new API returning raw dicts)

- Pro: Zero breaking changes, targeted optimization
- Con: Doesn't help user code that calls `.all()` on large querysets

**Path 3: Full migration (SQLerModel → msgspec)**

Replace Pydantic entirely. Solve PrivateAttr with a redesign (separate `_id` from the model).
Rewrite all ~20 introspection call sites.

- Pro: 8x faster hydration everywhere, 25x less memory
- Con: Breaking change for every sqler user. Months of work. Loses Pydantic ecosystem.

**Verdict:** Not recommended as a standalone effort.

**Path 4: Wait for Pydantic v3**

Pydantic v3 is in development. If it closes the performance gap with msgspec (plausible
given the v1→v2 Rust rewrite), the migration becomes unnecessary.

- Pro: Zero effort
- Con: Unknown timeline, may not materialize

### Option E: Selective Hydration

**Concept:** Hydrate only the fields the caller needs, skip the rest.

```python
# Only validate/coerce 'created_at' and 'score', pass the rest through
partial = model_cls.model_validate(doc, partial_fields=['created_at', 'score'])
```

Pydantic doesn't support this natively. It would require building a dynamic submodel with
only the needed fields, then validating against that. The overhead of dynamic model creation
might negate the savings.

**Verdict:** Interesting but not practical with current Pydantic API.

---

## Recommendation

### Short term (now)

1. **Keep the export optimization.** Raw dicts for exports are safe, proven, and shipped.
2. **Expose `queryset.as_dicts()`** as a public API. Internally calls `query.all_dicts()`
   (which already exists). Document that it skips validators and returns raw dicts.
3. **Fix FTS rebuild with single SQL.** Don't hydrate at all — let SQLite do the work.

### Medium term (next major)

4. **Evaluate msgspec as an optional model base.** Solve the PrivateAttr problem first.
   If the redesign is clean (storing `_id` outside the model), build `SQLerMsgspecModel`
   as an opt-in alternative with 8x faster reads.
5. **Benchmark `model_construct()` in sqler's specific context.** The Pydantic #10536
   finding is from a microbenchmark. sqler's models may behave differently (more fields,
   nested models, etc.). Measure before dismissing.

### Long term

6. **Watch Pydantic v3.** If it ships with significant perf improvements, the msgspec
   migration becomes less compelling.
7. **Consider msgspec for the Lite model layer.** sqler's `SQLerLiteModel` (dataclass-based)
   is already a "fast alternative" to the Pydantic model. Replacing its internals with
   msgspec would be lower risk since it doesn't use PrivateAttr.

---

## Decision Status

**Not yet decided.** This document captures the analysis. The next step is to:

1. Discuss the `queryset.as_dicts()` API shape
2. Prototype `SQLerMsgspecModel` to validate the PrivateAttr workaround
3. Run benchmarks comparing msgspec `convert()` vs Pydantic `model_validate()` in sqler's
   actual queryset materialization path (not microbenchmarks)

See also:
- [benchmarks/FINDINGS.md](../benchmarks/FINDINGS.md) — "Pydantic Hydration is the Dominant Cost"
- [Export optimization commit](../src/sqler/export.py) — the proof that raw dicts work
