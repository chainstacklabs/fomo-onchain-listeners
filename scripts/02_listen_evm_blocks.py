"""Listen to FOMO trades on one EVM chain block by block, behind a newHeads subscription.

New heads arrive over the socket and each sweep then fetches the logs of every block since
the last one, so this listener groups a block's activity together and the same code can be
pointed at history — at the cost of a few round trips per sweep. For a push-only stream
that fetches nothing, see 01_listen_evm_logs.py. A confirmed log is the first sight of a
trade either way. The chain is the first argument, one of shared/evm.py's `CHAINS`.

Emits BUY, SELL and PAY, defined in shared/feed.py. Here a buy is a Transfer out of Relay's
executor into an address carrying FOMO's delegation, and a sell is the user's own operation
moving a token into the executor — not classified by the presence of a `Swap` log, because
some venues emit their own shapes and a sell through them has no `Swap` at all. A pay is
money into the depository with no token leaving, and never names the token.

Buys and sells are separated by what moved inside the user's own log-index span, not by
the transaction: one handleOps bundle carries several users, so a hash join credits one
person's swap to another person's deposit. A buy also carries the Relay order id that paid
for it, in the last word of the fill's calldata.

FOMO's other route skips Relay, and a sweep reads its contract's events as well. A sell there
is the wallet's own operation sending the native asset into the contract, and a buy is a
relayer's call delivering the token. Neither event names the token, so a sweep also fetches
the receipt of every such trade in one more round trip, and the fill's calldata for its
buyer; both carry the intent id the Solana leg names in a memo.

An HTTPS endpoint can be served by several backends whose heads differ by a few blocks, and
one that has not reached the end of a range answers with silently fewer logs — Robinhood
Chain's does. Logs are therefore asked for block by block, by the hash the head
subscription delivered, and lookups are pinned to the block: a backend that lacks it
refuses instead of answering short, and the sweep is retried. Asking by hash also keeps a
reorganised block's logs out, since they belong to a hash nobody sweeps.

A wallet's EIP-7702 delegation names the application behind it and is reported as
`Trade.app`; FOMO is one tenant of Relay here, so that delegation is also the test that
keeps the other tenants out. A wallet that has never operated on this chain has no
delegation yet, so `claimed`, a test on the order id, lets a caller that knows the order is
FOMO's — 00_listen_fomo.py — have that delivery reported anyway. `listen()` here is the one
implementation of this transport; 00_listen_fomo.py imports it.

Reference: docs/04-listening.md (EVM chains).

    uv run scripts/02_listen_evm_blocks.py <chain> [seconds] [wallet ...]     default 120, FOMO only
"""

import asyncio
import json
import time
from functools import partial

import aiohttp
import websockets
from shared import cli, env
from shared.evm import (
    BURN,
    CHAINS,
    DECIMALS,
    DEPOSIT,
    DEPOSITORY,
    ENTRYPOINT,
    EXECUTOR_TOPICS,
    INTENT,
    INTENT_FILLED,
    INTENT_SENT,
    INTENT_TRADE_EVENTS,
    NATIVE_DEPOSIT,
    SYMBOL,
    TRANSFER,
    UNREAD_ORDER,
    USEROP,
    VENUES,
    address_in_topic,
    address_in_word,
    app_name,
    decode_string,
    delivered_to,
    erc20_deposit,
    intent_buyer,
    intent_ids,
    intent_sent,
    moved_from,
    native_deposit,
    order_from_calldata,
    padded_topic,
    quantity,
    span,
    sponsored,
    swaps_in,
    topic,
    trade_key,
    venues,
    where_of,
)
from shared.feed import UNNAMED, Trade
from web3 import Web3

RPC_TIMEOUT = aiohttp.ClientTimeout(total=10)
BATCH = 50  # calls per request; a catch-up sweep asks for hundreds of things at once
BEHIND = ("unknown block", "header not found")  # how a backend refuses a block it lacks
MAX_SWEEP_BLOCKS = 300  # if the listener falls behind, jump forward rather than queue up
STALL = 30  # seconds without a head before the subscription is called dead, at the least
OPEN_TIMEOUT = 30  # 00 opens every chain's socket at once, so a handshake can queue


def result(reply):
    """One batched reply's result, or raise. A sweep is wrong if any call in it silently failed."""
    if "result" not in reply:
        raise RuntimeError(reply.get("error") or "no reply")
    return reply["result"]


