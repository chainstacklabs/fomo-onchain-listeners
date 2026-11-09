"""What FOMO's EVM chains are made of, and the decoding that needs no network.

FOMO runs the same way on every EVM chain it trades on: an ERC-4337 operation from a wallet
carrying one EIP-7702 delegation, paid into and filled out of Relay's contracts, which Relay
deploys at the same addresses everywhere. So the contracts, the topics and the ABI slicing
that reads them are written once, here, and what genuinely differs between chains — the
endpoint, the native asset, the cash a trade passes through — is one row of `CHAINS`.

Everything here is either a fact about a chain or a pure function over bytes, so it can be
read in one sitting and changed in one place. Nothing here makes a request: a listener's own
plumbing stays in the listener, because how 01 and 02 fetch differs and hiding that behind a
shared layer would make both harder to follow, not easier.

`maintenance/registry.json` is the verified record of these values and
`maintenance/verify_registry.py` checks it against the live chain;
`maintenance/probe_chain.py` is how a chain earns its row. This module is the copy the
scripts import; when the two disagree, the chain decides.

Reference: docs/07-addresses.md, docs/04-listening.md (Event surface).
"""

from typing import NamedTuple

from web3 import Web3


class Chain(NamedTuple):
    """One EVM chain FOMO trades on: what a listener needs to know that the contracts do not say."""

    code: str  # the column a row is printed under: see shared/feed.py
    name: str  # what the chain is called in a sentence
    variable: str  # the prefix of its two endpoints in .env, as <variable>_MAINNET_WSS and _HTTPS
    chain_id: int
    native: str  # what a native deposit is paid in; 18 decimals on every one of these chains
    cash: tuple[str, ...]  # the quote tokens a sell is paid out of, lowercased: never the thing sold
    block_seconds: float  # how far apart blocks are, which sets how long silence is allowed to last


# Keyed by the name a transport is called after: `rh-logs`, `rh-blocks`, `rh-off`. Each row
# was earned by maintenance/probe_chain.py, and its cash read off FOMO's own deposits there.
CHAINS = {
    "rh": Chain(
        code="RH",
        name="Robinhood Chain",
        variable="ROBINHOOD",
        chain_id=4663,
        native="ETH",
        cash=("0x5fc5360d0400a0fd4f2af552add042d716f1d168",),  # USDG, six decimals
        block_seconds=0.1,
    ),
    "bsc": Chain(
        code="BSC",
        name="BNB Smart Chain",
        variable="BSC",
        chain_id=56,
        native="BNB",
        # USDT and USDC, both eighteen decimals here rather than six: read per token, never assumed.
        cash=("0x55d398326f99059ff775485246999027b3197955", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),
        block_seconds=0.45,
    ),
    "base": Chain(
        code="BASE",
        name="Base",
        variable="BASE",
        chain_id=8453,
        native="ETH",
        cash=("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",),  # USDC
        block_seconds=2.0,
    ),
    "eth": Chain(
        code="ETH",
        name="Ethereum",
        variable="ETHEREUM",
        chain_id=1,
        native="ETH",
        # USDC, USDT
        cash=("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xdac17f958d2ee523a2206206994597c13d831ec7"),
        block_seconds=12.0,
    ),
    "monad": Chain(
        code="MON",
        name="Monad",
        variable="MONAD",
        chain_id=143,
        native="MON",
        # USDC, USDT0
        cash=("0x754704bc059f8c67012fed69bc8a327a5aafb603", "0xe7cd86e13ac4309349f30b3435a9d337750fc82d"),
        block_seconds=0.4,
    ),
    "elysium": Chain(
        code="ELY",
        name="Elysium",
        variable="ELYSIUM",
        chain_id=99901,
        native="HYPE",
        cash=("0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65",),  # USDC
        block_seconds=0.12,
    ),
}

