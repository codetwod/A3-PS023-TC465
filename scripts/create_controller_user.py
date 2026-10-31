"""Provision a controller user offline; never put plaintext passwords in JSON."""
import argparse
import getpass
import json
from pathlib import Path
import os

from controller.auth import password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users-file", required=True, type=Path)
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=["user", "admin"], default="user")
    args = parser.parse_args()
    if not args.username.strip() or len(args.username) > 128:
        parser.error("Username must be nonempty and at most 128 characters")
    password = getpass.getpass("Password (at least 12 characters): ")
    if not 12 <= len(password) <= 1024 or password != getpass.getpass("Confirm password: "):
        parser.error("Passwords must match and be 12–1024 characters")
    users = json.loads(args.users_file.read_text()) if args.users_file.exists() else {}
    users[args.username] = {"password_hash": password_hash(password), "role": args.role}
    # Provision offline; serialize updates through this script, then restart controller.
    temporary = args.users_file.with_suffix(args.users_file.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(users, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, args.users_file)
    print("User saved. Restart controller processes to load the updated users file.")


if __name__ == "__main__":
    main()
