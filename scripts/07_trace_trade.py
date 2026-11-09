"""Follow one FOMO trade across Solana and an EVM chain, using nothing but the two chains.

A cross-chain trade is two transactions that share nothing an explorer will join for you.
The link is an id written on both sides, so the join needs no third party. FOMO crosses by
two routes and each writes its own: Relay a 32-byte order id, the intent route an intent id.
Relay's REST API can also resolve an order id, and this script used to depend on it, but
that API is throttled, retires on 2026-11-24, and its replacement wants a key.

The two directions are not shaped alike, and the id sits somewhere different in each:

  buy    Solana pays, the EVM chain fills. The Relay deposit instruction carries the id
         after the amount; the solver's fill ends its router calldata with it.
  sell   The EVM chain executes, Solana pays out. The depository log carries the id in
         its last word; the solver's transfer carries it in an spl-memo. Cash moving to
         Solana is the same shape with no swap before the deposit, so the deposit is
         printed as one and not called a sale.

A sell's payout is signed by a Relay solver and not by FOMO's co-signer, so no listener in
this repository sees it — the co-signer subscription never receives that transaction. This
script finds it by reading the solver's own recent transactions.

The intent route has the same two directions with the id in different places:

  buy    Solana pays the intent solver with `{"intentId":"<id>"}` in a memo; on the EVM chain
         a relayer's fill emits the id event beside the delivery.
  sell   The wallet's own operation sends the native asset into the intent contract, which
         emits the id event; the intent solver pays out on Solana with the bare id in a memo.

Both of its legs are found by their id alone: the contract's id events on the EVM chain, and
the intent solver's recent transactions on Solana, which hold its payments and its payouts.

Takes the EVM chain to search, one of shared/evm.py's `CHAINS`, then any one of a
transaction on it (0x, 66 chars — a solver's fill, a deposit in either asset shape, or an
intent-route fill or send), a Solana signature (base58, a Relay deposit or settlement, or an
intent-route payment or payout), an order id (0x, 66 chars, from either side) or an intent id
(7, 7 and 3 hex characters joined by hyphens), and searches for the counterpart in both
directions. Searching is bounded by the minutes argument, because without an index the only
way to find a transaction by its contents is to read a window of them.

That window is read as ranges rather than the per-block `blockHash` queries the listeners
use: this is a scan of thousands of blocks, and one request per block would be thousands of
round trips. A load-balanced endpoint can answer a range short when its end is past the
answering backend's head (docs/04-listening.md, Transports), so a miss re-reads the head and
sweeps the tail again before reporting nothing.

Reference: docs/04-listening.md (Joining the two legs), docs/05-crossing.md (Following one order).

A `handleOps` bundle carries several users, so a transaction holding more than one deposit
names a transaction and not a trade: the first is followed and the count is printed.

    uv run scripts/07_trace_trade.py <chain> <tx hash | signature | order id | intent id> [minutes]     default 5
"""

import sys
import time

import base58
import requests
from shared import cli, env
from shared.evm import (
    CHAINS,
    DECIMALS,
    DELEGATION,
    DEPOSIT,
    DEPOSITORY,
    EXECUTOR_TOPICS,
    INTENT,
    INTENT_FILLED,
    INTENT_ID,
    INTENT_SENT,
    NATIVE_DEPOSIT,
    ROUTERS,
    SYMBOL,
    TRANSFER,
    USEROP,
    decode_string,
    delivered_to,
    intent_buyer,
    intent_ids,
    intent_sent,
    moved_from,
    span,
    topic,
    word,
)
from shared.solana import (
    CO_SIGNER,
    DEPOSIT_DISCRIMINATOR,
    INTENT_ID_SHAPE,
    INTENT_SOLVER,
    KNOWN,
    MEMO_PROGRAMS,
    RELAY_DEPOSIT,
    SETTLEMENT_SOLVER,
    TX_VERSION,
    mint_name,
    payment_id,
    payout_id,
)
from web3 import Web3

