#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Knots developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test that the wallet honours the rolling generation maturity.

A coinbase output still inside its rolling period cannot be spent, so the
wallet must report it as immature and keep it out of coin selection rather than
building transactions the mempool will reject.
"""

from decimal import Decimal

from test_framework.blocktools import COINBASE_MATURITY
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    assert_greater_than,
    assert_raises_rpc_error,
)

BASE_TIME = 1600000000
MATURITY_SECONDS = 3600
# Outputs below this height are exempt, so the wallet must offer those and
# hold back only the covered ones.
ACTIVATION_HEIGHT = 120
EXPIRY_TIME = BASE_TIME + 1000000
# The wallet signs with unified sighash whenever BLAKE2b is scheduled, and
# blocks only verify under that rule from the fork height onward, so the fork
# has to be reached here rather than parked out of range.
BLAKE2B_HEIGHT = 100


class WalletRollingCoinbaseMaturityTest(BitcoinTestFramework):
    def add_options(self, parser):
        self.add_wallet_options(parser, legacy=False)

    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}',
            f'-rdtsexpiry={EXPIRY_TIME}',
            f'-coinbasematurityrolling={ACTIVATION_HEIGHT}:{MATURITY_SECONDS}',
        ]]

    def skip_test_if_missing_module(self):
        self.skip_if_no_wallet()

    def mtp(self):
        node = self.nodes[0]
        return node.getblockheader(node.getbestblockhash())['mediantime']

    def offers_height(self, wallet, height):
        tip = self.nodes[0].getblockcount()
        return any(tip - entry['confirmations'] + 1 == height for entry in wallet.listunspent())

    def advance_mtp_to(self, when, address):
        """Drive the median-time-past to exactly `when`."""
        node = self.nodes[0]
        while self.mtp() < when:
            node.setmocktime(when)
            self.generatetoaddress(node, 1, address)
        assert_equal(self.mtp(), when)

    def run_test(self):
        node = self.nodes[0]
        node.setmocktime(BASE_TIME)
        node.createwallet('miner')
        wallet = node.get_wallet_rpc('miner')
        address = wallet.getnewaddress()

        self.generatetoaddress(node, 250, address)
        tip_height = node.getblockcount()
        covered_anchor = node.getblockheader(node.getblockhash(ACTIVATION_HEIGHT - 1))['mediantime']

        self.log.info("Outputs mined before the flag day are spendable once buried")
        assert self.mtp() < covered_anchor + MATURITY_SECONDS, "setup: covered outputs must still be inside their period"
        balances = wallet.getbalances()['mine']
        assert_greater_than(balances['trusted'], 0)
        assert_greater_than(balances['immature'], 0)

        self.log.info("...and every offered output is one of them")
        for entry in wallet.listunspent():
            height = tip_height - entry['confirmations'] + 1
            assert height < ACTIVATION_HEIGHT, f'offered a covered output from height {height}'

        # Every buried output below the flag day must be offered, not just some
        # of them: the cutoff has to floor at the flag day regardless of how
        # little time has passed.
        for height in range(1, ACTIVATION_HEIGHT):
            if tip_height + 1 - height >= COINBASE_MATURITY:
                assert self.offers_height(wallet, height), f'exempt output at height {height} was withheld'

        self.log.info("The wallet spends an exempt output without trouble")
        txid = wallet.sendtoaddress(address, Decimal('1.0'))
        assert txid in node.getrawmempool()
        block_hash = self.generatetoaddress(node, 1, address)[0]
        assert txid in node.getblock(block_hash)['tx']

        self.log.info("The boundary is exact: the output matures the instant its own period ends")
        # ACTIVATION_HEIGHT is anchored to the block before it, so it becomes
        # spendable exactly when the median-time-past reaches that anchor plus
        # the period, and not a moment earlier.
        before = wallet.getbalances()['mine']['trusted']
        self.advance_mtp_to(covered_anchor + MATURITY_SECONDS - 1, address)
        assert_equal(self.mtp(), covered_anchor + MATURITY_SECONDS - 1)
        assert not self.offers_height(wallet, ACTIVATION_HEIGHT), "matured one second early"
        self.advance_mtp_to(covered_anchor + MATURITY_SECONDS, address)
        assert self.offers_height(wallet, ACTIVATION_HEIGHT), "did not mature on its own instant"
        assert_greater_than(wallet.getbalances()['mine']['trusted'], before)


if __name__ == '__main__':
    WalletRollingCoinbaseMaturityTest(__file__).main()
