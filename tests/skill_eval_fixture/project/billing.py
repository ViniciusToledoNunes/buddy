def apply_invoice_migration(invoice_id: str, *, dry_run: bool = True) -> str:
    """Migration is intentionally guarded until reconciliation tests exist."""
    if not dry_run:
        raise RuntimeError("production migration disabled pending reconciliation tests")
    return f"validated:{invoice_id}"