here = CHAINS[cli.evm_chain()]
EVM = env.endpoint(f"{here.variable}_MAINNET_HTTPS")
SOLANA = env.endpoint("SOLANA_MAINNET_HTTPS")
w3 = Web3(Web3.HTTPProvider(EVM))

BLOCK_SECONDS = here.block_seconds
SWEEP = 300  # blocks per eth_getLogs range
# A deposit comes in two shapes and they are the same trade. The ERC-20 one names the
# token and puts the order id at word 3; the ether one names no token and puts it one
# word earlier. Reading only the first loses every buy that was paid for in ether.
ORDER_WORD = {DEPOSIT: 3, NATIVE_DEPOSIT: 2}

if len(sys.argv) < 2:
    sys.exit(__doc__)
target = sys.argv[1]
minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 5
# Two of the busiest addresses on Solana are paged here. `blockTime` can come back null,
# and the cutoff is the only thing that ends the walk — without a cap, one null entry
# turns a five-minute lookback into the whole history of the address, on a metered node.
PAGES = 20


def batch(url, calls):
    """One JSON-RPC batch. `calls` are (method, params); replies come back in order."""
    out = []
    for start in range(0, len(calls), 100):
        chunk = calls[start : start + 100]
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(chunk)]
        replies = requests.post(url, json=body, timeout=120).json()
        by_id = {r["id"]: r.get("result") for r in replies}
        out += [by_id.get(i) for i in range(len(chunk))]
    return out


def token(address):
    try:
        raw = batch(
            EVM,
            [
                ("eth_call", [{"to": address, "data": SYMBOL}, "latest"]),
                ("eth_call", [{"to": address, "data": DECIMALS}, "latest"]),
            ],
        )
        return decode_string(bytes.fromhex(raw[0][2:])), int(raw[1], 16)
    except Exception:
        # No invented scale: a guessed 18 prints a wrong number that looks right.
        return "?", None


def data_of(log):
    return log["data"].hex().removeprefix("0x")


def order_from_evm(tx_hash):
    """(order id, what the transaction is, how many deposits it holds) for one hash."""
    transaction = w3.eth.get_transaction(tx_hash)
    calldata = transaction["input"].hex().removeprefix("0x")
    if (transaction["to"] or "").lower() in ROUTERS and len(calldata) >= 64:
        return "0x" + calldata[-64:], "a solver's fill", 1
    ids = []
    for log in w3.eth.get_transaction_receipt(tx_hash)["logs"]:
        if log["address"].lower() != DEPOSITORY.lower():
            continue
        position = ORDER_WORD.get(topic(log))
        if position is not None:
            ids.append("0x" + word(data_of(log), position))
    if not ids:
        return "", "", 0
    # A bundle carries several users, so a hash names a transaction and not a trade.
    return ids[0], "a deposit on this chain", len(ids)


