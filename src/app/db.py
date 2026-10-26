def pg_connect(database_url: str):
    """Open a psycopg connection; used as a context manager (commit on success, rollback on error)."""
    if not database_url:
        raise ValueError("STORAGE_BACKEND=postgres requires DATABASE_URL")
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - import guard
        raise ValueError("Postgres backend requires 'psycopg' package") from exc
    return psycopg.connect(database_url)
