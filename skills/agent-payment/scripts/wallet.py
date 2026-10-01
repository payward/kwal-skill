# /// script
# requires-python = ">=3.11"
# dependencies = ["web3==7.13.0"]
# ///
"""Create, fund, and inspect the owner wallet for agent wallet mode."""

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import rlp
from eth_abi import decode
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from web3.exceptions import TransactionNotFound

from errors import ConfigurationError
from session import refuse_a_checkout_path


NETWORKS = {
    "ethereum": {
        "chain_id": 11155111,
        "rpc": "https://ethereum-sepolia-rpc.publicnode.com",
        "usdc": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
        "explorer": "https://sepolia.etherscan.io",
    },
    "ink": {
        "chain_id": 763373,
        "rpc": "https://rpc-gel-sepolia.inkonchain.com",
        "usdc": "0xFabab97dCE620294D2B0b0e46C68964e326300Ac",
        "explorer": "https://explorer-sepolia.inkonchain.com",
    },
}
ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]
# The Ink Sepolia OptimismPortal on Ethereum Sepolia mints deposited ETH to the
# same address on Ink.
INK_PORTAL = "0x5c1d29C6c9C8b0800692acC95D700bcb4966A1d7"
DEPOSIT_EVENT = Web3.keccak(text="TransactionDeposited(address,address,uint256,bytes)")
DEPOSIT_GAS = 100000
PORTAL_ABI = [{
    "type": "function", "name": "depositTransaction", "stateMutability": "payable",
    "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
               {"name": "gasLimit", "type": "uint64"}, {"name": "isCreation", "type": "bool"},
               {"name": "data", "type": "bytes"}], "outputs": [],
}]
BRIDGE_ASSET = "ETH-to-Ink"
KEY_FILE = "private-key.txt"


def wallet_root():
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config).expanduser() if config else Path.home() / ".config"
    if not base.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path.")
    path = base / "pws/agent-payment/wallet"
    # A key inside a checkout can reach a commit, exactly like the session token.
    try:
        return refuse_a_checkout_path(path)
    except ConfigurationError:
        raise ValueError(f"Cannot keep the wallet at {path}. Set XDG_CONFIG_HOME outside a repository checkout.") from None