def order_from_solana(signature):
    """The order id a Solana Relay deposit carries, straight from the instruction."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION}],
    }
    tx = requests.post(SOLANA, json=body, timeout=60).json().get("result")
    if not tx:
        return "", None
    for instruction in tx["transaction"]["message"]["instructions"]:
        if instruction.get("programId") != RELAY_DEPOSIT or "data" not in instruction:
            continue
        raw = base58.b58decode(instruction["data"])
        if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR:
            return "0x" + raw[16:48].hex(), tx
    return "", tx


def recent_signatures(address, noun):
    """One address's recent successful signatures, newest first, and how far back it got.

    Without an index the only way to find a transaction by its contents is to read a
    window of them, and `PAGES` caps how far back that window can reach. Whether the
    walk stopped at the cutoff or ran out of pages is the difference between "not in the
    last few minutes" and "not in the pages that fit", and only the first is answered by
    widening the window, so which one happened is printed above the search it describes.
    """
    seen, before, reached = [], None, False
    cutoff = time.time() - minutes * 60
    for _ in range(PAGES):
        page = (
            requests.post(
                SOLANA,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [address, {"limit": 1000, "before": before}],
                },
                timeout=90,
            )
            .json()
            .get("result")
            or []
        )
        if not page or page[-1]["signature"] == before:
            reached = True  # the address has no older history left to read
            break
        seen += [s for s in page if not s["err"]]
        before = page[-1]["signature"]
        if page[-1].get("blockTime") and page[-1]["blockTime"] < cutoff:
            reached = True
            break
    if reached:
        print(f"  searching {len(seen)} Solana {noun} from the last {minutes} min")
    else:
        print(f"  searching {len(seen)} Solana {noun}, as far back as {PAGES} pages reach")
        print(f"    {noun.capitalize()} on this address are dense enough that this is short of {minutes} min.")
    return seen


def find_solana_leg(order_id):
    """Search recent Relay deposits on Solana for the payment carrying this order id."""
    seen = recent_signatures(RELAY_DEPOSIT, "deposits")
    options = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION}
    for start in range(0, len(seen), 100):
        chunk = seen[start : start + 100]
        fetched = batch(SOLANA, [("getTransaction", [s["signature"], options]) for s in chunk])
        for signature, tx in zip(chunk, fetched, strict=True):
            if not tx:
                continue
            for instruction in tx["transaction"]["message"]["instructions"]:
                if instruction.get("programId") != RELAY_DEPOSIT or "data" not in instruction:
                    continue
                raw = base58.b58decode(instruction["data"])
                if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR and "0x" + raw[16:48].hex() == order_id:
                    return signature["signature"], tx, int.from_bytes(raw[8:16], "little")
    return "", None, 0


def memo_ids(tx):
    """Every 32-byte order id a transaction carries in a memo."""
    found = []
    for instruction in tx["transaction"]["message"]["instructions"]:
        if instruction.get("programId") not in MEMO_PROGRAMS:
            continue
        note = instruction.get("parsed")
        if isinstance(note, str) and note.startswith("0x") and len(note) == 66:
            found.append(note.lower())
    return found


def find_solana_settlement(order_id):
    """Search the solver's recent payouts for the one whose memo is this order id."""
    seen = recent_signatures(SETTLEMENT_SOLVER, "payouts")
    options = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION}
    for start in range(0, len(seen), 100):
        chunk = seen[start : start + 100]
        fetched = batch(SOLANA, [("getTransaction", [s["signature"], options]) for s in chunk])
        for signature, tx in zip(chunk, fetched, strict=True):
            if tx and order_id.lower() in memo_ids(tx):
                return signature["signature"], tx
    return "", None


def find_evm_deposit(order_id):
    """Search recent depository logs for the deposit carrying this order id."""
    head = w3.eth.block_number
    low = head - int(minutes * 60 / BLOCK_SECONDS)
    print(f"  searching {here.name} deposits over blocks {low}-{head}")
    for start in range(head, low, -SWEEP):
        criteria = {
            "fromBlock": max(start - SWEEP + 1, low),
            "toBlock": start,
            "address": Web3.to_checksum_address(DEPOSITORY),
            "topics": [[DEPOSIT, NATIVE_DEPOSIT]],
        }
        for log in w3.eth.get_logs(criteria):
            data = log["data"].hex().removeprefix("0x")
            if "0x" + word(data, ORDER_WORD[topic(log)]) == order_id:
                h = log["transactionHash"].hex()
                return h if h.startswith("0x") else "0x" + h
    return ""


def sweep_fills(order_id, low, high):
    """The fill in blocks low..high whose router call ends with this order id, or ""."""
    hashes = []
    for start in range(low, high + 1, SWEEP):
        criteria = {"fromBlock": start, "toBlock": min(start + SWEEP - 1, high), "topics": [TRANSFER, EXECUTOR_TOPICS]}
        for log in w3.eth.get_logs(criteria):
            h = log["transactionHash"].hex()
            hashes.append(h if h.startswith("0x") else "0x" + h)
    hashes = list(dict.fromkeys(hashes))
    print(f"  searching {len(hashes)} {here.name} fills over blocks {low}-{high}")
    for start in range(0, len(hashes), 100):
        chunk = hashes[start : start + 100]
        for h, info in zip(chunk, batch(EVM, [("eth_getTransactionByHash", [t]) for t in chunk]), strict=True):
            if not info or (info.get("to") or "").lower() not in ROUTERS:
                continue
            calldata = info["input"][2:]
            if len(calldata) >= 64 and "0x" + calldata[-64:] == order_id:
                return h
    return ""


