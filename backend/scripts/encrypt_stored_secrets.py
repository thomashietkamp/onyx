"""Encrypt every stored secret under the current write key.

Run it after you turn encryption on (ENCRYPTION_KEY_SECRET) or switch
ENCRYPTION_WRITE_KEY_ID. It rewrites values that are plain text or under an
older key. It is safe while Onyx runs, and you can run it again.

Usage (docker):
    docker exec -it onyx-api_server-1 python -m scripts.encrypt_stored_secrets --dry-run
    docker exec -it onyx-api_server-1 python -m scripts.encrypt_stored_secrets

Pass --tenant-id to target a single tenant instead of all tenants.
"""

import argparse
import os
import sys

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(parent_dir)

from onyx.db.engine.sql_engine import (  # noqa: E402
    SqlEngine,
    get_session_with_tenant,
)
from onyx.db.engine.tenant_utils import get_all_tenant_ids  # noqa: E402
from onyx.db.models import Base  # noqa: E402
from onyx.db.secret_reencryption import reencrypt_all  # noqa: E402
from onyx.utils.encryption import (  # noqa: E402
    current_write_key_id,
    validate_encryption_configuration,
)
from onyx.utils.variable_functionality import set_is_ee_if_available  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tenant-id")
    args = parser.parse_args()

    set_is_ee_if_available()
    validate_encryption_configuration()
    write_key_id = current_write_key_id()
    if write_key_id is None:
        sys.exit(
            "No write key is configured, so there is nothing to encrypt under. "
            "Set ENCRYPTION_KEY_SECRET or ENCRYPTION_WRITE_KEY_ID first."
        )

    SqlEngine.init_engine(pool_size=2, max_overflow=0)
    tenant_ids = [args.tenant_id] if args.tenant_id else get_all_tenant_ids()
    mode = "DRY RUN: " if args.dry_run else ""
    print(f"{mode}encrypting stored secrets under key id {write_key_id!r}")

    for tenant_id in tenant_ids:
        with get_session_with_tenant(tenant_id=tenant_id) as db_session:
            for report in reencrypt_all(db_session, Base.metadata, args.dry_run):
                if report.total == 0:
                    continue
                print(
                    f"[{tenant_id}] {report.table}.{report.column}: "
                    f"{report.rewritten} rewritten, {report.up_to_date} up to date, "
                    f"{report.changed_during_run} changed during the run "
                    f"(of {report.total})"
                )


if __name__ == "__main__":
    main()
