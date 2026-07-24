#!/usr/bin/env python3
"""Set or change the Claude Manager login password.

Run this once (`python backend/set_password.py`) to turn on the login gate —
server.py checks for data/.auth_hash on every request and skips the gate
entirely until this has been run, so a fresh checkout still works with no
setup. Re-run anytime to change the password; the running server picks it
up immediately, no restart needed.
"""
import getpass
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

AUTH_HASH_PATH = Path(__file__).parent.parent / "data" / ".auth_hash"


def main():
    AUTH_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    password = getpass.getpass("New password: ")
    if not password:
        print("Password cannot be empty.")
        sys.exit(1)
    if getpass.getpass("Confirm password: ") != password:
        print("Passwords didn't match.")
        sys.exit(1)
    AUTH_HASH_PATH.write_text(generate_password_hash(password))
    print(f"Password set ({AUTH_HASH_PATH}). The login gate is now active — no restart needed.")


if __name__ == "__main__":
    main()