def find_evm_leg(order_id):
    """Search recent solver fills for the one whose call ends with this order id.

    A backend that had not reached the end of the range answers it short and says
    nothing, so a miss is re-read against a fresh head before it is believed.
    """
    head = w3.eth.block_number
    low = head - int(minutes * 60 / BLOCK_SECONDS)
    found = sweep_fills(order_id, low, head)
    if found:
        return found
    again = w3.eth.block_number
    if again > head:
        return sweep_fills(order_id, max(low, head - SWEEP), again)
    return ""


def show_solana(signature, tx, amount):
    print(f"\n  paid on Solana   {signature}")
    if not tx:
        return
    signers = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"] if k.get("signer")]
    user = next((s for s in signers if s != signers[0]), signers[0]) if len(signers) > 1 else signers[0]
    mint = ""
    for balance in tx["meta"].get("preTokenBalances", []):
        if balance.get("owner") == user:
            mint = balance["mint"]
            break
    balances = tx["meta"].get("preTokenBalances", [])
    # The mint's own decimals, or none at all. Six is the common case and guessing
    # it prints a wrong number for every mint that is not.
    decimals = next((b["uiTokenAmount"]["decimals"] for b in balances if b["mint"] == mint), None)
    named = mint_name(mint) if mint else "?"
    shown = f"{amount / 10**decimals:.6f} {named}" if decimals is not None else f"? {amount} raw, {named}"
    print(f"    payer          {user}")
    print(f"    amount         {shown}")


def show_evm(tx_hash):
    print(f"\n  filled on {here.name}   {tx_hash}")
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    for log in receipt["logs"]:
        if topic(log) != TRANSFER:
            continue
        if topic(log, 1) not in EXECUTOR_TOPICS:
            continue
        recipient = Web3.to_checksum_address("0x" + topic(log, 2)[-40:])
        # The executor also moves tokens through pools on its way; only a delegated
        # wallet is somebody's delivery.
        code = "0x" + w3.eth.get_code(recipient).hex().removeprefix("0x")
        if not code.startswith(DELEGATION):
            continue
        symbol, decimals = token(Web3.to_checksum_address(log["address"]))
        raw = int(log["data"].hex().removeprefix("0x") or "0", 16)
        shown = f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw} raw, {symbol}"
        print(f"    delivered      {shown}  ({log['address']})")
        print(f"    to             {recipient}")
    print(f"    block          {receipt['blockNumber']}")


def show_settlement(signature, tx):
    print(f"\n  paid out on Solana   {signature}")
    solver = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]

    def held(side):
        return {(b.get("owner"), b["mint"]): float(b["uiTokenAmount"]["uiAmountString"]) for b in tx["meta"][side]}

    before, after = held("preTokenBalances"), held("postTokenBalances")
    gained = ((after[k] - before.get(k, 0), k) for k in after)
    credited = sorted(((d, k) for d, k in gained if d > 0), reverse=True)
    if credited:
        amount, (owner, mint) = credited[0]
        print(f"    delivered      {amount:.6f} {mint_name(mint)}")
        print(f"    to             {owner}")
    print(f"    solver         {solver}")
    print(f"    slot           {tx['slot']}")


