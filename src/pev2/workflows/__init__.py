"""The four workflow definitions of section 4 plus the uncovered-case workflow. Importing the package registers them."""

from pev2.workflows import invoice_processing, price_deviation, production_purchase, supplier_query, unknown_case  # noqa: F401

__all__ = ["invoice_processing", "price_deviation", "production_purchase", "supplier_query", "unknown_case"]
