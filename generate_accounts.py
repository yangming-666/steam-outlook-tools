import argparse
import csv
import secrets
import string
from pathlib import Path


LOWER = string.ascii_lowercase
UPPER = string.ascii_uppercase
DIGITS = string.digits
USERNAME_CHARS = LOWER + DIGITS
PASSWORD_CHARS = LOWER + UPPER + DIGITS
FIRST_NAMES = [
    "aaron",
    "adam",
    "alex",
    "alice",
    "amelia",
    "andrew",
    "anna",
    "ava",
    "ben",
    "brian",
    "charlie",
    "chloe",
    "daniel",
    "david",
    "emily",
    "emma",
    "ethan",
    "grace",
    "hannah",
    "henry",
    "isabella",
    "jack",
    "james",
    "jason",
    "jessica",
    "john",
    "julia",
    "kevin",
    "laura",
    "leo",
    "lily",
    "lucas",
    "maria",
    "mark",
    "michael",
    "mia",
    "natalie",
    "noah",
    "olivia",
    "owen",
    "ryan",
    "sarah",
    "sophia",
    "thomas",
    "victoria",
    "william",
    "zoe",
]
LAST_NAMES = [
    "adams",
    "anderson",
    "baker",
    "brown",
    "campbell",
    "carter",
    "chen",
    "clark",
    "davis",
    "edwards",
    "evans",
    "garcia",
    "green",
    "hall",
    "harris",
    "hill",
    "jackson",
    "johnson",
    "king",
    "lee",
    "lewis",
    "martin",
    "miller",
    "mitchell",
    "moore",
    "nelson",
    "parker",
    "roberts",
    "scott",
    "smith",
    "taylor",
    "thomas",
    "walker",
    "watson",
    "white",
    "wilson",
    "wright",
    "young",
]


def random_text(chars: str, length: int) -> str:
    return "".join(secrets.choice(chars) for _ in range(length))


def generate_username(prefix: str) -> str:
    first_name = secrets.choice(FIRST_NAMES)
    last_name = secrets.choice(LAST_NAMES)
    number = secrets.choice(
        [
            str(secrets.randbelow(90) + 10),
            str(secrets.randbelow(900) + 100),
            str(secrets.randbelow(31) + 1975),
        ]
    )
    separator = secrets.choice(["", ".", "_"])
    pattern = secrets.choice(
        [
            f"{first_name}{separator}{last_name}{number}",
            f"{first_name[0]}{separator}{last_name}{number}",
            f"{first_name}{separator}{last_name[0]}{number}",
        ]
    )
    return f"{prefix}{pattern}"


def generate_password(length: int) -> str:
    if length < 3:
        raise ValueError("Password length must be at least 3.")

    chars = [
        secrets.choice(LOWER),
        secrets.choice(UPPER),
        secrets.choice(DIGITS),
    ]
    chars.extend(secrets.choice(PASSWORD_CHARS) for _ in range(length - 3))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def generate_records(count: int, password_length: int, prefix: str) -> list[dict[str, str]]:
    seen_usernames = set()
    records = []

    while len(records) < count:
        username = generate_username(prefix)
        if username in seen_usernames:
            continue

        seen_usernames.add(username)
        records.append(
            {
                "username": username,
                "password": generate_password(password_length),
            }
        )

    return records


def write_csv(records: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["username", "password"])
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate random usernames and passwords containing lowercase, uppercase, and digits."
    )
    parser.add_argument("-c", "--count", type=int, default=10, help="Number of records to generate. Default: 10")
    parser.add_argument("--password-length", type=int, default=12, help="Password length. Default: 12")
    parser.add_argument("--prefix", default="", help="Optional username prefix.")
    parser.add_argument("-o", "--output", type=Path, help="Optional CSV output path.")
    args = parser.parse_args()

    if args.count <= 0:
        raise SystemExit("Count must be greater than 0.")
    try:
        records = generate_records(args.count, args.password_length, args.prefix)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.output:
        write_csv(records, args.output)
        print(f"Generated {len(records)} records: {args.output}")
        return

    for record in records:
        print(f"{record['username']},{record['password']}")


if __name__ == "__main__":
    main()