# --- who is who, the same on every chain above -------------------------------
ENTRYPOINT = "0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108"
DEPOSITORY = "0x4cD00E387622C35bDDB9b4c962C136462338BC31"
# Relay's executor moves the tokens and its router is what a solver's fill calls. Relay
# replaced both in October 2026 and the two pairs overlapped while it moved over, so both
# are read: the current pair first, then the one every earlier trade went through.
EXECUTORS = ("0x0729e2a4716d9f7dd17858d5d504426d228697f7", "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f")
EXECUTOR_TOPICS = ["0x" + "0" * 24 + executor[2:] for executor in EXECUTORS]
ROUTERS = ("0x5567954cd026cebfe486062cb75848e0f1c12e58", "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be")
BURN = "0x0000000000000000000000000000000000000000"

# FOMO's second route across chains, which skips Relay. A sell sends the native asset from
# the wallet's own operation into this contract, which names the Solana recipient; a buy is one
# of two relayers calling it to swap its own native inventory into the token and deliver it. Every
# trade through it opens with an id event, whose data is the same intent id the Solana leg
# carries in its memo, so the two legs join on it the way a Relay order id joins them.
INTENT = "0xa834e70303322d86e5dae95ee47e9c6a073d9812"
INTENT_ID = "0x74647a56026fd5e5a5fdb01d686969d17f40667aeedffef44cdeb894156e35a4"
INTENT_SENT = "0xfe501452dda9020e758013c7a853ee26084957e0f6829b6f9b900741371c750b"
INTENT_FILLED = "0x7af24e5c613ac48d048691774e9628abdc41c7d6d0b61f915af585016c539ca8"
INTENT_PAID = "0x4246c15f4969fe0fc080f44aab1ae786773aaf3e47d9571b33b28d19d0f40f9e"  # beside most fills
# Every event a trade through the route emits; anything else is the route moving its own money.
INTENT_TRADE_EVENTS = (INTENT_ID, INTENT_SENT, INTENT_FILLED, INTENT_PAID)
# The relayers' two calls that fill a buy, with and without a payment beside it; in both,
# word 2 is the buyer and word 4 the router.
INTENT_FILL_CALLS = ("0xeb46f6eb", "0x4aae2e37")

# A FOMO wallet is an ordinary address carrying one EIP-7702 delegation, and the
# implementation it points at names the application behind it. FOMO_CODE is what
# eth_getCode returns for one, which is the whole of the wallet test.
DELEGATION = "0xef0100"
FOMO_IMPLEMENTATION = "e6cae83bde06e4c305530e199d7217f42808555b"
FOMO_CODE = DELEGATION + FOMO_IMPLEMENTATION
APPS = {FOMO_IMPLEMENTATION: "fomo"}

# --- what to listen for -----------------------------------------------------
USEROP = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
BEFORE_EXECUTION = "0xbb47ee3e183a558b1a2ff0874b079f3fc5478b7454eacf2bfc5af2ff5878f972"
DEPOSIT = "0x49fed1d0b752ce30eee63c7a81133f3363b532fec5d4d7dd1ccfd005de4555e1"
NATIVE_DEPOSIT = "0x8032066556caf3967d8fec4ad22a2d9e1e9576556b2903a0fcd5b1fd201e3477"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
VENUES = {
    V4_SWAP: "UniV4",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "UniV3",
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83": "PancakeV3",
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": "UniV2",
    # PancakeSwap Infinity, on BNB Smart Chain: Uniswap V4's shape with a protocol fee added.
    "0x04206ad2b7c0f463bff3dd4f33c5735b0f2957a351e4f79763a4fa9e775dd237": "PancakeInf",
    # The Solidly pair shape, which Aerodrome's pools on Base emit.
    "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b": "Solidly",
}
SYMBOL, DECIMALS = "0x95d89b41", "0x313ce567"  # symbol(), decimals()

# V4 is a singleton: one contract emits for every pool, the pool is topic 1, and the
# fee charged on the swap is the last field of the data rather than a fee() call.
V4_FIELDS = ["int128", "int128", "uint160", "uint128", "int24", "uint24"]


def word(data, i):
    """ABI word `i` of a hex payload with its 0x already stripped."""
    return data[i * 64 : (i + 1) * 64]


