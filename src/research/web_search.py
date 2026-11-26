"""Web research for Iron Scalper, through the official Exa SDK.

Nothing here feeds a trading decision: the strategies read price data and the
ML gate reads features computed from that same data. This module is for the
operator - it pulls recent, citable pages about gold, the dollar or a macro
event so a call can be argued from something other than memory.

Every search asks Exa for highlights, so a result arrives with the sentences
that actually matched the question rather than a bare link. That keeps the
returned payload small: no page bodies, no summaries, just the relevant parts.

The API key is read from ``EXA_API_KEY`` and is never written to the database,
a log line or a file.
"""

import argparse
from datetime import datetime, timedelta
import json
import os
import sys

from exa_py import Exa

KEY_VARIABLE = "EXA_API_KEY"
DEFAULT_RESULTS = 5
DEFAULT_NEWS_DAYS = 3

# How each traded symbol is described in words. Exa ranks by meaning, so the
# query reads like a question instead of a ticker lookup.
SYMBOL_NAMES = {
    "XAUUSD": "gold",
    "GBPUSD": "GBP/USD sterling",
    "US30": "Dow Jones US30",
}


class MissingApiKey(RuntimeError):
    """Raised when ``EXA_API_KEY`` is not set in the environment."""


def api_key():
    """The configured key, or an empty string when it has not been set."""
    return (os.environ.get(KEY_VARIABLE) or "").strip()


def client():
    """An Exa client bound to the key in the environment."""
    key = api_key()
    if not key:
        raise MissingApiKey(
            f"{KEY_VARIABLE} is not set. Create a key at https://dashboard.exa.ai, "
            f"set {KEY_VARIABLE} in the environment, then run this again."
        )
    return Exa(api_key=key)


def _published_since(days):
    """A ``YYYY-MM-DD`` cutoff ``days`` days back, or None when unbounded."""
    if days is None:
        return None
    days = int(days)
    if days < 1:
        raise ValueError("days must be at least 1")
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def search(query, num_results=DEFAULT_RESULTS, include_domains=None, category=None,
           start_published_date=None):
    """Run one Exa search and return plain result records, best match first."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query cannot be empty")
    if int(num_results) < 1:
        raise ValueError("num_results must be at least 1")

    response = client().search(
        query,
        type="auto",
        num_results=int(num_results),
        include_domains=list(include_domains) if include_domains else None,
        category=category,
        start_published_date=start_published_date,
        contents={"highlights": True},
    )

    records = []
    for result in getattr(response, "results", None) or []:
        records.append({
            "title": getattr(result, "title", None),
            "url": getattr(result, "url", None),
            "published_date": getattr(result, "published_date", None),
            "author": getattr(result, "author", None),
            "score": getattr(result, "score", None),
            "highlights": list(getattr(result, "highlights", None) or []),
        })
    return records


def market_news(symbol="XAUUSD", days=DEFAULT_NEWS_DAYS, num_results=DEFAULT_RESULTS,
                include_domains=None):
    """Recent news for one of the traded symbols, with highlights."""
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol cannot be empty")
    name = SYMBOL_NAMES.get(symbol, symbol)
    query = f"{name} ({symbol}) price outlook and market news"
    since = _published_since(days)
    return {
        "query": query,
        "symbol": symbol,
        "since": since,
        "results": search(
            query,
            num_results=num_results,
            include_domains=include_domains,
            category="news",
            start_published_date=since,
        ),
    }


def render(payload):
    """A readable report for the terminal, or an honest 'nothing found'."""
    lines = [f"Query: {payload['query']}"]
    if payload.get("since"):
        lines.append(f"Published since: {payload['since']}")
    results = payload.get("results") or []
    if not results:
        lines.append("")
        lines.append("No results returned.")
        return "\n".join(lines)

    for index, result in enumerate(results, start=1):
        lines.append("")
        lines.append(f"{index}. {result.get('title') or '(untitled)'}")
        lines.append(f"   {result.get('url') or '(no url)'}")
        details = [str(value) for value in (result.get("published_date"), result.get("author")) if value]
        if details:
            lines.append(f"   {' | '.join(details)}")
        for highlight in result.get("highlights") or []:
            lines.append(f"   - {' '.join(str(highlight).split())}")
    return "\n".join(lines)


def main(argv=None):
    """Search once from the command line. Read-only: places no orders."""
    parser = argparse.ArgumentParser(
        prog="python -m src.research.web_search",
        description="Search the web through Exa for market research.",
    )
    parser.add_argument("query", nargs="?", help="free-text question; omit it to search recent symbol news")
    parser.add_argument("--symbol", default="XAUUSD", help="traded symbol for the news query (default: XAUUSD)")
    parser.add_argument("--days", type=int, default=None,
                        help=f"only pages published in the last N days "
                             f"(default: {DEFAULT_NEWS_DAYS} for the news query, unbounded for a free-text query)")
    parser.add_argument("--results", type=int, default=DEFAULT_RESULTS, help=f"pages to return (default: {DEFAULT_RESULTS})")
    parser.add_argument("--domain", action="append", dest="domains", help="restrict to a domain; repeatable")
    parser.add_argument("--json", action="store_true", help="print the records as JSON instead of text")
    args = parser.parse_args(argv)

    try:
        if args.query:
            payload = {
                "query": args.query,
                "since": _published_since(args.days),
                "results": search(
                    args.query,
                    num_results=args.results,
                    include_domains=args.domains,
                    start_published_date=_published_since(args.days),
                ),
            }
        else:
            payload = market_news(
                args.symbol,
                days=args.days if args.days is not None else DEFAULT_NEWS_DAYS,
                num_results=args.results,
                include_domains=args.domains,
            )
    except MissingApiKey as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Exa search failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2) if args.json else render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())