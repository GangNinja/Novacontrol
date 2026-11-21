"""Free filing/fundamentals sources for PI-19 research.

One adapter ships in this build: :class:`YahooFundamentalsAdapter`, a no-key,
no-subscription reader of Yahoo Finance's public fundamentals endpoints for the
NSE/BSE catalogue. It supplies statement payloads and company-profile facts to
the research context and never fabricates a missing filing.
"""

from .yahoo_fundamentals import (
    FILING_SOURCE_ID,
    FILING_SOURCE_NAME,
    FILING_SOURCE_TIER,
    FilingBundle,
    HttpReply,
    YahooFundamentalsAdapter,
)

__all__ = [
    "FILING_SOURCE_ID",
    "FILING_SOURCE_NAME",
    "FILING_SOURCE_TIER",
    "FilingBundle",
    "HttpReply",
    "YahooFundamentalsAdapter",
]
