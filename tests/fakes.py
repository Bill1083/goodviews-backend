"""A small in-memory stand-in for the supabase-py query builder: enough of
select / eq / in_ / is_ / limit / order / range / upsert / update / delete for the
recommendation and daily-picks tests. Every write is logged so tests can
assert on what would have reached the database."""


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db: "FakeSupabase", table: str):
        self.db, self.table = db, table
        self.op, self.payload, self.kwargs = "select", None, {}
        self.columns = "*"
        self.filters: list[tuple[str, object]] = []
        self.window: tuple[int, int] | None = None

    # ── reads ────────────────────────────────────────────────────────────────
    def select(self, columns="*", **_kw):
        self.op, self.columns = "select", columns
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, set(vals)))
        return self

    def is_(self, col, val):
        self.filters.append((col, None if val == "null" else val))
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        self.window = (start, end)
        return self

    # ── writes ───────────────────────────────────────────────────────────────
    def upsert(self, payload, **kwargs):
        self.op, self.payload, self.kwargs = "upsert", payload, kwargs
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _matches(self, row: dict) -> bool:
        for col, val in self.filters:
            if isinstance(val, set):
                if row.get(col) not in val:
                    return False
            elif val is None:
                if row.get(col) is not None:
                    return False
            elif str(row.get(col)) != str(val):
                return False
        return True

    def _check_columns(self, names):
        missing = set(names) & self.db.missing_columns.get(self.table, set())
        if missing:
            col = sorted(missing)[0]
            raise RuntimeError(f"PGRST204 Could not find the '{col}' column of '{self.table}' in the schema cache")

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        self.db.log.append((self.op, self.table, self.payload, self.kwargs, list(self.filters)))

        if self.op == "select":
            if self.columns != "*":
                self._check_columns(c.strip() for c in self.columns.split(",") if "(" not in c)
            found = [dict(r) for r in rows if self._matches(r)]
            if self.window:
                found = found[self.window[0]:self.window[1] + 1]
            return FakeResult(found)

        if self.op == "delete":
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return FakeResult([])

        if self.op == "update":
            self._check_columns(self.payload)
            for r in rows:
                if self._matches(r):
                    r.update(self.payload)
            return FakeResult([])

        # upsert
        payload = self.payload if isinstance(self.payload, list) else [self.payload]
        for item in payload:
            self._check_columns(item)
        keys = [k.strip() for k in (self.kwargs.get("on_conflict") or "id").split(",")]
        for item in payload:
            existing = next((r for r in rows if all(r.get(k) == item.get(k) for k in keys)), None)
            if existing is None:
                rows.append(dict(item))
            elif not self.kwargs.get("ignore_duplicates"):
                existing.update(item)
        return FakeResult([])


class FakeSupabase:
    def __init__(self, tables=None, missing_columns=None):
        self.tables = tables if tables is not None else {}
        self.missing_columns = missing_columns or {}
        self.log: list[tuple] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def writes(self, op: str, table: str) -> list[tuple]:
        return [entry for entry in self.log if entry[0] == op and entry[1] == table]
