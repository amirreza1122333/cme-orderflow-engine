"""history() against the live endpoint - paging, window edges, bar shape."""

from datetime import datetime, timedelta, timezone

from core.bitstamp_client import BitstampClient

client = BitstampClient()
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

for label, step, span in (
    ("5m  / 6 hours ", 300, timedelta(hours=6)),
    ("1h  / 10 days ", 3600, timedelta(days=10)),
    ("1d  / 2 years ", 86400, timedelta(days=730)),
    ("5m  / 5 days  ", 300, timedelta(days=5)),      # ~1440 bars: forces paging
):
    start = now - span
    bars = client.history("BTC/USD", step, start, now)
    if not bars:
        print(f"{label}  NO DATA")
        continue

    gaps = {
        int((bars[i + 1].start - bars[i].start).total_seconds())
        for i in range(len(bars) - 1)
    }
    print(
        f"{label}  bars={len(bars):5}  "
        f"{bars[0].start:%Y-%m-%d %H:%M} -> {bars[-1].start:%Y-%m-%d %H:%M}  "
        f"spacing={sorted(gaps)[:3]}"
    )

print("\nnewest 5m bar:")
b = client.history("BTC/USD", 300, now - timedelta(hours=2), now)[-1]
print(f"  {b.start:%Y-%m-%d %H:%M}  O {b.open}  H {b.high}  L {b.low}  C {b.close}  V {b.volume}")

print("\nunsupported step:")
try:
    client.history("BTC/USD", 137, now - timedelta(days=1), now)
except Exception as exc:
    print(f"  {type(exc).__name__}: {exc}")
