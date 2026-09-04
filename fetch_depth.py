#!/usr/bin/env python
"""Download CME order-book depth, after showing what it will cost.

    # what would a week of front-month gold cost?
    python fetch_depth.py --start 2026-08-24 --end 2026-08-31

    # same query, actually download it
    python fetch_depth.py --start 2026-08-24 --end 2026-08-31 --confirm

WHY NOT THE WEB DOWNLOADER

The batch builder on the site selects a PRODUCT, not a contract. `GC` there
means every gold future at once - twenty-odd expiries plus the calendar
spreads between them, whose prices are negative and whose books describe
nothing you want. That is why a month of it comes to 54.8 GB. The API takes a
symbol, so `GC.v.0` - the front month by volume - can be asked for on its own.

WHY THE ESTIMATE COMES FIRST, ALWAYS

Requests are non-refundable, and the free credit arrives once. So this script
will not download anything unless `--confirm` is passed, and it prints the
cost and the size before it does. Estimating is free; guessing is not.

`--max-cost` is a second brake with a different job. The estimate is checked
against it and the run stops if it is exceeded, so a mistyped year - 2016
instead of 2026 - fails on a number rather than succeeding on a bill.

WHY GC.v.0 AND NOT GC.c.0

`v` rolls to whichever expiry traded the most yesterday; `c` rolls on the
calendar. Gold's volume moves to the next contract about a month before the
old one expires, so `c` spends that month pointed at a contract nobody is
trading, whose book is thin and whose depth means nothing. For order-book
work the difference is the whole measurement.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DATASET = "GLBX.MDP3"
DEFAULT_SYMBOL = "GC.v.0"
DEFAULT_SCHEMA = "mbp-10"


def human(nbytes: int) -> str:
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.2f} {unit}"
        size /= 1024
    return f"{size:,.2f} TB"


def _looks_like_key(value: str) -> bool:
    return bool(value) and value.isascii() and value.startswith("db-")


def _from_dotenv(name: str, path: Path | None = None) -> str:
    """Read one variable from a .env file, without a dependency.

    The file is `KEY=value` a line, `#` starts a comment, quotes are stripped.
    Nothing here writes to it and nothing prints its contents: the whole point
    of the file is that the secret is somewhere the code can reach and a
    screenshot cannot.
    """
    path = path or Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return ""
    # utf-8-sig, not utf-8. PowerShell's `Set-Content -Encoding utf8` writes a
    # byte-order mark, so the first key in a .env written on Windows arrives
    # as '\ufeffDATABENTO_API_KEY' and matches nothing. The file looks
    # perfect in every editor; three invisible bytes decide whether it works.
    # utf-8-sig strips the mark when present and behaves as utf-8 when not.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        found, _, value = line.partition("=")
        if found.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def _api_key() -> str:
    """The key from the environment, checked before it reaches the network.

    An unset key, or a key that is not a key, has to fail HERE. Handing a
    placeholder to `requests` produces a latin-1 codec error thrown from
    inside its auth handler, five frames deep, naming a character position -
    which describes the encoding of the mistake rather than the mistake. The
    first run of this script failed exactly that way, because the instruction
    text had been pasted in place of the key. Twice now, counting the same
    thing happening to edgar.py's --contact.
    """
    # Both sources, tried in order, each remembered by name. The first
    # version took the environment variable if it was set at all, so a stale
    # `$env:` value from earlier in the shell session shadowed a perfectly
    # good .env and the error blamed "the key" without saying which key. A
    # message that cannot name its source sends you to edit the wrong file.
    candidates = [
        ("the DATABENTO_API_KEY environment variable",
         (os.environ.get("DATABENTO_API_KEY") or "").strip()),
        (f"{Path(__file__).resolve().parent / '.env'}",
         _from_dotenv("DATABENTO_API_KEY").strip()),
    ]
    usable = [(src, val) for src, val in candidates if _looks_like_key(val)]
    present = [(src, val) for src, val in candidates if val]

    if usable:
        source, key = usable[0]
        if present[0][1] != key:
            print(f"note: ignoring {present[0][0]} - it does not hold a key. "
                  f"Using {source}.\n", file=sys.stderr)
    else:
        source, key = (present[0] if present else ("nowhere", ""))
    how = ("Put it in .env beside this script, on its own line:\n"
           "  DATABENTO_API_KEY=db-xxxxxxxx...\n"
           "(.env is already in .gitignore and is not tracked.) Or set it "
           "for one shell session:\n"
           '  PowerShell:  $env:DATABENTO_API_KEY = "db-xxxxxxxx..."\n'
           "Get the key from databento.com/portal - API keys.\n"
           "It is never a command-line flag: a key on a command line ends up "
           "in shell history and, sooner or later, in a screenshot.")

    if not key:
        raise SystemExit(f"No API key found: DATABENTO_API_KEY is not set "
                         f"and no .env holds it.\n{how}")
    if _looks_like_key(key):
        return key
    if not key.isascii():
        raise SystemExit(
            f"The value in {source} is not a key - it contains non-ASCII "
            f"characters, so it is almost certainly instruction text that "
            f"got pasted instead of the key itself.\n{how}"
        )
    raise SystemExit(
        f"The value in {source} does not look like a Databento key: they "
        f"begin with 'db-'. Got something starting {key[:4]!r}.\n{how}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="UTC date, exclusive")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL,
                        help=f"default {DEFAULT_SYMBOL}: front month by volume")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA,
                        help="mbp-10 is ten levels a side and the cheapest "
                             "per GB of the depth schemas; mbo is the full "
                             "book and several times the size")
    parser.add_argument("--out", type=Path, default=Path("data/depth"))
    parser.add_argument("--max-cost", type=float, default=5.0,
                        help="refuse to download above this many USD")
    parser.add_argument("--confirm", action="store_true",
                        help="without this the script only estimates")
    args = parser.parse_args(argv)

    key = _api_key()

    try:
        import databento as db
    except ImportError:
        raise SystemExit("pip install databento")

    client = db.Historical(key)

    query = dict(
        dataset=DATASET,
        symbols=[args.symbol],
        schema=args.schema,
        stype_in="continuous" if ".v." in args.symbol or ".c." in args.symbol
        else "raw_symbol",
        start=args.start,
        end=args.end,
    )

    print(f"{'dataset':10}{DATASET}")
    print(f"{'symbol':10}{args.symbol}  (stype_in={query['stype_in']})")
    print(f"{'schema':10}{args.schema}")
    print(f"{'range':10}{args.start} -> {args.end}\n")

    size = client.metadata.get_billable_size(**query)
    cost = client.metadata.get_cost(**query)
    print(f"{'size':10}{human(size)}")
    print(f"{'cost':10}${cost:,.4f}")

    days = _days(args.start, args.end)
    if days:
        print(f"{'per day':10}{human(size / days)}  ${cost / days:,.4f}")
        print(f"\n$125 of credit at this rate is about "
              f"{125 / (cost / days):,.0f} day(s) of {args.symbol}.")

    if cost > args.max_cost:
        raise SystemExit(
            f"\nestimate ${cost:,.2f} is above --max-cost ${args.max_cost:,.2f}"
            f". Nothing was downloaded. Shorten the range, or raise the limit "
            f"deliberately."
        )

    if not args.confirm:
        print("\nEstimate only. Pass --confirm to download.")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    name = (f"{args.symbol.replace('.', '_')}_{args.schema}_"
            f"{args.start}_{args.end}.dbn.zst")
    target = args.out / name
    if target.exists():
        raise SystemExit(
            f"{target} already exists. Downloading it again would be billed "
            f"again; delete it first if that is really what you want."
        )

    print(f"\ndownloading to {target} ...")
    try:
        data = client.timeseries.get_range(**query)
    except Exception as error:
        # Databento answers a blocked request with 402
        # `account_insufficient_funds`, which reads as "your credit ran out"
        # and sends you to check a balance that is fine. The usual cause is
        # the monthly usage limit on the billing page - a cap you set
        # yourself, separate from the credit. The tell is a SMALLER request
        # failing after a larger one succeeded: a balance cannot do that, a
        # cap can.
        if "insufficient_funds" in str(error) or "402" in str(error):
            raise SystemExit(
                f"\nDatabento refused the request: {error}\n\n"
                f"This is usually the monthly usage limit, not the credit "
                f"balance. They are separate:\n"
                f"  credit    - the $125 that came with the account\n"
                f"  usage cap - a ceiling you set, at portal > Billing > "
                f"Usage-based access > Manage\n"
                f"Check the cap first, especially if a smaller request just "
                f"failed after a bigger one succeeded.\nEstimated cost of "
                f"this request was ${cost:,.4f}; nothing was charged."
            )
        raise
    data.to_file(target)
    print(f"wrote {target} ({human(target.stat().st_size)} on disk, "
          f"compressed)")
    return 0


def _days(start: str, end: str) -> float:
    from datetime import datetime
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
    except ValueError:
        return 0.0
    return max((b - a).total_seconds() / 86400, 0.0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
