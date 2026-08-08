# Westerville Utilities for Home Assistant

A [HACS](https://hacs.xyz/) custom integration that logs into the
Westerville, Ohio utility billing portal (`billpay.westerville.org`) with
your own account credentials and brings electric + water usage and billing
data into Home Assistant -- including the Energy dashboard.

This is not affiliated with, endorsed by, or supported by the City of
Westerville. It's an independent integration built by reverse-engineering
the public customer portal; see [Known limitations](#known-limitations)
below.

## What you get

- One sensor per electric/water meter showing current cumulative usage
  (`device_class: energy` / `water`, `state_class: total_increasing`).
- Full historical usage -- hourly and daily where the portal's
  advanced-meter (AMI) data is available, monthly billed usage further
  back -- imported into Home Assistant's long-term statistics, so it shows
  up correctly in **Settings -> Dashboards -> Energy** once you add the
  Westerville statistics there.
- One sensor per account with your most recent bill amount, due date,
  billing period, service address, and account holder name as attributes.
- Bill amounts are also imported as a long-term statistic (running total
  billed, one point per billing period) so bill cost is graphable
  month-over-month via a Statistics Graph card or Developer Tools ->
  Statistics -- not just visible as the latest bill's sensor state. Note:
  Westerville bills electric + water (+ sewer) together as one amount, so
  this isn't split per utility; linking it as "cost" for a single source in
  the Energy dashboard would overstate that source's cost.
- Multi-meter support: if your account has more than one meter for a
  utility (e.g. a separate EV-charging submeter), each shows up as its own
  device.

## Installation (HACS custom repository)

1. In Home Assistant, open **HACS -> Integrations -> ⋮ -> Custom
   repositories**.
2. Add this repository's URL, category **Integration**.
3. Install "Westerville Utilities" from HACS, then restart Home Assistant.
4. Go to **Settings -> Devices & services -> Add integration**, search for
   "Westerville Utilities", and enter the username/password you use to log
   in at `billpay.westerville.org`.

Credentials are stored using Home Assistant's built-in config entry
storage (encrypted at rest) -- this integration never writes its own
credential storage, and never logs credential values, even at debug level.

## Polling and backfill

The first sync after adding the integration does a **backfill**: monthly
billed usage (as far back as the portal's billing table shows, one
request), plus daily/hourly advanced-meter (AMI) data for as far back as
it's actually available -- bounded by default to 400 days for daily and 30
days for hourly, since AMI retention is short (see Known limitations) and
Home Assistant's own setup process has a timeout a very long backfill could
exceed. Every sync after that is **incremental**: only the last ~week or
so is re-checked (plus a small overlap, to catch late corrections the
portal sometimes makes to recent data), not the whole history again.

Data is fetched once every 24 hours by default. Westerville's own usage
data typically lags 1-2 days behind real time, so more frequent polling
wouldn't surface newer data -- it would just re-request the same numbers.

All three of these (polling interval, daily backfill depth, hourly
backfill depth) are configurable: **Settings -> Devices & services ->
Westerville Utilities -> Configure**. Changing them reloads the
integration and applies immediately -- no restart needed (unlike a code
update, this is just an options change, not new Python being loaded).

## Known limitations

- **Bill billing-period start dates are approximate.** The portal's bill
  history table has no explicit period-start column; it's inferred from
  the previous bill's date (or 30 days back, for the oldest bill fetched).
- **This integration scrapes server-rendered HTML and an inline chart
  script**, not a documented API -- if Westerville changes the portal's
  page layout, syncing can break until the integration is updated. Debug
  logging is verbose by design to make that easy to diagnose and report.
- **Advanced-meter (AMI) retention is short.** Hourly data is typically
  only available for the last ~2 weeks and daily for several months;
  older history falls back to monthly billed usage only.
- Built and tested against one real Westerville account; sewer-only meters
  and multi-account logins are handled in code but less thoroughly
  exercised than the electric/water/single-account path.

## Fast-follow (deliberately deferred from v1)

- A real async-native HTTP retry/backoff tuned specifically for the
  Westerville portal's own rate limits, beyond the current fixed politeness
  delay + generic exponential backoff.
- A formal automated test suite for the integration itself (the sibling
  [`utility-reader`](https://github.com/prajwal-skdlove/utility-reader)
  project, which this integration's client code was ported from, does have
  one against recorded HTTP fixtures).
- Broader multi-account/multi-meter real-world testing.

## Related project

This integration's Westerville client code was ported from
[`utility-reader`](https://github.com/prajwal-skdlove/utility-reader), a
local-first CLI that syncs the same data into a SQLite database. That
project's `PLAN.md` documents how the portal's login flow, AMI endpoints,
and HTML quirks were originally reverse-engineered.

## License

MIT -- see [LICENSE](LICENSE).
