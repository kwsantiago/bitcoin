#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Knots developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the temporary extended generation maturity.

Until RDTS expires, a generation output created at or after the start height
must be the extended depth rather than COINBASE_MATURITY. Outputs mined before
that height keep the ordinary rule, so the deployment only applies going
forward and nothing already spendable is immobilized.
"""

from test_framework.blocktools import (
    add_witness_commitment,
    create_block,
    create_coinbase,
)
from test_framework.test_framework import BitcoinTestFramework
from test_framework.test_node import ErrorMatch
from test_framework.util import (
    assert_equal,
    assert_raises_rpc_error,
)
from test_framework.wallet import MiniWallet

SHORT = 100
LONG = 150
START = 200
BASE_TIME = 1600000000
EXPIRY_TIME = BASE_TIME + 100000
BLAKE2B_HEIGHT = 50

PREMATURE = 'bad-txns-premature-spend-of-coinbase'


class CoinbaseMaturityLongTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}',
            f'-rdtsexpiry={EXPIRY_TIME}',
            f'-coinbasematuritylong={START}:{LONG}',
        ]]

    def mtp(self):
        node = self.nodes[0]
        return node.getblockheader(node.getbestblockhash())['mediantime']

    def coinbase_utxo(self, height):
        for utxo in self.wallet.get_utxos(include_immature_coinbase=True, mark_as_spent=False):
            if utxo['coinbase'] and utxo['height'] == height:
                return utxo
        raise AssertionError(f'no unspent generation output at height {height}')

    def spend(self, height, *, expect_depth):
        depth = self.nodes[0].getblockcount() + 1 - height
        assert_equal(depth, expect_depth)
        return self.wallet.create_self_transfer(utxo_to_spend=self.coinbase_utxo(height))

    def block_with(self, tx):
        node = self.nodes[0]
        tip = node.getbestblockhash()
        height = node.getblockcount() + 1
        block = create_block(int(tip, 16), create_coinbase(height),
                             ntime=max(node.getblockheader(tip)['time'] + 1, self.mtp() + 1),
                             txlist=[tx], height=height, header_v2=height >= BLAKE2B_HEIGHT)
        add_witness_commitment(block)
        block.solve()
        return block

    def mine_to(self, height):
        if height > self.nodes[0].getblockcount():
            self.generate(self.wallet, height - self.nodes[0].getblockcount())

    def check_rejected_schedules(self):
        """The option must refuse anything that would weaken or silently
        disable the rule."""
        node = self.nodes[0]
        self.stop_node(0)
        expiry = f'-rdtsexpiry={EXPIRY_TIME}'
        fork = f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}'
        for args, msg in [
            # A depth below the ordinary rule would weaken the network rule
            # itself, and CTxMemPool::check asserts against COINBASE_MATURITY.
            ([fork, expiry, f'-coinbasematuritylong={START}:{SHORT - 1}'], 'a depth of at least'),
            ([fork, expiry, f'-coinbasematuritylong={START}:0'], 'a depth of at least'),
            # The deployment ends with RDTS, so it needs one.
            ([fork, f'-coinbasematuritylong={START}:{LONG}'], 'requires -rdtsexpiry'),
            # The unscheduled sentinel is not a usable start height.
            ([fork, expiry, f'-coinbasematuritylong=2147483647:{LONG}'], 'need 0 <= start'),
            ([fork, expiry, f'-coinbasematuritylong=-1:{LONG}'], 'need 0 <= start'),
            # Malformed and out of range.
            ([fork, expiry, '-coinbasematuritylong=200'], 'Invalid format'),
            ([fork, expiry, f'-coinbasematuritylong=200:{LONG}:7'], 'Invalid format'),
            ([fork, expiry, '-coinbasematuritylong=abc:150'], 'Invalid format'),
            ([fork, expiry, '-coinbasematuritylong=200:'], 'Invalid format'),
            ([fork, expiry, '-coinbasematuritylong=99999999999999999999:150'], 'Invalid format'),
        ]:
            node.assert_start_raises_init_error(extra_args=args, expected_msg=msg, match=ErrorMatch.PARTIAL_REGEX)
        # Exactly the ordinary depth is the weakest schedule allowed.
        self.start_node(0, extra_args=[fork, expiry, f'-coinbasematuritylong={START}:{SHORT}'])
        self.stop_node(0)
        self.start_node(0, extra_args=self.extra_args[0])

    def run_test(self):
        node = self.nodes[0]
        self.log.info("The regtest schedule refuses depths and heights that would break the rule")
        self.check_rejected_schedules()

        self.wallet = MiniWallet(node)
        node.setmocktime(BASE_TIME)
        self.mine_to(300)

        # Heights 199 and 200 straddle the flag day, so at this tip the two
        # outputs are one block apart in age and differ only in which rule
        # covers them.
        self.log.info("An output mined before the flag day keeps the ordinary maturity")
        tx = self.spend(START - 1, expect_depth=102)
        node.sendrawtransaction(tx['hex'])
        assert tx['txid'] in node.getrawmempool()
        block = self.block_with(tx['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), None)
        self.wallet.rescan_utxos()

        self.log.info("...while the very next output is held to the extended one")
        assert_raises_rpc_error(-26, PREMATURE, node.sendrawtransaction, self.spend(START, expect_depth=102)['hex'])
        block = self.block_with(self.spend(START, expect_depth=102)['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), PREMATURE)
        assert_equal(node.getblockcount(), 301)

        self.log.info("One block short of the extended depth is still rejected")
        self.mine_to(START + LONG - 2)
        assert_equal(node.getblockcount(), 348)
        block = self.block_with(self.spend(START, expect_depth=LONG - 1)['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), PREMATURE)
        assert_equal(node.getblockcount(), 348)

        self.log.info("...and accepted at exactly the extended depth")
        self.mine_to(START + LONG - 1)
        tx = self.spend(START, expect_depth=LONG)
        node.sendrawtransaction(tx['hex'])
        block = self.block_with(tx['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), None)
        self.wallet.rescan_utxos()

        self.log.info("Templates exclude a covered output the next block would reject")
        premature = self.spend(START + 2, expect_depth=LONG - 1)
        assert_raises_rpc_error(-26, PREMATURE, node.sendrawtransaction, premature['hex'])
        template = node.getblocktemplate({'rules': ['segwit', 'blake2b']})
        assert premature['txid'] not in [entry['txid'] for entry in template['transactions']]

        self.log.info("A covered output inside the period is rejected before the expiry")
        young = node.getblockcount() + 1 - SHORT
        assert young >= START, 'setup: needs a covered output at the ordinary depth'
        assert_raises_rpc_error(-26, PREMATURE, node.sendrawtransaction, self.spend(young, expect_depth=SHORT)['hex'])

        self.log.info("Once RDTS expires the ordinary maturity is all that remains")
        node.setmocktime(EXPIRY_TIME)
        while self.mtp() < EXPIRY_TIME:
            self.generate(self.wallet, 1)
        self.wallet.rescan_utxos()
        covered = node.getblockcount() + 1 - SHORT
        assert covered >= START
        tx = self.spend(covered, expect_depth=SHORT)
        node.sendrawtransaction(tx['hex'])
        block = self.block_with(tx['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), None)
        self.wallet.rescan_utxos()

        self.log.info("A reorg back inside the window re-tightens and evicts the spend")
        expired_tip = node.getbestblockhash()
        stale = self.spend(node.getblockcount() + 1 - (SHORT + 20), expect_depth=SHORT + 20)
        node.sendrawtransaction(stale['hex'])
        while self.mtp() >= EXPIRY_TIME:
            node.invalidateblock(node.getbestblockhash())
        assert stale['txid'] not in node.getrawmempool(), 'stale spend survived the reorg'
        assert_raises_rpc_error(-26, PREMATURE, node.sendrawtransaction, stale['hex'])
        template = node.getblocktemplate({'rules': ['segwit', 'blake2b']})
        assert stale['txid'] not in [entry['txid'] for entry in template['transactions']]
        node.reconsiderblock(expired_tip)
        assert_equal(node.getbestblockhash(), expired_tip)
        node.sendrawtransaction(stale['hex'])
        assert stale['txid'] in node.getrawmempool()


if __name__ == '__main__':
    CoinbaseMaturityLongTest(__file__).main()
