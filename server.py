import os
import time
import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
RPC_URL        = "https://rpc.monad.xyz"
LENS_ADDRESS   = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea"
POLL_INTERVAL  = 7   # seconds between price checks
TCG_SELL_AT    = 900_000  # USD market cap trigger

PRIVATE_KEY    = os.environ.get("PRIVATE_KEY", "")
if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

TOKENS = {
    "TCG":  "0x94CF69B5b13E621cB11f5153724AFb58c7337777",
    "FIRE": "0xCE1C9994331e1fd8E3B751De9Cf50c322BCb7777",
    "MONA": "0x2CB31c268819EE133De378A3A2E087F0b0eC7777",
}

# ── ABIs ─────────────────────────────────────────────────────────────────────
LENS_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "bool",    "name": "isBuy",    "type": "bool"},
        ],
        "name": "getAmountOut",
        "outputs": [
            {"internalType": "address", "name": "router",    "type": "address"},
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount",  "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ROUTER_ABI = [
    {
        "type": "function",
        "name": "sell",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "amountIn",     "type": "uint256"},
                    {"name": "amountOutMin", "type": "uint256"},
                    {"name": "token",        "type": "address"},
                    {"name": "to",           "type": "address"},
                    {"name": "deadline",     "type": "uint256"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
    }
]

# ── Setup ─────────────────────────────────────────────────────────────────────
w3      = Web3(Web3.HTTPProvider(RPC_URL))
lens    = w3.eth.contract(address=Web3.to_checksum_address(LENS_ADDRESS), abi=LENS_ABI)
account = w3.eth.account.from_key(PRIVATE_KEY)
WALLET  = account.address

ONE_MON = Web3.to_wei(1, "ether")

_mon_usd_cache: tuple[float, float] = (0.0, 0.0)
MON_PRICE_TTL = 60


def get_mon_usd_price() -> float:
    global _mon_usd_cache
    price, fetched_at = _mon_usd_cache
    if time.time() - fetched_at < MON_PRICE_TTL:
        return price

    # Try Binance first
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "MONUSDT"},
            timeout=5,
        )
        resp.raise_for_status()
        price = float(resp.json()["price"])
        _mon_usd_cache = (price, time.time())
        return price
    except Exception:
        pass

    # Fall back to CoinGecko
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "monad", "vs_currencies": "usd"},
        headers={"User-Agent": "monad-trading-bot/1.0"},
        timeout=5,
    )
    resp.raise_for_status()
    price = float(resp.json()["monad"]["usd"])
    _mon_usd_cache = (price, time.time())
    return price


def fetch_price(symbol: str, token_address: str, mon_usd: float) -> float:
    """Returns market cap in USD."""
    checksum = Web3.to_checksum_address(token_address)

    _, amount_out = lens.functions.getAmountOut(checksum, ONE_MON, True).call()

    tokens_per_mon = Web3.from_wei(amount_out, "ether")
    mon_per_token  = 1 / tokens_per_mon if tokens_per_mon else 0

    token_contract   = w3.eth.contract(address=checksum, abi=ERC20_ABI)
    total_supply_raw = token_contract.functions.totalSupply().call()
    total_supply     = Web3.from_wei(total_supply_raw, "ether")

    market_cap_usd = float(mon_per_token) * float(total_supply) * mon_usd

    wallet_balance     = Web3.from_wei(token_contract.functions.balanceOf(WALLET).call(), "ether")
    wallet_balance_usd = float(wallet_balance) * float(mon_per_token) * mon_usd

    print(
        f"  {symbol:<6}  "
        f"market cap: ${market_cap_usd:>14,.2f}  │  "
        f"owned: ${wallet_balance_usd:>12,.2f}"
    )
    return market_cap_usd


def sell_all_tcg() -> None:
    tcg_address = Web3.to_checksum_address(TOKENS["TCG"])
    tcg         = w3.eth.contract(address=tcg_address, abi=ERC20_ABI)

    balance = tcg.functions.balanceOf(WALLET).call()
    if balance == 0:
        print("  [sell] TCG balance is 0, nothing to sell.")
        return

    # Get sell quote
    router_address, mon_out = lens.functions.getAmountOut(tcg_address, balance, False).call()
    amount_out_min = int(mon_out * 95 // 100)  # 5% slippage tolerance

    router   = w3.eth.contract(address=Web3.to_checksum_address(router_address), abi=ROUTER_ABI)
    chain_id = w3.eth.chain_id
    deadline = int(time.time()) + 300

    # 1) Approve router to spend TCG
    print(f"  [sell] Approving router {router_address} to spend {Web3.from_wei(balance, 'ether'):.4f} TCG …")
    approve_tx = tcg.functions.approve(router_address, balance).build_transaction({
        "from":     WALLET,
        "nonce":    w3.eth.get_transaction_count(WALLET),
        "chainId":  chain_id,
        "gas":      100_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed  = w3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"  [sell] Approved — tx {tx_hash.hex()}")

    # 2) Sell (re-fetch nonce after approve is mined)
    print(f"  [sell] Selling {Web3.from_wei(balance, 'ether'):.4f} TCG (min {Web3.from_wei(amount_out_min, 'ether'):.4f} MON) …")
    sell_tx = router.functions.sell((
        balance,
        amount_out_min,
        tcg_address,
        WALLET,
        deadline,
    )).build_transaction({
        "from":     WALLET,
        "nonce":    w3.eth.get_transaction_count(WALLET),
        "chainId":  chain_id,
        "gas":      200_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed  = w3.eth.account.sign_transaction(sell_tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"  [sell] SOLD — tx {tx_hash.hex()}")


def main() -> None:
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {RPC_URL}")

    print(f"Connected to Monad  (block {w3.eth.block_number})")
    print(f"Wallet: {WALLET}\n")

    tcg_sold = False
    mon_usd  = 0.0

    while True:
        try:
            mon_usd = get_mon_usd_price()
        except Exception as exc:
            print(f"  [warn] MON/USD fetch failed (using last known ${mon_usd:.4f}) — {exc}")

        print(f"[{time.strftime('%H:%M:%S')}]  MON = ${mon_usd:.4f}")
        for symbol, address in TOKENS.items():
            try:
                mcap = fetch_price(symbol, address, mon_usd)
                if symbol == "TCG" and not tcg_sold and mcap >= TCG_SELL_AT:
                    print(f"  [sell] TCG market cap ${mcap:,.2f} hit trigger ${TCG_SELL_AT:,} — selling!")
                    for attempt in range(1, 4):
                        try:
                            sell_all_tcg()
                            tcg_sold = True
                            break
                        except Exception as sell_exc:
                            print(f"  [sell] attempt {attempt}/3 failed — {sell_exc}")
                            if attempt < 3:
                                time.sleep(3)
                    if not tcg_sold:
                        print("  [sell] all 3 attempts failed — will retry next poll cycle")
            except Exception as exc:
                print(f"  {symbol:<6}  ERROR — {exc}")
        print()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
