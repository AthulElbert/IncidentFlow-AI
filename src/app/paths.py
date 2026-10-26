def normalize_rel_path(path: str) -> str:
    """Forward slashes and no leading './'.

    Deliberately not `lstrip("./")`: that strips *characters*, so ".env" becomes "env" and
    ".github/ci.yml" becomes "github/ci.yml", which defeats allow- and deny-list checks.
    """
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return "" if cleaned == "." else cleaned
