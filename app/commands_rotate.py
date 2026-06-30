"""CLI: rotate the platform's CLOUD_TOKEN_KEY by re-encrypting every encrypted
column from the old key to a new one.

Usage:
    flask rotate-cloud-token-key --old <old-key> --new <new-key> [--dry-run]

The new key is NOT written anywhere — once the command exits successfully,
the operator must update CLOUD_TOKEN_KEY in `.env` and restart the workers
so subsequent reads/writes use the new key.

Strategy:
    - Bypass the EncryptedString TypeDecorator by issuing raw SQL SELECT/UPDATE
      so we can pass arbitrary Fernet keys (the TypeDecorator hardcodes the env
      key via _fernet()).
    - Idempotent: every row is first probed with the NEW key. If decryption
      succeeds, the row is already migrated and is skipped. Re-running the
      command after a partial failure is safe.
    - Per-table transaction (commit after each table). A crash in the middle
      leaves earlier tables migrated and later tables intact — re-run with the
      same args to finish.
    - Refuses to run if --old equals --new (no-op + risk of operator mistake).
"""
import click
from cryptography.fernet import InvalidToken
from flask.cli import with_appcontext
from sqlalchemy import text


# (table, [columns]) — every column protected by Fernet today, both raw _enc
# and EncryptedString-backed. Keep this list in sync with the model file
# whenever a new encrypted column is added.
_ENCRYPTED_COLUMNS = [
    ("cloud_connections", ["access_token_enc", "refresh_token_enc"]),
    ("org_instances", ["sso_secret_enc"]),
    ("organizations", ["ai_api_key_enc"]),
    ("oidc_providers", ["client_secret_enc"]),
    ("users", ["totp_secret"]),
    ("query_history", ["query", "answer_excerpt"]),
    ("saved_queries", ["name", "query"]),
    ("rag_feedbacks", ["query", "answer_excerpt", "comment"]),
    ("watch_alerts", ["keyword"]),
]


def _looks_like_fernet(value: str) -> bool:
    """Cheap pre-filter — Fernet tokens always start with the version byte 0x80
    base64-encoded as `gAAAA`. Anything else is plaintext (legacy pre-0014 row
    or empty value) and we leave it alone."""
    return isinstance(value, str) and value.startswith("gAAAA")


@click.command("rotate-cloud-token-key")
@click.option("--old", required=True, help="Current CLOUD_TOKEN_KEY in use (decrypts existing data).")
@click.option("--new", required=True, help="New CLOUD_TOKEN_KEY to migrate to (encrypts the data going forward).")
@click.option("--dry-run", is_flag=True, help="Walk every row and report counts without writing anything.")
@with_appcontext
def rotate_cloud_token_key(old: str, new: str, dry_run: bool) -> None:
    """Re-encrypt every encrypted DB column from --old key to --new key."""
    from app.ext import db
    from app.utils.crypto import _fernet_from_key

    if not old or not new:
        raise click.UsageError("--old and --new are both required.")
    if old == new:
        raise click.UsageError("--old and --new are identical — nothing to do.")

    fernet_old = _fernet_from_key(old)
    fernet_new = _fernet_from_key(new)

    grand_total = grand_migrated = grand_skipped_already = grand_skipped_plain = 0

    click.echo(f"{'[DRY RUN] ' if dry_run else ''}Rotation CLOUD_TOKEN_KEY → starting")
    click.echo(f"  Tables: {len(_ENCRYPTED_COLUMNS)}; columns: {sum(len(c) for _, c in _ENCRYPTED_COLUMNS)}")
    click.echo()

    for table, columns in _ENCRYPTED_COLUMNS:
        # Skip tables that don't exist (e.g. running before a migration on a
        # fresh DB) — the column list is the source of truth, but we don't
        # want a missing-table error to abort the whole rotation.
        try:
            db.session.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))  # nosec B608 — table from hardcoded _ENCRYPTED_COLUMNS, no user input
        except Exception as exc:
            db.session.rollback()
            click.secho(f"  ⊘ {table}: skipped ({type(exc).__name__})", fg="yellow")
            continue

        col_list = ", ".join(["id"] + columns)
        rows = db.session.execute(text(f"SELECT {col_list} FROM {table}")).fetchall()  # nosec B608 — table+columns from hardcoded list
        t_total = len(rows) * len(columns)
        t_migrated = t_already = t_plain = 0

        for row in rows:
            row_id = row[0]
            for offset, col in enumerate(columns, start=1):
                value = row[offset]
                if value is None or value == "":
                    continue
                if not _looks_like_fernet(value):
                    t_plain += 1
                    continue
                # Probe with new key first — already migrated rows are skipped.
                try:
                    fernet_new.decrypt(value.encode())
                    t_already += 1
                    continue
                except InvalidToken:
                    pass
                # Decrypt with old, re-encrypt with new
                try:
                    plaintext = fernet_old.decrypt(value.encode())
                except InvalidToken:
                    click.secho(
                        f"  ⚠ {table}.{col} id={row_id}: decrypt with --old failed (skipped)",
                        fg="red",
                    )
                    continue
                new_ct = fernet_new.encrypt(plaintext).decode()
                if not dry_run:
                    db.session.execute(
                        text(f"UPDATE {table} SET {col} = :v WHERE id = :id"),  # nosec B608 — table+col from hardcoded list, values bound
                        {"v": new_ct, "id": row_id},
                    )
                t_migrated += 1

        if not dry_run:
            db.session.commit()

        symbol = "✔" if t_migrated else "·"
        click.echo(
            f"  {symbol} {table:<22} migrated={t_migrated:<5} "
            f"already_new={t_already:<4} plaintext_skipped={t_plain:<4} total_cells={t_total}"
        )
        grand_total += t_total
        grand_migrated += t_migrated
        grand_skipped_already += t_already
        grand_skipped_plain += t_plain

    click.echo()
    click.echo("=" * 70)
    click.secho(
        f"{'[DRY RUN] ' if dry_run else ''}TOTAL  "
        f"migrated={grand_migrated}  already_new={grand_skipped_already}  "
        f"plaintext={grand_skipped_plain}  cells_scanned={grand_total}",
        fg="green",
        bold=True,
    )
    if dry_run:
        click.echo()
        click.echo("Dry run — no row updated. Re-run without --dry-run to apply.")
    else:
        click.echo()
        click.secho(
            "✅ Rotation complete. NEXT: update CLOUD_TOKEN_KEY in your .env to the new value\n"
            "   and restart the web + worker containers so subsequent reads/writes use the new key.",
            fg="cyan",
        )


def init_app(app):
    app.cli.add_command(rotate_cloud_token_key)
