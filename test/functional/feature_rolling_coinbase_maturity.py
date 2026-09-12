#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Knots developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the rolling generation maturity.

Every coinbase output matures on its own schedule, a fixed period after the
block that created it, so nothing unlocks in a batch. Only outputs mined at or
after the activation height are covered, so the flag day does not immobilize
coins that were already spendable. The deployment expires with RDTS.

The BLAKE2b activation height is set out of reach, so RDTS itself is never
active here and only the rolling maturity is under test.
"""

from test_framework.blocktools import (
    COINBASE_MATURITY,
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
MATURITY_SECONDS = 3600
ACTIVATION_HEIGHT = 200
EXPIRY_TIME = BASE_TIME + 100000
BLAKE2B_HEIGHT = 10000000

IMMATURE = 'bad-txns-coinbase-immature-time'


class RollingCoinbaseMaturityTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            f'-testactivationheight=blake2b@{BLAKE2B_HEIGHT}',
            f'-rdtsexpiry={EXPIRY_TIME}',
            f'-coinbasematurityrolling={ACTIVATION_HEIGHT}:{MATURITY_SECONDS}',
        ]]

    def mtp(self, blockhash=None):
        node = self.nodes[0]
        return node.getblockheader(blockhash or node.getbestblockhash())['mediantime']

    def matures_at(self, height):
        """The instant the coinbase created at `height` becomes spendable. The
        anchor is the block before it, so its own miner contributes no
        timestamp to the median."""
        return self.mtp(self.nodes[0].getblockhash(max(height - 1, 0))) + MATURITY_SECONDS

    def is_mature(self, height):
        """Whether it is spendable in the next block, by the node's own values."""
        return self.mtp() >= self.matures_at(height)

    def mine_empty(self, count=1):
        for _ in range(count):
            self.generateblock(self.nodes[0], output=self.wallet.get_descriptor(), transactions=[])
        self.wallet.rescan_utxos()

    def advance_mtp_to(self, when):
        """Six blocks stamped at `when` put the median-time-past there."""
        self.nodes[0].setmocktime(when)
        self.mine_empty(6)
        assert self.mtp() >= when, f'median-time-past {self.mtp()} did not reach {when}'

    def coinbase_utxo(self, height):
        for utxo in self.wallet.get_utxos(include_immature_coinbase=True, mark_as_spent=False):
            if utxo['coinbase'] and utxo['height'] == height:
                return utxo
        raise AssertionError(f'no unspent coinbase output at height {height}')

    def spend(self, height):
        return self.wallet.create_self_transfer(utxo_to_spend=self.coinbase_utxo(height))

    def block_with(self, tx):
        node = self.nodes[0]
        tip = node.getbestblockhash()
        height = node.getblockcount() + 1
        block = create_block(int(tip, 16), create_coinbase(height),
                             ntime=max(node.getblockheader(tip)['time'] + 1, self.mtp() + 1),
                             txlist=[tx], height=height)
        add_witness_commitment(block)
        block.solve()
        return block

    def newest_buried(self, *, mature):
        """The newest covered coinbase that is buried past the depth rule and
        either is or is not yet past its rolling period."""
        node = self.nodes[0]
        for height in range(node.getblockcount() + 1 - COINBASE_MATURITY, ACTIVATION_HEIGHT - 1, -1):
            if self._unspent(height) and self.is_mature(height) == mature:
                return height
        raise AssertionError(f'no buried covered coinbase with mature={mature}')

    def run_test(self):
        node = self.nodes[0]
        self.wallet = MiniWallet(node)
        # Two stages half a maturity period apart, so outputs from each have
        # visibly different maturity instants.
        node.setmocktime(BASE_TIME)
        self.generate(self.wallet, 150)
        node.setmocktime(BASE_TIME + MATURITY_SECONDS // 2)
        self.generate(self.wallet, 160)
        assert_equal(node.getblockcount(), 310)

        self.log.info("An output mined before the flag day is exempt, however recent")
        assert not self.is_mature(100), "setup: coinbase 100 is well inside a rolling period"
        assert 100 < ACTIVATION_HEIGHT
        tx = self.spend(100)
        node.sendrawtransaction(tx['hex'])
        assert tx['txid'] in node.getrawmempool()
        self.generate(self.wallet, 1)

        self.log.info("An output mined at or after the flag day is held to the period")
        covered = self.newest_buried(mature=False)
        assert covered >= ACTIVATION_HEIGHT
        assert_raises_rpc_error(-26, IMMATURE, node.sendrawtransaction, self.spend(covered)['hex'])

        self.log.info("A block containing one is invalid, not merely unrelayed")
        block = self.block_with(self.spend(covered)['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), IMMATURE)
        assert_equal(node.getblockcount(), 311)

        self.log.info("Outputs mature one at a time, not in a batch")
        # Bury the rest of the covered outputs so the pair below are both
        # spendable as far as the depth rule is concerned.
        self.mine_empty(110)
        early = ACTIVATION_HEIGHT
        late = max(h for h in range(ACTIVATION_HEIGHT, node.getblockcount() + 1 - COINBASE_MATURITY)
                   if self._unspent(h))
        assert self.matures_at(late) > self.matures_at(early), "setup: needs distinct maturity instants"
        self.advance_mtp_to(self.matures_at(early))
        assert self.is_mature(early) and not self.is_mature(late)
        node.sendrawtransaction(self.spend(early)['hex'])
        assert_raises_rpc_error(-26, IMMATURE, node.sendrawtransaction, self.spend(late)['hex'])
        self.generate(self.wallet, 1)

        self.log.info("The later output relays once its own period has passed")
        self.advance_mtp_to(self.matures_at(late))
        assert self.is_mature(late)
        node.sendrawtransaction(self.spend(late)['hex'])
        self.generate(self.wallet, 1)

        self.log.info("A matured output is accepted in a block")
        block = self.block_with(self.spend(self.newest_buried(mature=True))['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), None)
        self.wallet.rescan_utxos()

        self.log.info("The rule still holds for outputs still inside their period")
        block = self.block_with(self.spend(self.newest_buried(mature=False))['tx'])
        assert_equal(node.submitblock(block.serialize().hex()), IMMATURE)

        self.log.info("A reorg that un-matures an output evicts the spend from the mempool")
        # MaybeUpdateMempoolForReorg is the one path where the rolling check
        # runs ahead of the depth loop, so exercise it directly.
        fresh = self.newest_buried(mature=True)
        tx = self.spend(fresh)
        node.sendrawtransaction(tx['hex'])
        assert tx['txid'] in node.getrawmempool()
        # Roll the tip back so the median-time-past falls below that output's
        # own maturity instant again.
        target = node.getblockhash(node.getblockcount())
        while self.mtp() >= self.matures_at(fresh):
            node.invalidateblock(node.getbestblockhash())
        assert tx['txid'] not in node.getrawmempool(), "stale spend survived the reorg"
        assert_raises_rpc_error(-26, IMMATURE, node.sendrawtransaction, tx['hex'])
        # Relay and consensus evaluate the same predicate, so the mempool never
        # holds a spend the next block would reject and the assembler needs no
        # rule of its own. Templates must keep building through the boundary.
        template = node.getblocktemplate({'rules': ['segwit']})
        assert all(entry['txid'] != tx['txid'] for entry in template['transactions'])
        node.reconsiderblock(target)
        assert self.is_mature(fresh)
        node.sendrawtransaction(tx['hex'])
        self.generate(self.wallet, 1)

        self.log.info("Once the deployment expires, only the ordinary depth rule remains")
        self.advance_mtp_to(EXPIRY_TIME)
        self.mine_empty(COINBASE_MATURITY + 1)
        young = self.newest_buried(mature=False)
        tx = self.spend(young)
        node.sendrawtransaction(tx['hex'])
        block_hash = self.generate(self.wallet, 1)[0]
        assert tx['txid'] in node.getblock(block_hash)['tx']

    def _unspent(self, height):
        try:
            self.coinbase_utxo(height)
            return True
        except AssertionError:
            return False


if __name__ == '__main__':
    RollingCoinbaseMaturityTest(__file__).main()
