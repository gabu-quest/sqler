# sqler

Document-oriented JSON store on SQLite.

## Follow-up TODO

- [ ] Remove `set_db()` (backward compat concern — `using()` is the recommended path now)
- [ ] Add `db` param to `save()`/`delete()` instance methods (allows per-call DB binding; `using()` covers query path which is qler's primary need)
- [ ] Sync adapter changes (already uses thread-local; no concurrency bug — but could add `using()` parity)
