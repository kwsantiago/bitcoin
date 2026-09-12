#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the temporary coinbase spend freeze.

While the freeze is in force no transaction may spend a coinbase output at any
depth. The window opens on the first block whose parent median-time-past has
reached the flag day and closes at the RDTS expiry, after which the ordinary
COINBASE_MATURITY rule resumes.

The BLAKE2b activation height is set out of reach, so RDTS itself is never
active here and only the freeze is under test.
"""

from test_framework.blocktools import (
    add_witness_commitment,
    create_block,
    create_coinbase,
)
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    assert_raises_rpc_error,
)
from test_framework.wallet import MiniWallet

BASE_TIME = 1600000000
FREEZE_START = BASE_TIME + 1000
EXPIRY_TIME = BASE_TIME + 100000
BLAKE2B_HEIGHT = 10000000

FROZEN_REJECT = 'bad-txns-coinbase-spend-frozen'


class CoinbaseSpendFreezeTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}',
            f'-rdtsexpiry={EXPIRY_TIME}',
            f'-coinbasefreezestart={FREEZE_START}',
        ]]

    def freeze_active_for_next_block(self):
        mtp = self.nodes[0].getblockchaininfo()['mediantime']
        return FREEZE_START <= mtp < EXPIRY_TIME

    def mine_empty(self, count=1):
        """Mine blocks that do not draw on the mempool."""
        for _ in range(count):
            self.generateblock(self.nodes[0], output=self.wallet.get_descriptor(), transactions=[])
        self.wallet.rescan_utxos()

    def coinbase_utxo(self, height):
        for utxo in self.wallet.get_utxos(include_immature_coinbase=True, mark_as_spent=False):
            if utxo['coinbase'] and utxo['height'] == height:
                return utxo
        raise AssertionError(f'no unspent coinbase output at height {height}')

    def coinbase_spend_at_depth(self, depth):
        """A transaction spending the coinbase that would be `depth` deep in the next block."""
        height = self.nodes[0].getblockcount() + 1 - depth
        return self.wallet.create_self_transfer(utxo_to_spend=self.coinbase_utxo(height))

    def spends_coinbase(self, node, txid):
        entry = node.getrawtransaction(txid, True)
        for vin in entry['vin']:
            parent = node.getrawtransaction(vin['txid'], True)
            if 'coinbase' in parent['vin'][0]:
                return True
        return False

    def block_with(self, tx):
        node = self.nodes[0]
        tip = node.getbestblockhash()
        height = node.getblockcount() + 1
        block = create_block(int(tip, 16), create_coinbase(height),
                             ntime=node.getblockheader(tip)['time'] + 1,
                             txlist=[tx], height=height)
        add_witness_commitment(block)
        block.solve()
        return block

    def run_test(self):
        node = self.nodes[0]
        self.wallet = MiniWallet(node)
        node.setmocktime(BASE_TIME)
        self.generate(self.wallet, 200)

        self.log.info("Before the flag day a matured coinbase output is spendable")
        assert_equal(self.freeze_active_for_next_block(), False)
        spend = self.coinbase_spend_at_depth(150)
        node.sendrawtransaction(spend['hex'])
        assert spend['txid'] in node.getrawmempool()
        block_hash = self.generate(self.wallet, 1)[0]
        assert spend['txid'] in node.getblock(block_hash)['tx']
        non_coinbase_utxo = spend['new_utxo']

        self.log.info("A coinbase spend already in the mempool is evicted when the freeze takes effect")
        stale = self.coinbase_spend_at_depth(150)
        node.sendrawtransaction(stale['hex'])
        node.setmocktime(FREEZE_START)
        # Five blocks stamped at the flag day leave the median-time-past below
        # it: the sixth is the first block that reaches it.
        self.mine_empty(5)
        assert_equal(self.freeze_active_for_next_block(), False)
        assert stale['txid'] in node.getrawmempool()
        self.mine_empty(1)
        assert_equal(self.freeze_active_for_next_block(), True)
        assert stale['txid'] not in node.getrawmempool()

        self.log.info("During the window a coinbase output is unspendable at any depth")
        for depth in (50, 100, 150):
            tx = self.coinbase_spend_at_depth(depth)
            assert_raises_rpc_error(-26, FROZEN_REJECT, node.sendrawtransaction, tx['hex'])
        self.generate(self.wallet, 1000)
        assert node.getblockcount() > 1000
        tx = self.wallet.create_self_transfer(utxo_to_spend=self.coinbase_utxo(1))
        assert_raises_rpc_error(-26, FROZEN_REJECT, node.sendrawtransaction, tx['hex'])

        self.log.info("getdeploymentinfo reports the window as in force")
        info = node.getdeploymentinfo()['deployments']['coinbase_spend_freeze']
        assert_equal(info['type'], 'flagday')
        assert_equal(info['start_time'], FREEZE_START)
        assert_equal(info['expiry_time'], EXPIRY_TIME)
        assert_equal(info['active'], True)

        self.log.info("Block templates built during the window carry no coinbase spend")
        template = node.getblocktemplate({'rules': ['segwit']})
        assert all(not self.spends_coinbase(node, entry['txid']) for entry in template['transactions'])

        self.log.info("A node without the freeze scheduled accepts the same spend, and it does not survive a restart into the window")
        unscheduled_args = [a for a in self.extra_args[0] if not a.startswith('-coinbasefreezestart')]
        self.restart_node(0, extra_args=unscheduled_args + [f'-mocktime={FREEZE_START}'])
        assert 'coinbase_spend_freeze' not in node.getdeploymentinfo()['deployments']
        persisted = self.coinbase_spend_at_depth(150)
        node.sendrawtransaction(persisted['hex'])
        assert persisted['txid'] in node.getrawmempool()
        self.restart_node(0, extra_args=self.extra_args[0] + [f'-mocktime={FREEZE_START}'])
        assert_equal(node.getdeploymentinfo()['deployments']['coinbase_spend_freeze']['active'], True)
        assert persisted['txid'] not in node.getrawmempool()

        self.log.info("Spending a non-coinbase output is unaffected")
        ordinary = self.wallet.create_self_transfer(utxo_to_spend=non_coinbase_utxo)
        node.sendrawtransaction(ordinary['hex'])
        self.generate(self.wallet, 1)

        self.log.info("A block containing a coinbase spend is invalid, not merely non-standard")
        tip = node.getbestblockhash()
        block = self.block_with(self.coinbase_spend_at_depth(150)['tx'])
        assert_equal(node.getblocktemplate({'mode': 'proposal', 'data': block.serialize().hex(), 'rules': ['segwit']}), FROZEN_REJECT)
        assert_equal(node.submitblock(block.serialize().hex()), FROZEN_REJECT)
        assert_equal(node.getbestblockhash(), tip)

        self.log.info("The freeze holds through the last block of the window")
        node.setmocktime(EXPIRY_TIME)
        self.mine_empty(5)
        assert_equal(self.freeze_active_for_next_block(), True)
        tx = self.coinbase_spend_at_depth(150)
        assert_raises_rpc_error(-26, FROZEN_REJECT, node.sendrawtransaction, tx['hex'])

        self.log.info("Coinbase outputs frozen during the window are spendable once it expires")
        self.mine_empty(1)
        crossing_block = node.getbestblockhash()
        assert_equal(self.freeze_active_for_next_block(), False)
        released = self.wallet.create_self_transfer(utxo_to_spend=self.coinbase_utxo(1))
        node.sendrawtransaction(released['hex'])
        assert released['txid'] in node.getrawmempool()

        # The eviction hook is edge-triggered on the freeze becoming active. Past
        # expiry it must never fire, so a coinbase spend already in the mempool
        # survives an ordinary block.
        self.mine_empty(1)
        assert released['txid'] in node.getrawmempool()
        post_expiry_tip = node.getbestblockhash()

        self.log.info("A reorg back across the expiry re-freezes and evicts the mempool")
        node.invalidateblock(crossing_block)
        assert_equal(self.freeze_active_for_next_block(), True)
        assert released['txid'] not in node.getrawmempool()
        assert_raises_rpc_error(-26, FROZEN_REJECT, node.sendrawtransaction, released['hex'])
        node.reconsiderblock(crossing_block)
        assert_equal(node.getbestblockhash(), post_expiry_tip)
        assert_equal(self.freeze_active_for_next_block(), False)
        node.sendrawtransaction(released['hex'])
        block_hash = self.generate(self.wallet, 1)[0]
        assert released['txid'] in node.getblock(block_hash)['tx']


if __name__ == '__main__':
    CoinbaseSpendFreezeTest(__file__).main()
