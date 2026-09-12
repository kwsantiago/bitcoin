#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test that the wallet treats frozen coinbase outputs as unspendable.

During the coinbase spend freeze a mined output cannot be spent at any depth,
so the wallet must report it as immature and keep it out of coin selection
instead of building transactions the mempool will reject.
"""

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    assert_greater_than,
    assert_raises_rpc_error,
)

BASE_TIME = 1600000000
FREEZE_START = BASE_TIME + 1000
EXPIRY_TIME = BASE_TIME + 100000
# Unlike the two consensus tests, which put the hardfork out of reach so only
# the freeze is under test, this one needs it actually active: the wallet signs
# with unified sighash whenever BLAKE2b is scheduled, and blocks only verify
# under that rule from the fork height onward.
BLAKE2B_HEIGHT = 100


class WalletCoinbaseSpendFreezeTest(BitcoinTestFramework):
    def add_options(self, parser):
        self.add_wallet_options(parser, legacy=False)

    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}',
            f'-rdtsexpiry={EXPIRY_TIME}',
            f'-coinbasefreezestart={FREEZE_START}',
        ]]

    def skip_test_if_missing_module(self):
        self.skip_if_no_wallet()

    def freeze_active(self):
        return FREEZE_START <= self.nodes[0].getblockchaininfo()['mediantime'] < EXPIRY_TIME

    def run_test(self):
        node = self.nodes[0]
        node.setmocktime(BASE_TIME)
        node.createwallet('miner')
        wallet = node.get_wallet_rpc('miner')
        address = wallet.getnewaddress()

        self.generatetoaddress(node, 200, address)
        assert_equal(self.freeze_active(), False)

        spendable = wallet.getbalances()['mine']['trusted']
        assert_greater_than(spendable, 0)
        self.log.info(f"Before the flag day the wallet can spend its coinbase outputs ({spendable} BTC trusted)")
        txid = wallet.sendtoaddress(address, Decimal('1.0'))
        assert txid in node.getrawmempool()
        self.generatetoaddress(node, 1, address)

        self.log.info("Crossing the flag day")
        node.setmocktime(FREEZE_START)
        self.generatetoaddress(node, 6, address)
        assert_equal(self.freeze_active(), True)

        self.log.info("During the window every coinbase output is reported immature, not spendable")
        balances = wallet.getbalances()['mine']
        assert_greater_than(balances['immature'], spendable)
        # Only the ordinary outputs of the pre-freeze spend are still offered;
        # every coinbase output has moved to the immature balance.
        unspent = wallet.listunspent()
        assert unspent
        assert all(entry['txid'] == txid for entry in unspent)
        assert_equal(balances['trusted'], sum(entry['amount'] for entry in unspent))
        assert_raises_rpc_error(-6, 'Insufficient funds', wallet.sendtoaddress, address, balances['trusted'] + 1)

        self.log.info("Once the window expires the wallet can spend them again")
        node.setmocktime(EXPIRY_TIME)
        self.generatetoaddress(node, 6, address)
        assert_equal(self.freeze_active(), False)
        assert_greater_than(wallet.getbalances()['mine']['trusted'], spendable)
        txid = wallet.sendtoaddress(address, Decimal('1.0'))
        assert txid in node.getrawmempool()
        self.generatetoaddress(node, 1, address)
        assert_equal(wallet.gettransaction(txid)['confirmations'], 1)


if __name__ == '__main__':
    WalletCoinbaseSpendFreezeTest(__file__).main()
