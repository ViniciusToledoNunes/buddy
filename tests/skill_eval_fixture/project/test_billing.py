from billing import apply_invoice_migration


def test_dry_run_is_available():
    assert apply_invoice_migration("inv-42") == "validated:inv-42"
