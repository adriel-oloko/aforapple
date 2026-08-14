"""Read-only Sepolia diagnostic: what landed on the watch address.

Checks, for 0x217153D3CC0Ca2E72d633Af400b554d5Bb1aB5F8 on Ethereum Sepolia:
  1. Latest block number
  2. Native (ETH) balance
  3. Any ERC20 Transfer log TO the address across ALL contracts (topic2
     filter only) over the last ~4000 blocks
  4. Specifically Circle USDC (0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238)
     transfers to the address, wide range
No writes, no keys.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from services.config import load_config

cfg = load_config()
ADDR = cfg.test_watch_address
USDC = cfg.test_token_contract
RPC = cfg.test_rpc_urls[0]

padded = "0x" + ADDR.lower().removeprefix("0x").zfill(64)
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def rpc(method, params):
    r = httpx.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


latest = int(rpc("eth_blockNumber", []), 16)
print("latest sepolia block:", latest)

bal = int(rpc("eth_getBalance", [ADDR, "latest"]), 16)
print(f"native ETH balance: {bal / 1e18:.6f} ETH")

# 1) Any ERC20 Transfer to the address, all contracts, last 4000 blocks.
logs = rpc(
    "eth_getLogs",
    [{"fromBlock": hex(latest - 4000), "toBlock": hex(latest), "topics": [TRANSFER, None, padded]}],
)
print(f"any-token Transfer logs to {ADDR[:10]}... in last 4000 blocks: {len(logs)}")
for e in logs[:20]:
    contract = e["address"]
    value = int(e["data"][2:66], 16)
    tx = e["transactionHash"]
    blk = int(e["blockNumber"], 16)
    print(f"  block={blk} token={contract} value_raw={value} tx={tx[:18]}...")

# 2) Circle USDC specifically, last 20000 blocks (~2.8 days).
usdc_logs = rpc(
    "eth_getLogs",
    [{"address": USDC, "fromBlock": hex(latest - 20000), "toBlock": hex(latest), "topics": [TRANSFER, None, padded]}],
)
print(f"Circle USDC Transfer logs to address in last 20000 blocks: {len(usdc_logs)}")
for e in usdc_logs[:20]:
    value = int(e["data"][2:66], 16) / 1e6
    print(f"  block={int(e['blockNumber'], 16)} usdc={value:.6f} tx={e['transactionHash'][:18]}...")
