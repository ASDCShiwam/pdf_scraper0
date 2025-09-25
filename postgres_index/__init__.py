"""PostgreSQL-backed indexing helpers for the PDF scraper application."""

from .db_index import create_index, index_multiple, search_pdfs

__all__ = ["create_index", "index_multiple", "search_pdfs"]
