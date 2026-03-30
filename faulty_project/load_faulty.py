import random
import time
from urllib import request

BASE_URL = "http://127.0.0.1:8010"


def call_checkout() -> None:
    try:
        with request.urlopen(f"{BASE_URL}/api/checkout", timeout=5) as resp:
            _ = resp.read()
    except Exception:
        pass


def main() -> None:
    print("Generating traffic on faulty project. Ctrl+C to stop.")
    while True:
        for _ in range(random.randint(2, 6)):
            call_checkout()
            time.sleep(0.05)
        time.sleep(0.15)


if __name__ == "__main__":
    main()
