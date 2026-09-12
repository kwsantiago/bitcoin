#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the coinbase spend freeze taking effect through a reorg.

A reorg can cross the start of the window inside a single chain activation:
blocks are disconnected and the replacements connected before the mempool is
made consistent again. The freeze must still take hold, and the node must
survive it.
"""

from test_framework.blocktools import (
    add_witness_commitment,
    create_block,
    create_coinbase,
)
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal
from test_framework.wallet import MiniWallet

BASE_TIME = 1600000000
FREEZE_START = BASE_TIME + 1000
EXPIRY_TIME = BASE_TIME + 100000
BLAKE2B_HEIGHT = 10000000


class CoinbaseSpendFreezeReorgTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}',
            f'-rdtsexpiry={EXPIRY_TIME}',
            f'-coinbasefreezestart={FREEZE_START}',
            '-checkmempool=1',
        ]]

    def build_on(self, prev_hash, height, ntime, marker):
        block = create_block(int(prev_hash, 16), create_coinbase(height, extra_output_script=marker),
                             ntime=ntime, height=height)
        add_witness_commitment(block)
        block.solve()
        return block

    def run_test(self):
        node = self.nodes[0]
        wallet = MiniWallet(node)
        node.setmocktime(BASE_TIME)
        self.generate(wallet, 200)

        # A confirmed, non-coinbase output to anchor a mempool entry's
        # lock points to the block that the reorg will disconnect.
        funding = wallet.create_self_transfer()
        node.sendrawtransaction(funding['hex'])

        node.setmocktime(FREEZE_START)
        # Four blocks stamped at the flag day, then a fifth carrying the
        # funding transaction. The median-time-past is still below the flag
        # day, so the freeze has not taken hold yet.
        for _ in range(4):
            self.generateblock(node, output=wallet.get_descriptor(), transactions=[])
        self.generateblock(node, output=wallet.get_descriptor(), transactions=[funding['hex']])
        wallet.rescan_utxos()
        assert node.getblockchaininfo()['mediantime'] < FREEZE_START
        fork_point = node.getblockheader(node.getbestblockhash())['previousblockhash']
        doomed_tip = node.getbestblockhash()
        assert_equal(node.getblock(doomed_tip)['height'], 205)

        # Two mempool entries that the reorg must deal with: one spending the
        # output confirmed in the block about to be disconnected, and one
        # spending a coinbase output, which the freeze forbids.
        anchored = wallet.create_self_transfer(utxo_to_spend=funding['new_utxo'])
        node.sendrawtransaction(anchored['hex'])
        coinbase_spend = wallet.create_self_transfer(utxo_to_spend=self.oldest_coinbase_utxo(wallet))
        node.sendrawtransaction(coinbase_spend['hex'])
        assert coinbase_spend['txid'] in node.getrawmempool()

        # A competing branch of two blocks, both stamped at the flag day, so
        # connecting it pushes the median-time-past to the flag day and the
        # freeze takes effect in the middle of the reorg.
        self.log.info("Reorging across the start of the window")
        first = self.build_on(fork_point, 205, FREEZE_START, b'\x51')
        assert_equal(node.submitblock(first.serialize().hex()), 'inconclusive')
        second = self.build_on(first.hash, 206, FREEZE_START, b'\x51')
        assert_equal(node.submitblock(second.serialize().hex()), None)

        assert_equal(node.getbestblockhash(), second.hash)
        assert node.getblockchaininfo()['mediantime'] >= FREEZE_START
        self.log.info("Node survived the reorg and the freeze is in force")
        assert coinbase_spend['txid'] not in node.getrawmempool()
        assert_equal(node.getblockcount(), 206)

    def oldest_coinbase_utxo(self, wallet):
        coins = [u for u in wallet.get_utxos(include_immature_coinbase=True, mark_as_spent=False) if u['coinbase']]
        assert coins, 'no unspent coinbase output'
        return min(coins, key=lambda u: u['height'])


if __name__ == '__main__':
    CoinbaseSpendFreezeReorgTest(__file__).main()