# ------------------------------------------------------------------- reading a sweep
# No network and no state: each takes the logs a sweep fetched and answers from them.
# The rest of the decoding is in shared/evm.py.


def operation_boundaries(op_logs):
    """The log index of every `UserOperationEvent`, per transaction.

    A handleOps bundle is one flat list of logs delimited by these, so they are what
    scopes a deposit to the user it belongs to. `span` turns them into a range.
    """
    boundaries = {}
    for log in op_logs:
        boundaries.setdefault(log["transactionHash"], []).append(int(log["logIndex"], 16))
    return boundaries


def swaps_by_transaction(swap_logs):
    """(log index, venue) per transaction, so a venue can be scoped to one operation."""
    grouped = {}
    for log in swap_logs:
        grouped.setdefault(log["transactionHash"], []).append((int(log["logIndex"], 16), VENUES[log["topics"][0]]))
    return grouped


def sold_in_span(sent, user, tx, start, end, cash):
    """(token, raw amount) this user moved into the executor inside their own operation.

    None when nothing left, which is what makes the deposit a payment rather than a
    sell. Cash leaving is skipped: that is the same money going the other way, not a
    sale of anything.
    """
    for moved in sent:
        if moved["transactionHash"] != tx:
            continue
        if not start < int(moved["logIndex"], 16) < end:
            continue
        if address_in_topic(moved, 1).lower() != user.lower():
            continue
        if moved["address"].lower() in cash:
            continue
        return moved["address"], int(moved["data"][2:] or "0", 16)
    return None


def read_intents(trade_logs, receipts, calls):
    """One entry per trade through the intent route, read out of its receipt.

    (log, wallet, operation, moved, venue, order): the seller or buyer, the seller's own
    `UserOperationEvent` (None for a buy, which is a relayer's call rather than an
    operation), the token that left or arrived as (token, raw) or None, the venues it ran
    through — inside the seller's own operation, since a bundle carries several users —
    and the intent id.
    `receipts` maps a transaction to its logs and `calls` a fill's transaction to its
    `eth_getTransactionByHash` result. A buyer that could not be read is None and is
    counted by the caller.
    """
    read = []
    for log in trade_logs:
        tx = log["transactionHash"]
        logs = receipts[tx]
        order = intent_ids(logs).get(topic(log, 1), UNREAD_ORDER)
        if topic(log) == INTENT_FILLED:
            buyer = intent_buyer(calls.get(tx))
            moved = delivered_to(logs, buyer) if buyer else None
            read.append((log, buyer, None, moved, venues(swaps_in(logs)), order))
            continue
        user = intent_sent(log)[0]
        operations = {int(op["logIndex"], 16): op for op in logs if topic(op) == USEROP}
        start, end = span(operations, int(log["logIndex"], 16))
        venue = venues(swaps_in(logs), start, end)
        read.append((log, user, operations.get(end), moved_from(logs, user, start, end), venue, order))
    return read


