# The recorded feed

Every listener writes one JSON object per trade to `scripts/runs/<script>-<time>.jsonl`, flushed as
it is written. The terminal is a hundred columns aimed at the eye; this file is the record. Amounts
keep their symbol, wallets and mints are whole, and two rows join on `order_id`.

The `<time>` in the filename is UTC, the same clock as every row's `at`, so a row and the file it
came from can be read against each other without an offset in between.

## Fields

| Field | Type | What it is |
|---|---|---|
| `t` | float | seconds since this listener started, to a millisecond. Restarts at zero every run |
| `at` | string | wall clock, ISO 8601 with a UTC offset. Use this to join anything outside the file |
| `run` | string | the run that produced the row, matching the filename. Survives a concatenation |
| `chain` | string | `SOL` for Solana; `RH`, `BSC`, `BASE`, `ETH`, `MON` or `ELY` for an EVM chain |
| `kind` | string | `BUY` `SELL` `PAY` `SWAP` `FILL` `IN` `OUT` `OTHER`, defined in `shared/feed.py` |
| `gave` | string | what left the wallet, as `<amount> <symbol>`, or one of the sentences below |
| `got` | string | what arrived, same shape |
| `who` | string | the wallet in full: a 0x address on an EVM chain, the same on all of them, base58 on Solana |
| `app` | string | `fomo`, from the wallet's delegation. `eoa` on a buy delivered to a wallet with no delegation on that chain yet, kept because its order id matches a FOMO payment. Empty on `FILL`, which no chain attributes |
| `note` | string | the venues a trade ran through, joined by `+` |
| `where` | string | `blk <number>` on an EVM chain, `slot <number>` on Solana |
| `key` | string | `<transaction>#<log index>` on an EVM chain, the signature on Solana |
| `order_id` | string | the id the trade's route writes on both chains: Relay's 32-byte order id as `0x…`, or the intent route's id as 19 characters, 7 hex, 7 hex and 3 hex joined by hyphens. Empty when the trade did not cross, `?` when a Relay fill's transaction could not be fetched or an intent trade's receipt held no id event for it. `?` is not an id and joins nothing. An intent trade whose receipt could not be fetched at all is counted and not recorded by the log listener, and swept again by the block listener |

## Amounts

An amount is `<decimal> <symbol>`, six decimal places, human units. The symbol is a ticker when the
chain names one and the mint itself when it does not — 44 characters of base58, whole, because six
characters of a mint name no token and two mints sharing a prefix would merge into one row.

Four values are not figures, and a parser has to expect them:

| Value | What happened |
|---|---|
| `` (empty) | the trade has no side there: a `BUY` gave nothing, an `IN` gave nothing |
| `an unnamed token` | a token moved and the chain does not name it. Every crossing payment says this on the side it bought, because the deposit carries no token |
| `nothing the wallet owns moved` | a transaction arrived, none of the wallet's balances changed |
| `? <integer>`, or `?` alone | the decimals lookup failed, so the value is **raw base units** or absent. Scaling it by a guess would print a number that looks right and is wrong |

Filter on a leading `?` before summing anything.

## Joining the two legs

`order_id` is the key. A buy is a Solana `PAY` and a `BUY` on an EVM chain; a sell is a `SELL` on
an EVM chain and a Solana `FILL`; cash moving to Solana is a `PAY` on an EVM chain and a Solana
`FILL`. Both legs carry the same id, and the id is unique: 19,681 rows recorded on every chain
held no duplicate on either side of any of the three joins.

```python
import pandas as pd
rows = pd.read_json("scripts/runs/00_listen_fomo-<time>.jsonl", lines=True)
crossed = rows[~rows.order_id.isin(["", "?"])]
buys = crossed[(crossed.chain == "SOL") & (crossed.kind == "PAY")].merge(
    crossed[(crossed.chain != "SOL") & (crossed.kind == "BUY")], on="order_id", suffixes=("_paid", "_got")
)
```

The two legs land within about two seconds of each other, so a window of ten is generous. A leg
whose partner fell outside the run has no match; `00_listen_fomo.py` counts those at the end as
*still in flight* and *sells with no payout seen*.

`key` is the dedup key. It is derived from the chain, not from the run, so the same trade recorded
by two listeners carries the same `key`.

## Counting volume

`maintenance/volume.py` totals what these files record, in dollars, over a window, split by the
chain each trade ran on:

```bash
uv run scripts/maintenance/volume.py 12
```