def app_name(code):
    """Which application a wallet belongs to, read from its EIP-7702 delegation.

    The application's name when it is one we know, the implementation address when
    it is not, "eoa" for an address with no code, and "contract" for anything else.
    FOMO is one tenant of these contracts, so this is a label rather than a test.
    """
    code = (code or "0x").lower()
    if code == "0x":
        return "eoa"
    if code.startswith(DELEGATION):
        implementation = code[len(DELEGATION) :]
        return APPS.get(implementation, implementation[:8] + "…")
    return "contract"


def topic(log, index=0):
    """Topic `index` of a log as 0x-prefixed 32 bytes, whatever the client returned.

    Raw JSON-RPC hands back a prefixed string and web3 hands back HexBytes, whose
    `hex()` drops the prefix. Stripping `0x` with `lstrip` would eat a leading zero
    of the topic itself, so the prefix is removed once and the value padded back.
    A topic the log does not carry is "", which matches no topic: an anonymous event
    has none at all, and a receipt read whole will hold one sooner or later.
    """
    if len(log["topics"]) <= index:
        return ""
    raw = log["topics"][index]
    raw = raw if isinstance(raw, str) else raw.hex()
    return "0x" + raw.removeprefix("0x").rjust(64, "0")


def sponsored(operation):
    """Did a paymaster pay for this `UserOperationEvent`? Topic 3 is the paymaster.

    FOMO's bundler absorbs the gas, so a FOMO operation names no paymaster. Its delegation
    is the reference account deployed at one address on every chain, and on most of them
    other applications delegate to it too; the ones seen sponsor their users' operations
    through a paymaster. So a paymaster is what tells such a wallet's deposit from FOMO's.
    """
    return int(operation["topics"][3], 16) != 0


def span(boundaries, index):
    """The user operation a log belongs to, as an exclusive (start, end) range.

    A handleOps bundle is one flat list of logs delimited by `UserOperationEvent`,
    so a log belongs to whoever's boundary comes next. Joining by transaction hash
    instead credits one user's swap to another user's deposit.
    """
    ordered = sorted(boundaries)
    return (max([b for b in ordered if b < index], default=-1), next((b for b in ordered if b > index), float("inf")))


def decode_string(raw):
    """An ABI-encoded string return value, or a bytes32 symbol from an older token."""
    if len(raw) >= 96 and int.from_bytes(raw[:32]) == 32:
        length = int.from_bytes(raw[32:64])
        return raw[64 : 64 + length].decode(errors="replace")
    return raw.rstrip(b"\x00").decode(errors="replace") or "?"


def trade_key(log):
    """What identifies one trade: the log that names it, not its transaction.

    A handleOps bundle carries several users, so two trades share a hash.
    """
    return f"{log['transactionHash']}#{int(log['logIndex'], 16)}"


def quantity(raw, decimals, symbol):
    """An amount when the token's decimals are known, the raw integer when they are not.

    A failed decimals lookup used to become 18, which prints a number that looks
    right and is wrong by whatever the real scale was. An unscaled value marked `?`
    is the honest answer, and the sidecar keeps it whole.
    """
    return f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw}"


def padded_topic(address):
    """An address as a 32-byte topic, for matching it in an indexed log position."""
    return "0x" + "0" * 24 + address[2:].lower()


def where_of(log):
    """`blk 58394188` — the block a log sits in, as a row reports it."""
    return f"blk {int(log['blockNumber'], 16)}"


def address_in_topic(log, index):
    """The address indexed topic `index` holds, checksummed: 32 bytes carrying 20."""
    return Web3.to_checksum_address("0x" + log["topics"][index][-40:])


def address_in_word(log, index):
    """The address data word `index` holds, checksummed."""
    return Web3.to_checksum_address("0x" + word(log["data"].removeprefix("0x"), index)[24:])


def erc20_deposit(log):
    """(token, raw amount, order id) out of a `RelayErc20Deposit`.

    Word 0 is the depositor, which `address_in_word` reads.
    """
    data = log["data"].removeprefix("0x")
    return "0x" + word(data, 1)[24:], int(word(data, 2), 16), "0x" + word(data, 3)


def native_deposit(log):
    """(raw amount, order id) out of a `RelayNativeDeposit`, which names no token.

    The token is ether, so the amount sits one word earlier than in the ERC-20 shape and
    the order id with it. Reading only the ERC-20 shape loses every buy paid for in ether.
    """
    data = log["data"].removeprefix("0x")
    return int(word(data, 1), 16), "0x" + word(data, 2)