async def listen(deadline, watched, emit, counts, chain="rh", claimed=None):
    """Decode this transport on `chain` until `deadline`, calling `emit` with one Trade per trade.

    `watched` is a set of lowercased addresses, empty for every wallet. `Trade.key` is
    the transaction hash and the log index together, which is what identifies one trade
    across transports: the hash alone is not enough, because one handleOps bundle
    carries several users' trades. `claimed`, when given, says whether an order id is
    FOMO's, which reports a fill to a wallet with no delegation on this chain yet.
    """
    here = CHAINS[chain]
    websocket, url = env.require(f"{here.variable}_MAINNET_WSS", f"{here.variable}_MAINNET_HTTPS")
    # Blocks are a tenth of a second apart on one chain and twelve seconds on another, so
    # the silence that means a dead subscription is measured in blocks, with a floor.
    stall = max(STALL, 10 * here.block_seconds)

    meta, codes, orders = {}, {}, {}
    async with aiohttp.ClientSession(timeout=RPC_TIMEOUT) as http:
        checksum = Web3.to_checksum_address
        hashes = {}  # block number -> hash, from the head subscription

        async def rpc(method, params):
            async with http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                return (await r.json()).get("result")

        async def batched(calls):
            """One reply per call, in order, or None when a backend refused a block it lacks."""

            async def part(chunk):
                body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(chunk)]
                async with http.post(url, json=body) as r:
                    replies = {reply["id"]: reply for reply in await r.json()}
                return [replies.get(i, {}) for i in range(len(chunk))]

            parts = await asyncio.gather(*(part(calls[i : i + BATCH]) for i in range(0, len(calls), BATCH)))
            replies = [reply for p in parts for reply in p]
            if any(str((reply.get("error") or {}).get("message", "")).startswith(BEHIND) for reply in replies):
                return None
            return replies

        def wanted(address):
            return not watched or address.lower() in watched

        # A fill delivers to somebody's wallet, and a wallet is an EOA with or without a
        # delegation. FOMO's delegation is what keeps the other applications settling
        # through these same contracts out of the feed.
        def mine(app):
            return app == "fomo"

        # ---- one sweep, as four round trips and two decoding passes ----
        # Each returns None or False when a backend had not reached the range yet, which
        # means the same blocks are asked for again rather than reported short.

        async def block_hashes(blocks):
            """Fill in the hash of every block whose head the socket did not deliver.

            Heads that arrived while the socket was quiet have no hash yet, and a backend
            that does not have the block yet answers null for it.
            """
            unknown = [n for n in blocks if n not in hashes]
            if not unknown:
                return True
            replies = await batched([("eth_getBlockByNumber", [hex(n), False]) for n in unknown])
            if replies is None or any(result(r) is None for r in replies):
                return False
            for n, reply in zip(unknown, replies, strict=True):
                hashes[n] = reply["result"]["hash"]
            return True

        async def sweep_logs(blocks):
            """The six filters' logs for a range of blocks, asked for by block hash.

            By hash rather than by range: a load-balanced endpoint whose answering backend
            has not reached the end of a range answers with silently fewer logs, while one
            that lacks a block by hash refuses, and a refusal can be retried.
            """
            filters = (
                {"address": ENTRYPOINT, "topics": [USEROP]},
                {"address": DEPOSITORY, "topics": [DEPOSIT]},
                {"topics": [TRANSFER, EXECUTOR_TOPICS]},
                {"topics": [list(VENUES)]},
                {"address": DEPOSITORY, "topics": [NATIVE_DEPOSIT]},
                {"address": INTENT},
            )
            calls = [("eth_getLogs", [{"blockHash": hashes[n], **f}]) for n in blocks for f in filters]
            replies = await batched(calls)
            if replies is None:
                return None
            # One filter's logs are every len(filters)'th reply, in the order asked for.
            return [[log for reply in replies[k :: len(filters)] for log in result(reply)] for k in range(len(filters))]

        async def resolve_intents(trade_logs):
            """(receipt logs per transaction, fill calldata per transaction), or None to retry.

            A receipt the backend has not reached yet comes back null, which means the
            same blocks are asked for again rather than a trade reported without its token.
            """
            txs = sorted({log["transactionHash"] for log in trade_logs})
            fills = sorted({log["transactionHash"] for log in trade_logs if topic(log) == INTENT_FILLED})
            calls = [("eth_getTransactionReceipt", [tx]) for tx in txs]
            calls += [("eth_getTransactionByHash", [tx]) for tx in fills]
            replies = await batched(calls) if calls else []
            if replies is None or any(result(r) is None for r in replies[: len(txs)]):
                return None
            receipts = {tx: result(r)["logs"] for tx, r in zip(txs, replies[: len(txs)], strict=True)}
            return receipts, {tx: r.get("result") for tx, r in zip(fills, replies[len(txs) :], strict=True)}

        async def resolve_wallets(payments, fill_logs, at, others=()):
            """(app per wallet, transfers out of every depositor), or None to retry.

            A deposit is a sell if a token also left the depositor inside the same user
            operation, and a payment if nothing did. Fetching a receipt per bundle to find
            out is far too slow at 0.1 s blocks; one filtered query for transfers out of
            every depositor answers it instead. The fills' order ids come along for the
            ride, since they are one call each on the same trip.
            """
            depositors = {address_in_word(log, 0) for log, _ in payments}
            buyers = {address_in_topic(log, 2) for log in fill_logs} | set(others)
            unseen = sorted(w for w in buyers | depositors if w not in codes)
            fill_txs = sorted({log["transactionHash"] for log in fill_logs} - set(orders))
            calls = [("eth_getCode", [w, at]) for w in unseen]
            calls += [("eth_getTransactionByHash", [tx]) for tx in fill_txs]
            per_block = {}
            for log, _ in payments:
                per_block.setdefault(log["blockNumber"], set()).add(padded_topic(address_in_word(log, 0)))
            for number, senders in per_block.items():
                calls.append(
                    ("eth_getLogs", [{"blockHash": hashes[int(number, 16)], "topics": [TRANSFER, sorted(senders)]}])
                )

            replies = await batched(calls) if calls else []
            if replies is None:
                return None
            wallets_end = len(unseen)
            txs_end = wallets_end + len(fill_txs)
            # An address with no code yet may be delegated in a later block, so that
            # answer is never remembered; the rest is decided for this sweep.
            app = {}
            for wallet, reply in zip(unseen, replies[:wallets_end], strict=True):
                code = result(reply) or "0x"
                app[wallet] = app_name(code)
                if code != "0x":
                    codes[wallet] = app[wallet]
            app |= {w: codes[w] for w in buyers | depositors if w in codes}
            for tx, reply in zip(fill_txs, replies[wallets_end:txs_end], strict=True):
                orders[tx] = order_from_calldata(result(reply))
            sent = [log for reply in replies[txs_end:] for log in result(reply)]
            return app, sent

        async def resolve_tokens(payments, fill_logs, sent, at, others=()):
            """Cache the symbol and decimals of every token this sweep names. False to retry.

            A token that reverts on either is remembered as "?" and counted, never given a
            guessed scale: a wrong number that looks right is worse than a missing one.
            """
            addresses = {erc20_deposit(log)[0] for log, native in payments if not native}
            addresses |= {log["address"] for log in fill_logs} | {log["address"] for log in sent} | set(others)
            tokens = sorted(a for a in addresses if checksum(a) not in meta)
            calls = [
                (m, p)
                for a in tokens
                for m, p in (
                    ("eth_call", [{"to": checksum(a), "data": SYMBOL}, at]),
                    ("eth_call", [{"to": checksum(a), "data": DECIMALS}, at]),
                )
            ]
            replies = await batched(calls) if calls else []
            if replies is None:
                return False
            for n, a in enumerate(tokens):
                symbol, decimals = replies[2 * n].get("result"), replies[2 * n + 1].get("result")
                if not symbol or not decimals:
                    counts["lookups failed"] += 1
                    meta[checksum(a)] = ("?", None)
                else:
                    meta[checksum(a)] = (decode_string(bytes.fromhex(symbol[2:])), int(decimals, 16))
            return True

        def emit_deposits(payments, app, sent, ops, swaps, paid_for, sender_of):
            """A deposit is a sell when a token also left the same wallet, a payment when not.

            The deposit names the order it pays for either way, which is what joins it to
            its other chain. Both the sold token and the venue are scoped to the depositor's
            own operation rather than to the bundle, which carries several users.
            """
            for log, native in payments:
                user = address_in_word(log, 0)
                if not wanted(user) or not mine(app[user]):
                    continue
                if native:
                    # The native asset names no token, and its 18 decimals are a fact about
                    # the chain rather than a guess standing in for a failed lookup.
                    raw, order = native_deposit(log)
                    symbol, decimals = here.native, 18
                else:
                    address, raw, order = erc20_deposit(log)
                    symbol, decimals = meta[checksum(address)]
                paid = quantity(raw, decimals, symbol)
                tx = log["transactionHash"]
                start, end = span(ops.get(tx, []), int(log["logIndex"], 16))
                # An operation's own `UserOperationEvent` closes its span, so `end` is the
                # depositor's operation; a sweep has every log of its blocks, so a deposit with
                # no operation after it is not inside one, and FOMO deposits only from one.
                if sender_of.get((tx, end), "").lower() != user.lower():
                    counts["deposits outside the depositor's own operation"] += 1
                    continue
                # A paymaster on the depositor's operation makes this another application's.
                if (tx, end) in paid_for:
                    counts["deposits sponsored by a paymaster, so not FOMO's"] += 1
                    continue

                went_out = sold_in_span(sent, user, tx, start, end, here.cash)
                if went_out is None:
                    counts["PAY"] += 1
                    emit(Trade("PAY", paid, UNNAMED, user, app[user], "", where_of(log), trade_key(log), order))
                    continue
                out_address, out_raw = went_out
                symbol_out, decimals_out = meta[checksum(out_address)]
                counts["SELL"] += 1
                emit(
                    Trade(
                        "SELL",
                        quantity(out_raw, decimals_out, symbol_out),
                        paid,
                        user,
                        app[user],
                        venues(swaps.get(tx, []), start, end),
                        where_of(log),
                        trade_key(log),
                        order,
                    )
                )

        def emit_fills(fill_logs, app, swaps):
            """A token arriving from Relay's executor into a FOMO wallet: a buy.

            A fill is the solver's own transaction rather than a bundle, so every swap in
            it belongs to this delivery and there is no span to scope by.
            """
            for log in fill_logs:
                buyer = address_in_topic(log, 2)
                tx = log["transactionHash"]
                if buyer == BURN or not wanted(buyer):
                    continue
                # A claimed order makes a plain address the buyer, never a router or pool
                # the same fill moved tokens through on the way.
                if not mine(app[buyer]):
                    if app[buyer] != "eoa" or not claimed:
                        continue
                    if not claimed(orders.get(tx, "")):
                        # Another application's user, or a FOMO payment the Solana leg has
                        # not decoded yet: either way not reported, so counted rather than lost.
                        counts["fills to a plain address no FOMO payment claimed"] += 1
                        continue
                symbol, decimals = meta[checksum(log["address"])]
                counts["BUY"] += 1
                emit(
                    Trade(
                        kind="BUY",
                        got=quantity(int(log["data"][2:] or "0", 16), decimals, symbol),
                        who=buyer,
                        app=app[buyer],
                        note=venues(swaps.get(tx, [])),
                        where=where_of(log),
                        key=trade_key(log),
                        order_id=orders.get(tx, ""),
                    )
                )

        def emit_intents(intents, app):
            """A trade through the intent route: a buy delivered, or the native asset sent away.

            The same rules as Relay's: a buyer carries FOMO's delegation or is a plain
            address whose intent id the caller claims; a seller carries it and no paymaster
            paid for the operation. A send with no token leaving first is a payment.
            """
            for log, wallet, operation, moved, venue, order in intents:
                if wallet is None:
                    counts["intent fills whose buyer could not be read"] += 1
                    continue
                if not wanted(wallet):
                    continue
                token = None
                if moved:
                    symbol, decimals = meta[checksum(moved[0])]
                    token = quantity(moved[1], decimals, symbol)
                if topic(log) == INTENT_FILLED:
                    if not (mine(app[wallet]) or (app[wallet] == "eoa" and claimed and claimed(order))):
                        # A plain address is a wallet new to this chain or another application's
                        # user; either way not reported, so counted apart from a known other one.
                        if app[wallet] == "eoa":
                            counts["intent fills to a plain address no FOMO payment claimed"] += 1
                        else:
                            counts["intent fills to another application's wallet"] += 1
                        continue
                    if token is None:
                        counts["intent fills whose token could not be read"] += 1
                    counts["BUY"] += 1
                    emit(
                        Trade(
                            kind="BUY",
                            got=token or "?",
                            who=wallet,
                            app=app[wallet],
                            note=venue,
                            where=where_of(log),
                            key=trade_key(log),
                            order_id=order,
                        )
                    )
                    continue
                if not mine(app[wallet]) or operation is None or sponsored(operation):
                    counts["intent sends that are not FOMO's"] += 1
                    continue
                paid = quantity(intent_sent(log)[1], 18, here.native)
                if token is None:
                    counts["PAY"] += 1
                    emit(Trade("PAY", paid, UNNAMED, wallet, app[wallet], "", where_of(log), trade_key(log), order))
                    continue
                counts["SELL"] += 1
                emit(Trade("SELL", token, paid, wallet, app[wallet], venue, where_of(log), trade_key(log), order))

        async def handle(start_block, end_block):
            """One sweep. False when a backend was behind and the same range must be retried."""
            blocks = list(range(start_block, end_block + 1))
            at = hex(end_block)
            if not await block_hashes(blocks):
                return False
            logs = await sweep_logs(blocks)
            if logs is None:
                return False
            op_logs, deposit_logs, fill_logs, swap_logs, native_logs, intent_logs = logs
            counts["user operations"] += len(op_logs)
            # The id event opens every trade and is read back out of the receipt; what is
            # neither a buy nor a sell is the route moving its own money.
            trade_logs = [log for log in intent_logs if topic(log) in (INTENT_FILLED, INTENT_SENT)]
            fetched = await resolve_intents(trade_logs)
            if fetched is None:
                return False
            intents = read_intents(trade_logs, *fetched)

            # A payment in ether and a payment in a token are the same trade seen from the
            # paying side, so they go through one list; the flag says which shape to read
            # it with, because the ether one names no token and puts its fields earlier.
            payments = [(log, False) for log in deposit_logs] + [(log, True) for log in native_logs]
            ops, swaps = operation_boundaries(op_logs), swaps_by_transaction(swap_logs)
            paid_for = {(log["transactionHash"], int(log["logIndex"], 16)) for log in op_logs if sponsored(log)}
            sender_of = {
                (log["transactionHash"], int(log["logIndex"], 16)): "0x" + log["topics"][2][-40:] for log in op_logs
            }

            wallets = {wallet for _, wallet, _, _, _, _ in intents if wallet}
            resolved = await resolve_wallets(payments, fill_logs, at, wallets)
            if resolved is None:
                return False
            app, sent = resolved
            tokens = {moved[0] for _, _, _, moved, _, _ in intents if moved}
            if not await resolve_tokens(payments, fill_logs, sent, at, tokens):
                return False

            emit_deposits(payments, app, sent, ops, swaps, paid_for, sender_of)
            emit_fills(fill_logs, app, swaps)
            emit_intents(intents, app)
            # Counted once the sweep has succeeded, since a retried one sees them again.
            counts["intent-route events that are not trades"] += sum(
                1 for log in intent_logs if topic(log) not in INTENT_TRADE_EVENTS
            )
            return True

        async with websockets.connect(websocket, open_timeout=OPEN_TIMEOUT, close_timeout=1) as socket:
            await socket.send(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]})
            )
            latest = last = int(await rpc("eth_blockNumber", []), 16)

            # Heads arrive one per block and queue in order, so reading them inside the
            # work loop falls a block further behind on every pass. A reader task drains
            # the queue and keeps only the newest; the loop then sweeps whole ranges.
            failure = None

            async def reader():
                nonlocal latest, failure
                try:
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("method") == "eth_subscription":
                            head = message["params"]["result"]
                            hashes[int(head["number"], 16)] = head["hash"]
                            latest = max(latest, int(head["number"], 16))
                except asyncio.CancelledError:
                    pass
                except Exception as err:
                    # A dead socket ends the run. Carrying on by polling would answer a
                    # different question than the one this script exists to answer, and
                    # would do it without the reader ever saying the subscription died.
                    failure = err

            pump = asyncio.create_task(reader())
            idle = 0.0
            while time.time() < deadline:
                if failure is not None:
                    raise failure
                if latest > last:
                    # Pin the target before the sweep. Reading `latest` again afterwards
                    # would skip every block that arrived while the sweep was running.
                    target = latest
                    if target - last > MAX_SWEEP_BLOCKS:
                        print(f"[behind by {target - last} blocks - skipping to the last {MAX_SWEEP_BLOCKS}]")
                        last = target - MAX_SWEEP_BLOCKS
                    try:
                        done = await handle(last + 1, target)
                    except Exception as err:
                        print(f"[sweep {last + 1}-{target} failed: {type(err).__name__}: {err} - retrying]")
                        await asyncio.sleep(0.2)
                        continue
                    if not done:  # a backend had not reached the target yet; ask again
                        counts["sweeps retried behind a lagging backend"] += 1
                        await asyncio.sleep(0.05)
                        continue
                    counts["blocks swept"] += target - last
                    last = target
                    idle = 0.0
                    for n in [n for n in hashes if n < last - 1000]:
                        del hashes[n]
                    if len(orders) > 20000:
                        orders.clear()
                else:
                    await asyncio.sleep(0.05)
                    idle += 0.05
                    # Nothing is fetched to cover for a subscription that has gone quiet.
                    # This long without a head is a subscription that is not delivering,
                    # and that is worth stopping for.
                    if idle > stall:
                        raise RuntimeError(f"no new head for {stall:.0f} s — the subscription is not delivering")
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)


if __name__ == "__main__":
    chain = cli.evm_chain()
    cli.run(f"{chain}-blocks", partial(listen, chain=chain))
