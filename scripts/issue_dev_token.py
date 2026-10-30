import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--issuer", default="vm-controller")
    parser.add_argument("--subject", default="controller")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    print(jwt.encode({"iss": args.issuer, "aud": args.audience, "sub": args.subject,
                      "iat": now, "exp": now + timedelta(minutes=5)},
                     args.private_key.read_bytes(), algorithm="RS256"))


if __name__ == "__main__":
    main()