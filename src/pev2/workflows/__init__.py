"""The four workflow definitions of section 4. Importing the package registers them with the runtime."""

from pev2.workflows import invoice_processing, price_deviation, production_purchase, supplier_query  # noqa: F401

__all__ = ["invoice_processing", "price_deviation", "production_purchase", "supplier_query"]
