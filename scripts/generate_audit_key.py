"""Generate an Ed25519 signing key for audit checkpoints + evidence bundles (Part 12).

    python scripts/generate_audit_key.py                    # private key -> AUDIT_SIGNING_KEY_PATH
    python scripts/generate_audit_key.py --out keys/k2.pem  # rotation: then point AUDIT_SIGNING_KEY_PATH at it

The private key is written outside git (outputs/ is gitignored; never commit it). The public key is written to
docs/audit-public-keys/<key_id>.pem: COMMIT that file, and keep old ones (they verify old checkpoints).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.audit.keys import generate
    from app.config import get_settings

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--public-dir", type=Path, default=ROOT / "docs" / "audit-public-keys")
    ap.add_argument("--force", action="store_true", help="overwrite an existing private key file")
    args = ap.parse_args()
    out = args.out or get_settings().audit_signing_key_path
    if out.exists() and not args.force:
        print(f"{out} already exists (use --force to overwrite, or --out for a new key)", file=sys.stderr)
        return 1
    kid = generate(out, args.public_dir)
    print(f"key_id {kid}\n  private: {out}  (keep secret, never commit)\n  public:  {args.public_dir / (kid + '.pem')}"
          "  (commit this)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
