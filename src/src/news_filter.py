"""Backward-compatible import path for the economic news filter.

The maintained implementation lives at ``src.news_filter``. This module is
kept so existing integrations importing the historical nested path continue
working while the package layout is corrected.
"""
from src.news_filter import NewsStatus, check_news_status

__all__ = ["NewsStatus", "check_news_status"]