Every trade is counted once, on the leg that names its size in cash. A Solana swap names both its
sides. A buy names its cash where that cash left — the `PAY` — and names its token on the other
chain, so the amount is read from one leg and the chain from the other. A sell is readable end to
end on its EVM chain: the token left the wallet and the proceeds went into a route — Relay's
depository, or the intent route's contract — in the same transaction. A crossing trade therefore lands on one chain and not both, and the payout
that settles a sell on Solana is that same sell arriving a second later rather than more volume —
it is reported as a settlement check under the table.

| Row | What it holds |
|---|---|
| Solana | swaps that never left it |
| each EVM chain | crossing trades both ways: buys delivered there, sells swapped there |
| `chain unknown` | Solana payments whose delivery was not matched |

A payment on an EVM chain that a Solana payout settles is cash moving to Solana, not a trade, so it
is no volume and is printed as a count beneath the table. One whose other leg was not matched is
counted there too: most such payments are cash moving, so none of them is valued as a trade. A
`BUY` with no order id was paid with cash already on its EVM chain, inside the wallet's own
operation; the row names the token but not the cash spent, so it is counted unpriced beneath the
table rather than valued. A `BUY` whose order id is `?` did cross, but its payment cannot be found
by it, so the buy is counted beneath the table; a Solana payment for it, if the run holds one, is
valued under `chain unknown`.

`chain unknown` is payment value the recordings cannot place, not volume proven to be a trade. A
payment carries an order id and an amount and no destination, so the destination is the delivery on
the far side. With every EVM chain FOMO trades on listened to, a payment lands in this row when its
far leg fell outside the run, when the run had a chain switched off, when the delivery is on a
chain this repository does not read — the Hyperliquid perpetuals in FOMO's catalog are out of its
scope ([`03-tokens-and-liquidity.md`](03-tokens-and-liquidity.md)) — or when it is a withdrawal:
USDC paid by Relay's solver to a plain address on an EVM chain, with the order id in the calldata
and no token bought. It is counted and named rather than dropped; every chain above it is a floor
and this row a ceiling.

A sell is missing rather than unknown when the run was not listening to the chain it ran on: what
marks a trade as FOMO's sits on that chain, so its payout on Solana has nothing to match against
and is counted with every other application's. A run with every chain on misses none.

The settlement check under the table reads both ends of the same sells. Across the recordings held
on 2026-09-15 the payout on Solana was 0.62% smaller than the deposit on Robinhood Chain, which is
what Relay and the solver keep on a sell — measured off the two legs, not quoted.

A stablecoin is worth a dollar and nothing else is priced, which values all but a per-mille of the
rows. Amounts are already scaled by the decimals each token reports, so an eighteen-decimal
stablecoin on BNB Smart Chain sums with a six-decimal one elsewhere. A native-asset sell, a
token-for-token swap, and an amount the chain would not decode are counted unpriced under the table
rather than valued at a guess.

A window is not coverage. A file holds only what its own run was listening to — minutes inside a
window of hours — so the report prints the time actually watched beside the window asked for, and a
window with no run in it is empty rather than zero.

## What the file is not

Only `00_listen_fomo.py` records every chain, so only its files join without a merge across files.

The `PAY` and `FILL` streams are Relay's, not FOMO's: they carry every application settling through
the same contracts. A row becomes FOMO's when its `order_id` matches a leg that `app` marks as
FOMO's — which is why `00_listen_fomo.py` counts *payouts for other applications* rather than
hiding them.

Every transport here subscribes. A window nobody was listening to is gone: nothing in this
repository re-reads a block range into this shape, and a file records only what its own run saw.

## Where this comes from

Read from the chains. The field list and the sentences are what `scripts/shared/feed.py` writes;
the leg latency and the absence of duplicate ids were measured over 40,571 rows recorded by the
listeners in this repository on 2026-09-09, and the shapes are the ones
`maintenance/verify_registry.py` re-verified against both mainnets the same day. What Relay keeps
on a sell was measured on 2026-09-15 over the sells recorded on Robinhood Chain and the payouts
recorded on Solana. The EVM chain codes, and payments on EVM chains settled as cash on Solana, are
from recordings of every chain on 2026-10-30. The uniqueness of the order id was measured again
on 2026-11-01 over 19,681 rows recorded on every chain on 2026-10-30, across Solana payments and
fills and EVM buys, sells and payments. The withdrawals were read on 2026-11-01: four Solana
payments from a seven-minute recording of every chain on 2026-10-30 were settled by transactions on
Ethereum and Base whose calldata carries the payment's order id and whose only USDC `Transfer` pays
an address with no code, for slightly less than the payment.