def checked_id(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise ValueError("Use a request ID with 1-48 lowercase letters, digits, or hyphens; start with a letter.")
    return value


def check_permissions(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Expected an owner-only {'directory' if directory else 'file'}: {path}")


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_permissions(path, directory=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def read_private(path):
    check_permissions(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return stream.read()


def save_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(root):
    private_dir(root)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def load_wallet(root):
    check_permissions(root, directory=True)
    try:
        account = Account.from_key(read_private(root / KEY_FILE).strip())
    except (ValueError, TypeError):
        raise ValueError("Cannot recover the wallet key.") from None
    metadata = json.loads(read_private(root / "wallet.json"))
    if metadata.get("address") != account.address:
        raise ValueError("Wallet address does not match its key.")
    return account


def wallet_info(root):
    account = load_wallet(root)
    message = encode_defunct(text="agent-payment local recovery check")
    signed = account.sign_message(message)
    if Account.recover_message(message, signature=signed.signature) != account.address:
        raise ValueError("Local signature recovery failed.")
    return {"address": account.address, "recovery_verified": True,
            "private_key_file": str(root / KEY_FILE)}


def create_wallet(root):
    if any((root / name).exists() or (root / name).is_symlink() for name in (KEY_FILE, "wallet.json")):
        return {"created": False, **wallet_info(root)}
    account = Account.create()
    write_new(root / KEY_FILE, Web3.to_hex(account.key) + "\n")
    write_new(root / "wallet.json", json.dumps({
        "address": account.address, "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "testnet-only",
    }, indent=2) + "\n")
    return {"created": True, **wallet_info(root)}


def connect(network):
    config = NETWORKS[network]
    url = os.environ.get(f"TESTNET_{network.upper()}_RPC", config["rpc"])
    if not url.startswith("https://"):
        raise ValueError("Use an HTTPS RPC URL.")
    web3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
    if web3.eth.chain_id != config["chain_id"]:
        raise ValueError(f"RPC chain ID does not match {network} testnet.")
    token = web3.eth.contract(address=config["usdc"], abi=ABI)
    if not web3.eth.get_code(config["usdc"]) or token.functions.decimals().call() != 6:
        raise ValueError("The configured Circle USDC contract is missing or has unexpected decimals.")
    return web3, token


def read_balances(web3, token, address):
    block = web3.eth.block_number
    return {
        "block": block,
        "eth_wei": web3.eth.get_balance(address, block_identifier=block),
        "usdc_units": token.functions.balanceOf(address).call(block_identifier=block),
    }


def display_balances(values):
    return {**values, "eth": str(Decimal(values["eth_wei"]) / 10**18),
            "usdc": str(Decimal(values["usdc_units"]) / 10**6)}


def units(value, decimals, maximum):
    try:
        number = Decimal(value)
        scaled = number * 10**decimals
        if not number.is_finite() or not 0 < number <= maximum or scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Amount must be above 0 and at most {maximum}, with at most {decimals} decimal places.") from None


def journal_path(root, network):
    return root / f"transactions-{network}.json"


def read_journal(path):
    if path.exists() or path.is_symlink():
        value = json.loads(read_private(path))
        if not isinstance(value, list):
            raise ValueError("Invalid transaction journal.")
        return value
    return []


def transaction_view(entry, network):
    return {key: entry[key] for key in ("hash", "asset", "units", "recipient", "status", "request_id") if key in entry} | {
        "explorer": NETWORKS[network]["explorer"] + "/tx/" + entry["hash"]}


def reconcile(web3, entries, path):
    for entry in entries:
        if entry["status"] == "reverted":
            raise ValueError("A transfer reverted. Inspect the journal and receipt before a manual retry.")
        if entry["status"] == "confirmed":
            continue
        try:
            receipt = web3.eth.get_transaction_receipt(entry["hash"])
        except TransactionNotFound:
            raw = bytes.fromhex(entry["raw"][2:])
            if Web3.to_hex(Web3.keccak(raw)) != entry["hash"]:
                raise ValueError("Signed transaction hash does not match the journal.")
            try:
                web3.eth.send_raw_transaction(raw)
            except Exception:
                return {"state": "unresolved", "hash": entry["hash"],
                        "next": "Check this hash and RPC availability. Keep the journal; do not create a replacement transfer."}
            return {"state": "pending", "hash": entry["hash"]}
        if web3.eth.block_number < receipt["blockNumber"] + 1:
            return {"state": "pending", "hash": entry["hash"]}
        if web3.eth.get_block(receipt["blockNumber"])["hash"] != receipt["blockHash"]:
            return {"state": "pending", "hash": entry["hash"]}
        entry["status"] = "confirmed" if receipt["status"] == 1 else "reverted"
        save_json(path, entries)
        if entry["status"] == "reverted":
            raise ValueError("Transfer reverted. Inspect the receipt before a manual retry.")
    return None


def saved_request(entries, request_id, entry):
    existing = next((item for item in entries if item.get("request_id") == request_id), None)
    if existing and any(existing.get(key) != entry[key] for key in ("asset", "units", "recipient")):
        raise ValueError("This request ID has different parameters. Restore the original command.")
    return existing


def submit(web3, sender, network, tx, entry, path, entries, eth_balance):
    nonce = web3.eth.get_transaction_count(sender.address, "latest")
    if nonce != web3.eth.get_transaction_count(sender.address, "pending"):
        raise ValueError("Wallet has an untracked pending transaction. Wait before sending.")
    block = web3.eth.get_block("latest")
    priority = web3.eth.max_priority_fee
    tx = {**tx, "chainId": NETWORKS[network]["chain_id"], "nonce": nonce, "from": sender.address,
          "type": 2, "maxPriorityFeePerGas": priority,
          "maxFeePerGas": 2 * block["baseFeePerGas"] + priority}
    tx["gas"] = (web3.eth.estimate_gas(tx) * 12 + 9) // 10
    maximum_fee = tx["gas"] * tx["maxFeePerGas"]
    if maximum_fee > 10**15:
        raise ValueError("Estimated execution fee exceeds the 0.001 ETH per-transaction limit.")
    if eth_balance < tx["value"] + maximum_fee + 10**14:
        return {"state": "funds_needed", "address": sender.address, "network": network,
                "next": "Add ETH for the transfer and the gas reserve."}
    signed = sender.sign_transaction(tx)
    entry = {**entry, "hash": Web3.to_hex(signed.hash), "raw": Web3.to_hex(signed.raw_transaction),
             "status": "prepared"}
    entries.append(entry)
    save_json(path, entries)
    pending = reconcile(web3, entries, path)
    return {"state": "pending", "transaction": transaction_view(entry, network), **(pending or {})}


def deposit_hash(receipt, address, amount):
    logs = [log for log in receipt["logs"] if log["address"].lower() == INK_PORTAL.lower()
            and log["topics"] and log["topics"][0] == DEPOSIT_EVENT]
    if len(logs) != 1:
        raise ValueError("Expected one Ink deposit event. Inspect the Sepolia receipt.")
    log = logs[0]
    opaque, = decode(["bytes"], log["data"])
    sender, recipient = bytes(log["topics"][1])[-20:], bytes(log["topics"][2])[-20:]
    mint, value = int.from_bytes(opaque[:32]), int.from_bytes(opaque[32:64])
    gas = int.from_bytes(opaque[64:72])
    if (len(opaque) != 73 or opaque[72] != 0 or int.from_bytes(log["topics"][3]) != 0
            or sender != bytes.fromhex(address[2:]) or recipient != sender
            or mint != amount or value != amount or gas != DEPOSIT_GAS):
        raise ValueError("Deposit event does not match the saved request.")
    source = Web3.keccak(bytes(32) + Web3.keccak(bytes(receipt["blockHash"]) + log["logIndex"].to_bytes(32)))
    payload = rlp.encode([bytes(source), sender, recipient, mint, value, gas, 0, b""])
    return Web3.to_hex(Web3.keccak(b"\x7e" + payload))


def deposit_status(l1, l2, entry):
    try:
        receipt = l1.eth.get_transaction_receipt(entry["hash"])
    except TransactionNotFound:
        # Public Sepolia RPC backends can lag behind a confirmed journal entry; the ID never sends again.
        return {"state": "pending", "hash": entry["hash"], "request_id": entry["request_id"]}
    if (receipt["status"] != 1 or l1.eth.block_number < receipt["blockNumber"] + 1
            or l1.eth.get_block(receipt["blockNumber"])["hash"] != receipt["blockHash"]):
        raise ValueError("Sepolia deposit receipt changed. Inspect before continuing.")
    l2_hash = deposit_hash(receipt, entry["recipient"], entry["units"])
    result = {"state": "bridge_pending", "l1_hash": entry["hash"], "l2_hash": l2_hash,
              "request_id": entry["request_id"],
              "l2_explorer": NETWORKS["ink"]["explorer"] + "/tx/" + l2_hash}
    try:
        l2_receipt = l2.eth.get_transaction_receipt(l2_hash)
    except TransactionNotFound:
        return result
    if l2_receipt["status"] != 1:
        raise ValueError("Ink deposit execution failed. Inspect both receipts; preserve the request ID.")
    if (l2.eth.block_number >= l2_receipt["blockNumber"] + 1
            and l2.eth.get_block(l2_receipt["blockNumber"])["hash"] == l2_receipt["blockHash"]):
        result["state"] = "bridged"
    return result


def bridge_eth(root, request_id, amount):
    sender = load_wallet(root)
    l1, _ = connect("ethereum")
    l2, _ = connect("ink")
    path = journal_path(root, "ethereum")
    entries = read_journal(path)
    entry = {"asset": BRIDGE_ASSET, "units": amount, "recipient": sender.address, "request_id": request_id}
    existing = saved_request(entries, request_id, entry)
    pending = reconcile(l1, entries, path)
    if pending:
        return pending
    if existing:
        return deposit_status(l1, l2, existing)
    if not l1.eth.get_code(INK_PORTAL):
        raise ValueError("The Ink bridge contract is missing on Ethereum Sepolia.")
    data = l1.eth.contract(address=INK_PORTAL, abi=PORTAL_ABI).encode_abi(
        "depositTransaction", args=[sender.address, amount, DEPOSIT_GAS, False, b""])
    tx = {"to": INK_PORTAL, "value": amount, "data": data}
    return submit(l1, sender, "ethereum", tx, entry, path, entries, l1.eth.get_balance(sender.address))


def transfer_usdc(root, request_id, recipient, amount):
    if not Web3.is_address(recipient):
        raise ValueError("Use the vault address that the funding command prints.")
    recipient = Web3.to_checksum_address(recipient)
    sender = load_wallet(root)
    if recipient == sender.address:
        raise ValueError("The vault address must differ from the owner wallet address.")
    web3, token = connect("ink")
    path = journal_path(root, "ink")
    entries = read_journal(path)
    entry = {"asset": "USDC", "units": amount, "recipient": recipient, "request_id": request_id}
    existing = saved_request(entries, request_id, entry)
    pending = reconcile(web3, entries, path)
    if pending:
        return pending
    if existing:
        return {"state": "confirmed", "transaction": transaction_view(existing, "ink")}
    balances = read_balances(web3, token, sender.address)
    if balances["usdc_units"] < amount:
        return {"state": "funds_needed", "address": sender.address, "network": "ink",
                "next": "Claim test USDC on Ink Sepolia.", **display_balances(balances)}
    if token.functions.transfer(recipient, amount).call({"from": sender.address}) is not True:
        raise ValueError("USDC transfer simulation failed.")
    tx = {"to": NETWORKS["ink"]["usdc"], "value": 0, "data": token.encode_abi("transfer", args=[recipient, amount])}
    return submit(web3, sender, "ink", tx, entry, path, entries, balances["eth_wei"])


def parser():
    result = argparse.ArgumentParser(description="Create, fund, and inspect the owner wallet. The key stays on disk.")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="Create the wallet, or reuse the saved one")
    commands.add_parser("info", help="Show the address and check that the saved key recovers it")
    for command in ("balance", "history"):
        commands.add_parser(command).add_argument("--network", choices=NETWORKS, required=True)
    commands.add_parser("networks")
    bridge = commands.add_parser("bridge", help="Deposit Sepolia ETH into the same wallet on Ink Sepolia")
    bridge.add_argument("--request-id", required=True, type=checked_id, help="Reuse this ID for every retry")
    bridge.add_argument("--eth", default="0.02", help="Deposit amount, not target balance; maximum 0.1")
    transfer = commands.add_parser("transfer", help="Send Ink Sepolia USDC to the vault")
    transfer.add_argument("--request-id", required=True, type=checked_id, help="Reuse this ID for every retry")
    transfer.add_argument("--to", required=True, help="Vault address")
    transfer.add_argument("--usdc", required=True, help="Amount to send; maximum 100")
    return result


def main():
    os.umask(0o077)
    args = parser().parse_args()
    if args.command == "networks":
        return NETWORKS
    root = wallet_root()
    with locked(root):
        if args.command == "create":
            return create_wallet(root)
        if args.command == "info":
            return wallet_info(root)
        if args.command == "bridge":
            return bridge_eth(root, args.request_id, units(args.eth, 18, Decimal("0.1")))
        if args.command == "transfer":
            return transfer_usdc(root, args.request_id, args.to, units(args.usdc, 6, Decimal("100")))
        account = load_wallet(root)
        if args.command == "history":
            return [transaction_view(entry, args.network)
                    for entry in read_journal(journal_path(root, args.network))]
        web3, token = connect(args.network)
        return {"network": args.network, "chain_id": NETWORKS[args.network]["chain_id"],
                "address": account.address, "usdc_contract": NETWORKS[args.network]["usdc"],
                **display_balances(read_balances(web3, token, account.address))}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2))
    except (ValueError, FileNotFoundError, BlockingIOError) as error:
        print(json.dumps({"state": "error", "message": str(error)}), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(json.dumps({"state": "error", "type": type(error).__name__,
                          "message": "Operation failed. Check RPC access and local files. Keep pending transaction journals."}), file=sys.stderr)
        sys.exit(1)
