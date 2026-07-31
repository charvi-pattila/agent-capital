#!/usr/bin/env python3
"""Set up emailing of the daily report.

Run once (`python backend/set_email.py`). Until it has been run, Close All still
builds the PDF and saves it under data/reports/ — it just doesn't mail it, so a
fresh checkout works with no setup.

Gmail will NOT accept your normal account password here. Create an App Password:
  myaccount.google.com → Security → 2-Step Verification → App passwords
It looks like "abcd efgh ijkl mnop" (16 characters, spaces optional).

Nothing is emailed by this script; it only writes data/.email_config, which is
gitignored along with the rest of data/. Re-run anytime to change it — the
running server re-reads the file per send, so no restart is needed.
"""
import getpass
import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "data" / ".email_config"


def main():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    sender = input("Gmail address to send from: ").strip()
    if "@" not in sender:
        print("That doesn't look like an email address.")
        sys.exit(1)

    app_password = getpass.getpass("Google App Password (not your normal password): ").strip()
    if not app_password:
        print("App password cannot be empty.")
        sys.exit(1)
    if " " in app_password.strip() and len(app_password.replace(" ", "")) != 16:
        print("Warning: Google app passwords are normally 16 characters. Saving anyway.")

    recipient = input(f"Send the report to [{sender}]: ").strip() or sender

    CONFIG_PATH.write_text(json.dumps({
        "smtp_user": sender,
        "smtp_pass": app_password.replace(" ", ""),
        "to": recipient,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
    }, indent=2))
    CONFIG_PATH.chmod(0o600)

    print(f"\nSaved to {CONFIG_PATH} (permissions 600).")
    print(f"The daily report will be emailed to {recipient} whenever you press Close All.")
    print("Send one right now to check it works:")
    print("  curl -X POST localhost:8888/api/daily-report")


if __name__ == "__main__":
    main()
