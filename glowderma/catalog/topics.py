"""Outbox topics `catalog` announces to the modules above it (they register the handlers)."""

# A Billz import applied every row (``tasks.run_billz_import``): ``ProductStock`` was read again. commerce
# settles the sold units Billz's stock now reflects (``commerce.services.settle_billz_reservations``).
STOCK_SYNCED = "catalog.stock_synced"
