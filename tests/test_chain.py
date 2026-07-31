from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.chain import (  # noqa: E402
    TransactionBudget,
    load_wallet_credentials,
    parse_chain_id,
    require_base_sepolia,
)


class ChainTests(unittest.TestCase):
    def test_chain_id_gate(self) -> None:
        response = b'{"jsonrpc":"2.0","id":84532,"result":"0x14a34"}'
        chain_id = parse_chain_id(response)
        self.assertEqual(chain_id, 84532)
        require_base_sepolia(chain_id)
        with self.assertRaisesRegex(RuntimeError, "refusing transaction"):
            require_base_sepolia(8453)

    def test_private_key_is_redacted_from_repr(self) -> None:
        secret = "ab" * 32
        credentials = load_wallet_credentials(
            {
                "BASE_SEPOLIA_RPC_URL": "https://sepolia.base.org",
                "BASE_SEPOLIA_PRIVATE_KEY": secret,
                "BASE_SEPOLIA_PUBLIC_ADDRESS": "0x" + "12" * 20,
            }
        )
        self.assertNotIn(secret, repr(credentials))

    def test_transaction_budget_caps(self) -> None:
        budget = TransactionBudget(max_transactions=2, max_total_wei=10_000)
        budget.reserve(4_000)
        budget.reserve(5_000)
        with self.assertRaisesRegex(RuntimeError, "transaction-count"):
            budget.reserve(500)


if __name__ == "__main__":
    unittest.main()
