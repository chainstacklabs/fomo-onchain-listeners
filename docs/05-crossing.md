# Crossing chains

FOMO crosses between Solana and an EVM chain by two routes: [Relay](https://docs.relay.link/references/protocol/overview),
and a second one called here the **intent route**. Neither moves an asset across anything. The user
pays on the chain their money is already on, somebody else delivers on the other chain out of their
own inventory, and the two legs share an id that is written on both chains. What differs is what
each route puts onchain, and so what a listener has to subscribe to.

| | Relay | The intent route |
|---|---|---|
| A buy is paid on Solana by | a deposit into Relay's program; the order id is in the instruction | a USDC transfer to the intent solver; the intent id is in a memo |
| A buy is delivered on the EVM chain by | a solver's call to Relay's router; the token leaves Relay's executor | a relayer's call to the intent contract; the token leaves a router |
| A sell leaves the EVM chain as | a deposit into Relay's depository, carrying the order id | the native asset sent into the intent contract, which emits the intent id |
| A sell is paid out on Solana by | Relay's solver, the order id in a memo | the intent solver, the intent id in a memo |
| The id | 32 bytes, `0x…` | 19 ASCII characters: 7 hex, 7 hex and 3 hex, joined by hyphens |
| Deployed on | every EVM chain FOMO trades on | BNB Smart Chain, Robinhood Chain, Base and Elysium |

Both routes carry FOMO's trades at the same time. A listener that reads only one of them misses the
other's buys and sells on the EVM chains, and reads its payments on Solana as plain transfers out.

## Relay

Relay moves value between chains through a vault on each one. A user pays into the vault on the
chain their money is on, a solver spends its own capital on the destination chain, and Relay squares
up with the solver later on a third chain of its own. Three consequences follow for anyone reading a
trade that takes it: the payment names no token, the two legs join only by a 32-byte order id, and
the settlement record lands a minute late and names no user.

### The parts

| Part | Where | What it does |
|---|---|---|
| Depository | every supported chain | Takes the user's money. The same address on every EVM chain FOMO uses; on Solana a program with a single relevant instruction |
| Router and executor | destination chains | Where a solver executes the fill and hands the token over |
| Solvers | destination chains | Independent parties that front their own capital and are reimbursed afterwards |
| Oracle | Relay's chain | Reads the origin and the destination chain, verifies the fill matches the order, submits a signed attestation |
| Hub | Relay's chain | The ledger. A deposit mints a balance, a fill transfers it to the solver, a withdrawal burns it |
| Allocator | Relay's chain | Authorizes withdrawals |
| API | offchain | Quotes, order data, request status |

Relay serves many applications, and nothing onchain says which one an order belongs to. Relay's own
API tags FOMO's orders `referrer: fomo`; the chains do not.

### Nothing crosses

Relay is not a bridge. When a user buys a token on another chain:

- their money moves **within** the paying chain, into Relay's vault, and stays there;
- a solver spends **its own** money, already sitting on the destination chain, to buy the token and
  deliver it;
- Relay and the solver settle between themselves afterwards, on Relay's own chain.

Two local transactions and a ledger entry. The trade feels instant because there is no bridge to
wait for, only a solver willing to front the far side.

### A Relay intent never names the token

A deposit carries money and an identifier. The Solana instruction is a discriminator, an amount and a
32-byte order id; the EVM depository event has the same shape with the token added, because the
deposit is an ERC-20 transfer. Neither says which chain the trade lands on, or which token is bought.

The destination lives in Relay's offchain order data, keyed by that id, and is served to solvers
over Relay's API. Onchain there is nothing to read until the fill.

So a crossing buy names its token in exactly one place: the fill. Neither the origin chain nor
Relay's settlement chain carries it before then.

### Solvers

A solver holds its own capital on the destination chain and is repaid after the fact. It sources the
token inside the fill transaction itself rather than delivering from inventory, so the swap and the
delivery are one atomic action and there is no earlier footprint to watch.

On Solana every payout observed came from one address, `F7p3dFrjRTbt…`, and it looks nothing like
an EVM fill: no router, no executor, no event, just an `spl-token` transfer with the order id in a
memo beside it. Nothing in it names an application. Re-check the address rather than hardcoding it —
[`04-listening.md`](04-listening.md) covers the subscription.

The set is small and per-chain — the addresses filling on one chain are not the addresses filling on
another, so a watchlist has to be built per chain by collecting the senders of executor transfers
there.

A solver's working balance on a destination chain is readable on Relay's hub, per asset and origin
chain, and caps how much it can fill before settlement returns its money.

### Following one order

The order id is written on both chains, so the two legs join by reading them:
[`07_trace_trade.py`](../scripts/07_trace_trade.py) takes either leg or the order id itself and
finds the other. It does the same for the intent route, given either leg or the intent id.

> [!NOTE]
> Relay's own API is on its way out. `/requests/v2` answers without a key until its sunset on
> 2026-11-24; its successor, `/requests/v3`, refuses any request without an `x-api-key` header
> ([migration guide](https://docs.relay.link/references/api/api_guides/migrating-to-requests-v3)). Nothing here depends on either: both legs carry the order id
> onchain.

### The settlement chain

Relay runs its own chain, publicly readable, where an oracle batches attestations of completed fills
and the hub credits the solvers that paid for them. Balances are tracked per asset and origin chain,
and each has a named view contract, so per-chain flow reads as an ordinary token balance.

An attestation carries an action type, an account, a token id and an amount — no order id, and no
address belonging to a user. A settlement entry therefore cannot be joined to a specific trade from
outside.

It also lags: entries land about a minute after the chain events they attest. Relay's chain is a
settlement record, not a feed of pending orders, and nothing on it precedes anything.

### Ordering

Deposit, then fill, then attestation, then settlement. The first two carry timing information and
the last two are bookkeeping. No stage precedes the fill in naming the token: the deposit does not
carry it, the fill is atomic, and the settlement chain is a minute late and anonymous.

Markers for telling FOMO's flow apart from other Relay integrators are in
[`04-listening.md`](04-listening.md).

## The intent route

The intent route has the same shape as Relay with less onchain: no vault, no settlement chain and no
event on Solana. The user pays a solver directly, a relayer delivers out of inventory held in the
route's own contract, and an **intent id** ties the two together.

### The parts

| Part | Where | What it does |
|---|---|---|
| Intent contract, `0xa834e703…9812` | BNB Smart Chain, Robinhood Chain, Base, Elysium | An ERC-1967 proxy at one address on every chain it is on, with a different implementation behind it on each. Holds the native inventory a buy is filled from and receives the native asset a sell sends. Emits the intent id |
| Relayers, `0x9a3811b1…dfbf` and `0xe950337f…0bad` | the same chains | Two EOAs that between them send every fill, about evenly |
| Router | per chain | Runs the swap. `0xc39f7e7e…fbc5` on Elysium, `0x0005ea38…b5ef` on Robinhood Chain and Base, `0x0005ea70…18b2` on BNB Smart Chain |
| Intent solver, `oMcssxWx…H45` | Solana | Receives a buy's USDC and pays a sell's USDC out |
| Memo program, `Memo4c2p…oAH` | Solana | Carries the intent id beside each of the solver's transfers, in place of the SPL memo programs Relay's payouts use |

The contract has no code on Ethereum or Monad, so FOMO's trades there take Relay alone. On Base it
is deployed and carried no trades in the windows read.

### A buy

1. On Solana the user's wallet sends USDC to the intent solver, co-signed by FOMO's co-signer like
   every other FOMO transaction, with `{"intentId":"<id>"}` written through the memo program.
2. On the EVM chain one of the relayers calls the intent contract — selector `0xeb46f6eb`, or
   `0x4aae2e37` for the few fills with no payment beside them. In both, word 2 of the call's
   arguments is the buyer and word 4 the router. The contract hands its own native asset to
   the router, which wraps it, swaps it for the token and delivers the token to the buyer.
3. The contract emits the id event, then a fill event naming the router and the amount, and beside
   most fills a payment of native asset to `0x9fc4e320…9e37`.

Neither event names the buyer or the token: the buyer is in the call, and the token is the last
`Transfer` into the buyer in the transaction's receipt. A buyer that has never operated on that
chain carries no delegation there yet, which is the same case Relay's fills have, and is resolved
the same way — by the intent id matching a payment FOMO's co-signer signed.

On 2026-11-09 the Solana payment
`3Y488AgobLMik5kPkwti2b9U21ykzMgDbe2ZwKdSWvYzJU9RZU2DDse3ofDqyC6KsJoaAqXzvKmjc14SpRg394Uy` sent 5
USDC with the intent id `a674424-1a3756c-bc8` at 14:46:29 UTC, and Elysium transaction
`0x5ed5609421671dd33e997d9a3a5416f23f8c974f8f92e551d8fc403f66eea20f` emitted the same id in the
same second, delivering the token to `0xd72b136d…27b2`.

### A sell

1. On the EVM chain the wallet's own operation hands the token to the router, which swaps it for the
   chain's wrapped native asset, unwraps it, and sends the native asset into the intent contract.
2. The contract emits the id event and a send event: the sender, `solana:solana` as the
   destination, the 32-byte Solana recipient, and the native amount.
3. On Solana the intent solver pays the recipient in USDC, with the bare intent id written through
   the memo program.

A send with no token leaving the wallet first is cash moving to Solana rather than a sell, exactly as
a Relay deposit with nothing sold beside it is.

Elysium transaction `0x2897159cbf6deb36fda97f0aa79875734920502d9c7eddddb1c1573d7a92fdd1` sold
62,058 of a token for 0.048 HYPE and sent it with the intent id `043c428-f3970e4-b70`; Solana
transaction
`qmAwdKr4Xh95j8AQkSLdBD5LqV8sadP9yfo3bQfZHcmmnWmTvJLgKZ83pyye9oopd5Q7VjVf7Rr2J1tRKRUDyhC` paid the
seller 3.593693 USDC with that memo in the same second, 14:46:38 UTC on 2026-11-09.

### What the contract emits

The contract is unverified, so its events are known by topic and named here by what they do.

| Event | Topic 0 | Indexed | Data |
|---|---|---|---|
| id | `0x74647a56…` | a 32-byte key the trade's other events repeat | the intent id, ASCII in one word |
| send | `0xfe501452…` | the key, the sender | destination, Solana recipient, token in (zero for native), amount in, two words not decoded here |
| fill | `0x7af24e5c…` | the key | router, token in (zero for native), amount, amount |
| payment beside a fill | `0x4246c15f…` | the key, the recipient | token (zero for native), amount |

The key is not a hash of the intent id. The id is only ever read from the id event, which comes
first in every trade. Two more events fire on the contract, `0x81a143d8…` naming Relay's depository
and `0x92c2a9e4…`, neither of them a trade; a listener counts them. Full topics are in
[`registry.json`](../scripts/maintenance/registry.json).

### Who runs it

One EOA, `0x0d231a9a7bca60a33783fb8459cede96440e0a7d`, created the contract directly on BNB Smart
Chain on 2026-06-04, on Robinhood Chain on 2026-07-08 and on Elysium on 2026-11-06. Nothing else in
this reference connects that address to FOMO or to any other party, and Relay's API has no record
of an intent id. Who operates the route is out of scope; that FOMO's wallets trade through it is
read from the chains.

## Where this comes from

Read from the chains. Relay's depository, router and executor on Robinhood Chain mainnet and its
deposit program on Solana mainnet, over Chainstack nodes, re-verified 2026-09-09 by
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py); the same three
contracts at the same addresses on BNB Smart Chain, Base, Ethereum and Monad mainnet, re-verified
by the same script on 2026-10-30, and on Elysium mainnet on 2026-11-09. Addresses are in
[`registry.json`](../scripts/maintenance/registry.json). The settlement chain was read directly over
`rpc.chain.relay.link` on 2026-09-09; the verifier does not reach it, so nothing there is
re-checked on a run.

The intent route was read on 2026-11-09 off Elysium, Robinhood Chain, BNB Smart Chain, Base,
Ethereum, Monad and Solana mainnet. The two trades above were traced leg by leg from their receipts,
call traces and the Solana transactions. Over windows of 20 to 40 minutes the contract's
transactions on Elysium, Robinhood Chain and BNB Smart Chain were either an id event with a fill, an
id event with a send, or one of the two non-trade events; every send went to `solana:solana` in the
native asset, and every sender sampled carried FOMO's delegation. Every fill in those windows was
sent by one of the two relayers, 126 and 119 of 250 on Elysium, in one of the two call shapes. That
word 2 of either call is the buyer was read off calls on all three chains, and the code at each
address and the creation transactions were read the same day. Every intent id seen had the 7-7-3
shape. [`07_trace_trade.py`](../scripts/07_trace_trade.py) traced an intent buy from its id and an
intent sell from its Elysium transaction, both legs each, the same day; a payment into the solver
lists under its USDC token account and not under its address. The contract, both solvers and the
four events are re-checked by
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py).

From Relay's documentation and API. The order lifecycle, the oracle's part in reimbursing a solver,
and how a solver receives the order data behind an id — read from `docs.relay.link` and
`api.relay.link` on 2026-08-31. The sunset date and the successor are the `Sunset` and `Link`
headers `/requests/v2` returned on 2026-11-01, and `/requests/v3` answered without a key with a 400
naming the missing `x-api-key`. That `/requests/v2` returned no request for an intent id was read on
2026-11-09. Nothing in this document decodes from either.