def show_deposit(tx_hash, order_id):
    """One handleOps bundle carries several users, so only the matching deposit is this trade."""
    print(f"\n  deposited on {here.name}   {tx_hash}")
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    for log in receipt["logs"]:
        if log["address"].lower() != DEPOSITORY.lower() or topic(log) not in ORDER_WORD:
            continue
        data = log["data"].hex().removeprefix("0x")
        if "0x" + word(data, ORDER_WORD[topic(log)]) != order_id:
            continue
        depositor = Web3.to_checksum_address("0x" + word(data, 0)[24:])
        if topic(log) == DEPOSIT:
            symbol, decimals = token(Web3.to_checksum_address("0x" + word(data, 1)[24:]))
            raw = int(word(data, 2), 16)
        else:
            symbol, decimals, raw = here.native, 18, int(word(data, 1), 16)
        shown = f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw} raw, {symbol}"
        print(f"    amount         {shown}")
        print(f"    from           {depositor}")
    print(f"    block          {receipt['blockNumber']}")


# ------------------------------------------------------------------- the intent route
USDC = next(mint for mint, name in KNOWN.items() if name == "USDC")
# Read over raw JSON-RPC rather than web3, so a log reaches shared/evm.py's helpers in the
# shape the listeners hand them: hex strings, not bytes.


def evm_rpc(method, params):
    """One JSON-RPC call to the EVM endpoint; None on an error."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return requests.post(EVM, json=body, timeout=60).json().get("result")


def fetch_solana(signature):
    options = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION}
    body = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [signature, options]}
    return requests.post(SOLANA, json=body, timeout=60).json().get("result")


def intent_memos(tx):
    """Every intent id a Solana transaction carries in a memo, as a payment or as a payout."""
    found = []
    for instruction in tx["transaction"]["message"]["instructions"]:
        note = instruction.get("parsed")
        if instruction.get("programId") in MEMO_PROGRAMS and isinstance(note, str):
            found.append(payment_id(note) or (note if INTENT_ID_SHAPE.fullmatch(payout_id(note)) else ""))
    return [f for f in found if f]


def intent_of(value):
    """(intent id, EVM transaction, Solana signature, Solana transaction) behind `value`.

    The intent id is "" when `value` is no part of an intent-route trade, which leaves it to
    the Relay path below. A Relay deposit or fill reads as nothing here.
    """
    if INTENT_ID_SHAPE.fullmatch(value):
        return value, "", "", None
    if value.startswith("0x") and len(value) == 66:
        receipt = evm_rpc("eth_getTransactionReceipt", [value])
        logs = (receipt or {}).get("logs") or []
        ids = intent_ids(logs)
        for log in logs:
            if log["address"].lower() == INTENT and topic(log) in (INTENT_FILLED, INTENT_SENT):
                return ids.get(topic(log, 1), ""), value, "", None
        return "", "", "", None
    if len(value) > 60:
        tx = fetch_solana(value)
        found = intent_memos(tx) if tx else []
        if found:
            return found[0], "", value, tx
    return "", "", "", None


def id_in(log):
    """The intent id an id event states, in ASCII in one word."""
    return bytes.fromhex(log["data"][2:66]).rstrip(b"\x00").decode(errors="replace")


def sweep_intents(intent, low, high):
    """The transaction in blocks low..high whose id event names `intent`, or ""."""
    for start in range(high, low, -SWEEP):
        criteria = {"fromBlock": hex(max(start - SWEEP + 1, low)), "toBlock": hex(start)}
        for log in evm_rpc("eth_getLogs", [{**criteria, "address": INTENT, "topics": [INTENT_ID]}]) or []:
            if id_in(log) == intent:
                return log["transactionHash"]
    return ""


def find_intent_evm(intent):
    """The intent-route trade on this chain carrying `intent`, re-read once against a fresh head."""
    head = w3.eth.block_number
    low = head - int(minutes * 60 / BLOCK_SECONDS)
    print(f"  searching {here.name} intent-route trades over blocks {low}-{head}")
    found = sweep_intents(intent, low, head)
    again = w3.eth.block_number
    if not found and again > head:
        found = sweep_intents(intent, max(low, head - SWEEP), again)
    return found


def find_intent_solana(intent):
    """The intent solver's payment or payout whose memo carries `intent`.

    A payout is signed by the solver and lists under its address. A payment is a transfer
    into the solver's USDC account, which names that account and not the solver, so it
    lists only under the account: both are read.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [INTENT_SOLVER, {"mint": USDC}, {"encoding": "jsonParsed"}],
    }
    accounts = [a["pubkey"] for a in requests.post(SOLANA, json=body, timeout=60).json()["result"]["value"]]
    seen = recent_signatures(INTENT_SOLVER, "intent-solver payouts")
    for account in accounts:
        seen += recent_signatures(account, "payments into the intent solver")
    options = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION}
    for start in range(0, len(seen), 100):
        chunk = seen[start : start + 100]
        fetched = batch(SOLANA, [("getTransaction", [s["signature"], options]) for s in chunk])
        for signature, tx in zip(chunk, fetched, strict=True):
            if tx and intent in intent_memos(tx):
                return signature["signature"], tx
    return "", None