# The order id of a fill whose transaction could not be fetched. Empty would say the trade
# never crossed, which is a claim about the trade; this is a claim about the lookup.
UNREAD_ORDER = "?"


def order_from_calldata(info):
    """The Relay order id a solver's fill carries: the last word of the router call.

    This is the whole cross-chain join — the same id the deposit on the other chain
    names — with no Relay API in the path. Empty for any transaction that is not a
    router call, which is every transaction that is not a fill. `info` is an
    `eth_getTransactionByHash` result, None when the lookup failed.
    """
    if info is None:
        return UNREAD_ORDER
    if (info.get("to") or "").lower() not in ROUTERS:
        return ""
    calldata = info["input"][2:]
    return "0x" + calldata[-64:] if len(calldata) >= 64 else ""


def venues(swaps, start=None, end=None):
    """The venues a trade ran through, in order, joined for the `note` column.

    `swaps` is (log index, venue) pairs from one transaction. With no span every swap in
    it counts, which is right for a solver's fill: that is its own transaction rather
    than a bundle, so there is nothing to scope by. With a span only the swaps inside the
    depositor's own user operation count, because a bundle carries several users and a
    hash join would credit one person's swap to another person's deposit.
    """
    inside = swaps if start is None else [(i, v) for i, v in swaps if start < i < end]
    return "+".join(dict.fromkeys(v for _, v in inside))


# --- the intent route --------------------------------------------------------
# A trade through the intent contract is read out of its whole receipt, because what makes
# it a trade — the token leaving the seller, the token reaching the buyer — is a Transfer
# that names neither the contract nor anything a filter could know in advance.


def intent_ids(logs):
    """{topic 1: intent id} out of a transaction's id events.

    Topic 1 is a 32-byte key the contract's other events repeat; it is not a hash of the
    id, so the id is only ever read from the event that states it.
    """
    return {
        topic(log, 1): bytes.fromhex(log["data"][2:66]).rstrip(b"\x00").decode(errors="replace")
        for log in logs
        if log["address"].lower() == INTENT and topic(log) == INTENT_ID
    }


def intent_sent(log):
    """(sender, raw native amount) out of a sell's event: the wallet and what it sent."""
    return address_in_topic(log, 2), int(word(log["data"].removeprefix("0x"), 3), 16)


def intent_buyer(info):
    """The wallet an intent fill delivers to, or None for any other transaction.

    The fill's events name the router the relayer swapped through, never the buyer; the
    buyer is word 2 of the relayer's call, in either of its two shapes. `info` is an
    `eth_getTransactionByHash` result.
    """
    if not info or (info.get("to") or "").lower() != INTENT or not info["input"].startswith(INTENT_FILL_CALLS):
        return None
    return Web3.to_checksum_address("0x" + word(info["input"][10:], 2)[24:])


def moved_from(logs, user, start, end):
    """(token, raw amount) of the first token `user` sent inside (start, end), or None.

    That is the token a sell gave up: the operation hands it to a router, and everything
    after is the router's. Scoped to the user's own operation, as a deposit is.
    """
    for log in logs:
        if topic(log) != TRANSFER or len(log["topics"]) < 3:
            continue
        if start < int(log["logIndex"], 16) < end and address_in_topic(log, 1).lower() == user.lower():
            return log["address"], int(log["data"][2:] or "0", 16)
    return None


def delivered_to(logs, buyer):
    """(token, raw amount) of the last token that reached `buyer`, or None."""
    landed = [
        (log["address"], int(log["data"][2:] or "0", 16))
        for log in logs
        if topic(log) == TRANSFER and len(log["topics"]) > 2 and address_in_topic(log, 2).lower() == buyer.lower()
    ]
    return landed[-1] if landed else None


def swaps_in(logs):
    """(log index, venue) for every swap a receipt holds, the shape `venues` reads."""
    return [(int(log["logIndex"], 16), VENUES[topic(log)]) for log in logs if topic(log) in VENUES]
