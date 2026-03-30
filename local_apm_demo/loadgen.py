import random
import time
from urllib import request

BASE_URL = "http://127.0.0.1:8001"


def call(path: str) -> None:
    try:
        with request.urlopen(f"{BASE_URL}{path}", timeout=3) as resp:
            _ = resp.read()
    except Exception:
        pass


def main() -> None:
    print("Starting loadgen. Press Ctrl+C to stop.")
    while True:
        n = random.random()
        if n < 0.7:
            call("/api/ok")
        elif n < 0.9:
            call("/api/slow")
        else:
            call("/api/error")
        time.sleep(0.08)


if __name__ == "__main__":
    main()