def shown(raw, address):
    symbol, decimals = token(Web3.to_checksum_address(address))
    return f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw} raw, {symbol}"


def show_intent_evm(tx_hash, intent):
    """The EVM leg: a relayer's fill delivering a token, or a wallet's send into the contract."""
    logs = evm_rpc("eth_getTransactionReceipt", [tx_hash])["logs"]
    key = next((k for k, v in intent_ids(logs).items() if v == intent), "")
    for log in logs:
        if log["address"].lower() != INTENT or topic(log, 1) != key:
            continue
        if topic(log) == INTENT_FILLED:
            buyer = intent_buyer(evm_rpc("eth_getTransactionByHash", [tx_hash]))
            print(f"\n  filled on {here.name}   {tx_hash}")
            landed = delivered_to(logs, buyer) if buyer else None
            if landed:
                print(f"    delivered      {shown(landed[1], landed[0])}  ({landed[0]})")
            print(f"    to             {buyer or '?'}")
        elif topic(log) == INTENT_SENT:
            user, raw = intent_sent(log)
            operations = [int(op["logIndex"], 16) for op in logs if topic(op) == USEROP]
            start, end = span(operations, int(log["logIndex"], 16))
            sold = moved_from(logs, user, start, end)
            recipient = base58.b58encode(bytes.fromhex(word(log["data"][2:], 1))).decode()
            print(f"\n  sent on {here.name}   {tx_hash}")
            if sold:
                print(f"    sold           {shown(sold[1], sold[0])}  ({sold[0]})")
            print(f"    sent           {raw / 1e18:.6f} {here.native}, to {recipient} on Solana")
            print(f"    from           {user}")
    print(f"    block          {int(evm_rpc('eth_getTransactionByHash', [tx_hash])['blockNumber'], 16)}")


def show_intent_payment(signature, tx):
    """The Solana leg of a buy: the user's USDC into the intent solver."""
    print(f"\n  paid on Solana   {signature}")
    signers = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"] if k.get("signer")]

    def held(side):
        return {(b.get("owner"), b["mint"]): float(b["uiTokenAmount"]["uiAmountString"]) for b in tx["meta"][side]}

    before, after = held("preTokenBalances"), held("postTokenBalances")
    to_solver = [(after[k] - before.get(k, 0), k[1]) for k in after if k[0] == INTENT_SOLVER]
    print(f"    payer          {next((s for s in signers if s != CO_SIGNER), '?')}")
    if to_solver:
        amount, mint = max(to_solver)
        print(f"    amount         {amount:.6f} {mint_name(mint)}")
    print(f"    slot           {tx['slot']}")


def trace_intent(intent, evm_tx, signature, tx):
    """Both legs of one intent-route trade, found by the id each of them states."""
    print(f"  intent id {intent}")
    if not evm_tx:
        evm_tx = find_intent_evm(intent)
    if not signature:
        signature, tx = find_intent_solana(intent)
    # The Solana leg says which way the trade ran: the solver signs a payout, never a payment.
    paid_out = bool(tx) and tx["transaction"]["message"]["accountKeys"][0]["pubkey"] == INTENT_SOLVER
    if tx and not paid_out:
        show_intent_payment(signature, tx)
    elif not tx:
        print(f"\n  on Solana   not found in the last {minutes} min — widen the window")
    if evm_tx:
        show_intent_evm(evm_tx, intent)
    else:
        print(f"\n  on {here.name}   not found in the last {minutes} min — another chain, or widen the window")
    if paid_out:
        show_settlement(signature, tx)


