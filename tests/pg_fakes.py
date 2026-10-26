"""A scripted stand-in for a psycopg connection: records SQL and returns canned results."""


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.db.statements.append((normalized, params))
        self._result = None
        if normalized.startswith("UPDATE change_records"):
            self.rowcount = self.db.update_rowcounts.pop(0) if self.db.update_rowcounts else 1
        for prefix, value in self.db.results.items():
            if normalized.startswith(prefix):
                self._result = value
                break

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.db)


class FakeDatabase:
    def __init__(self):
        self.statements: list[tuple[str, object]] = []
        self.update_rowcounts: list[int] = []
        # SQL prefix -> value returned by fetchone()/fetchall() for statements starting with it.
        self.results: dict[str, object] = {}

    def sql(self, prefix: str) -> list[tuple[str, object]]:
        return [item for item in self.statements if item[0].startswith(prefix)]

    def connect(self, *_args, **_kwargs):
        return FakeConnection(self)
