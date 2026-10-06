"""Print a new access-token signing key entry for BELUNO_AUTH_SIGNING_KEYS.

Rotation: prepend the new entry (it signs new tokens) and keep the previous entry
listed until every token it signed has expired, then remove it. Store the value
in the secret manager; never commit it.
"""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import UTC, datetime

from beluno.testkit.environment import generate_signing_keys_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kid", default=None, help="Key id; defaults to a dated random id")
    parser.add_argument(
        "--with-token-hash-key",
        action="store_true",
        help="Also print a random BELUNO_TOKEN_HASH_KEY for a fresh environment",
    )
    arguments = parser.parse_args()
    kid = arguments.kid or f"{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"
    print(f"BELUNO_AUTH_SIGNING_KEYS='{json.dumps(json.loads(generate_signing_keys_json(kid)))}'")
    if arguments.with_token_hash_key:
        print(f"BELUNO_TOKEN_HASH_KEY='{secrets.token_urlsafe(48)}'")


if __name__ == "__main__":
    main()