intent, intent_evm, intent_signature, intent_tx = intent_of(target)
if intent:
    print(f"intent-route trade, from {target}")
    trace_intent(intent, intent_evm, intent_signature, intent_tx)
    sys.exit()

order_id, source = "", ""
solana_signature, solana_tx, solana_amount = "", None, 0
evm_tx = ""
settlement_signature, settlement_tx, evm_deposit = "", None, ""
direction = ""

if target.startswith("0x") and len(target) == 66:
    deposits = 0
    try:
        order_id, source, deposits = order_from_evm(target)
    except Exception:
        order_id = ""
    if order_id and source == "a deposit on this chain":
        # Proceeds going into Relay: a sell, whose other half is a payout on Solana.
        evm_deposit, direction = target, "sell"
        print(f"{here.name} transaction {target}\n  it is {source}")
        if deposits > 1:
            print(f"  {deposits} deposits in this transaction, following the first")
    elif order_id:
        evm_tx, direction = target, "buy"
        print(f"{here.name} transaction {target}\n  it is {source}")
    else:
        order_id, direction = target, ""
        print(f"order id {target}")
elif len(target) > 60:
    print(f"Solana signature {target}")
    order_id, solana_tx = order_from_solana(target)
    if order_id:
        solana_signature, direction = target, "buy"
        print("  it is a Relay deposit")
        for instruction in solana_tx["transaction"]["message"]["instructions"]:
            if instruction.get("programId") == RELAY_DEPOSIT and "data" in instruction:
                raw = base58.b58decode(instruction["data"])
                if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR:
                    solana_amount = int.from_bytes(raw[8:16], "little")
    elif solana_tx and memo_ids(solana_tx):
        # No Relay deposit, but a memo carrying an order id: this is the payout half.
        order_id = memo_ids(solana_tx)[0]
        settlement_signature, settlement_tx, direction = target, solana_tx, "sell"
        print("  it is a solver's payout")
    else:
        sys.exit("  no Relay deposit and no order id memo — this is not a leg of a cross-chain trade")
else:
    sys.exit(f"give a {here.name} transaction, a Solana signature, an order id or an intent id")

if order_id != target:
    print(f"  order id {order_id}")

# A bare order id says nothing about which way the trade ran, so both shapes are tried.
if direction in ("", "buy"):
    if not solana_signature:
        solana_signature, solana_tx, solana_amount = find_solana_leg(order_id)
    if not evm_tx:
        evm_tx = find_evm_leg(order_id)
    if solana_signature or evm_tx:
        direction = "buy"

if direction in ("", "sell"):
    if not evm_deposit:
        evm_deposit = find_evm_deposit(order_id)
    if not settlement_signature:
        settlement_signature, settlement_tx = find_solana_settlement(order_id)
    if evm_deposit or settlement_signature:
        direction = "sell"

if direction == "sell":
    if evm_deposit:
        show_deposit(evm_deposit, order_id)
    else:
        print(f"\n  deposited on {here.name}   not found in the last {minutes} min — widen the window")
    if settlement_signature:
        show_settlement(settlement_signature, settlement_tx)
    else:
        print(f"\n  paid out on Solana   not found in the last {minutes} min — not settled yet, or widen the window")
elif direction == "buy":
    if solana_signature:
        show_solana(solana_signature, solana_tx, solana_amount)
    else:
        print(f"\n  paid on Solana   not found in the last {minutes} min — widen the window")
    if evm_tx:
        show_evm(evm_tx)
    else:
        print(f"\n  filled on {here.name}   not found in the last {minutes} min — not filled yet")
else:
    print(f"\n  neither leg found in the last {minutes} min — widen the window")
