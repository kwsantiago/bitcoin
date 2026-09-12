// Copyright (c) 2026 The Bitcoin Knots developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <chain.h>
#include <chainparams.h>
#include <coins.h>
#include <consensus/params.h>
#include <consensus/validation.h>
#include <primitives/transaction.h>
#include <test/util/setup_common.h>
#include <util/chaintype.h>
#include <validation.h>

#include <boost/test/unit_test.hpp>

#include <limits>

BOOST_FIXTURE_TEST_SUITE(rolling_coinbase_maturity_tests, BasicTestingSetup)

// Consensus enforces it from the activation height until the RDTS expiry.
BOOST_AUTO_TEST_CASE(consensus_window_boundaries)
{
    constexpr int H{1000};
    constexpr int64_t E{2'000'000'000};
    Consensus::Params params;
    params.CoinbaseMaturityRollingHeight = H;
    params.RdtsExpiryTime = E;

    for (const int height : {H - 1, H, H + 1}) {
        for (const int64_t mtp : {E - 1, E, E + 1}) {
            BOOST_CHECK_EQUAL(params.RollingCoinbaseMaturityActiveAt(height, mtp), height >= H && mtp < E);
        }
    }
}

// A coinbase mined before the flag day keeps the ordinary depth rule, so a
// subject output only exists above the activation height, where consensus is
// always in force by the time it could be spent.
BOOST_AUTO_TEST_CASE(only_outputs_from_the_flag_day_are_covered)
{
    constexpr int H{1000};
    constexpr int64_t E{2'000'000'000};
    Consensus::Params params;
    params.CoinbaseMaturityRollingHeight = H;
    params.RdtsExpiryTime = E;

    // A covered output is created at height >= H, so the earliest block that
    // could spend it is above H, where the rule is enforced.
    for (const int coin_height : {H, H + 1, H + 1000}) {
        BOOST_CHECK(params.RollingCoinbaseMaturityActiveAt(coin_height + 1, E - 1));
    }
}

// Unscheduled is the default, and then nothing anywhere changes.
BOOST_AUTO_TEST_CASE(unscheduled_is_inert)
{
    const Consensus::Params defaults{};
    BOOST_CHECK(!defaults.IsRollingCoinbaseMaturityScheduled());
    BOOST_CHECK(!defaults.RollingCoinbaseMaturityActiveAt(std::numeric_limits<int>::max(), 0));

    for (const auto chain : {ChainType::MAIN, ChainType::TESTNET, ChainType::TESTNET4, ChainType::SIGNET, ChainType::REGTEST}) {
        const auto params{CreateChainParams(*m_node.args, chain)};
        const Consensus::Params& consensus{params->GetConsensus()};
        BOOST_CHECK(!consensus.IsRollingCoinbaseMaturityScheduled());
        BOOST_CHECK(!consensus.RollingCoinbaseMaturityActiveAt(std::numeric_limits<int>::max(),
                                                               std::numeric_limits<uint32_t>::max()));
    }
}

namespace {
//! A chain whose block times rise by one second a block, so every
//! median-time-past is known exactly: MTP(i) == t0 + i - 5 for i >= 10.
std::vector<CBlockIndex> SyntheticChain(int length, int64_t t0)
{
    std::vector<CBlockIndex> chain(length);
    for (int i = 0; i < length; ++i) {
        chain[i].nHeight = i;
        chain[i].pprev = i ? &chain[i - 1] : nullptr;
        chain[i].nTime = static_cast<uint32_t>(t0 + i);
        chain[i].BuildSkip();
    }
    return chain;
}

void AddCoinbaseAt(CCoinsViewCache& coins, const std::vector<COutPoint>& outpoints, int height)
{
    for (const COutPoint& op : outpoints) {
        coins.AddCoin(op, Coin{CTxOut{50 * COIN, CScript()}, height, /*fCoinBaseIn=*/true}, false);
    }
}

CTransaction SpendingAll(const std::vector<COutPoint>& outpoints)
{
    CMutableTransaction tx;
    for (const COutPoint& op : outpoints) tx.vin.emplace_back(op);
    tx.vout.emplace_back(CTxOut{40 * COIN, CScript()});
    return CTransaction{tx};
}
} // namespace

// The rule itself: the boundary lands exactly where the anchor plus the period
// meets the spending block's parent median-time-past.
BOOST_AUTO_TEST_CASE(check_rolling_maturity_boundary)
{
    constexpr int64_t T0{1'000'000};
    constexpr int COIN_HEIGHT{100};
    constexpr int64_t SECONDS{50};
    // Anchor is MTP(COIN_HEIGHT - 1) == T0 + 94, so it matures at T0 + 144,
    // which is MTP(149).
    auto chain{SyntheticChain(200, T0)};
    BOOST_CHECK_EQUAL(chain[COIN_HEIGHT - 1].GetMedianTimePast(), T0 + 94);
    BOOST_CHECK_EQUAL(chain[149].GetMedianTimePast(), T0 + 144);

    Consensus::Params params;
    params.CoinbaseMaturityRollingHeight = 0;
    params.CoinbaseMaturitySeconds = SECONDS;

    const std::vector<COutPoint> outpoints{COutPoint{Txid::FromUint256(uint256{7}), 0}};
    const CTransaction tx{SpendingAll(outpoints)};
    CCoinsView backend;

    LOCK(cs_main);
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, outpoints, COIN_HEIGHT);
        TxValidationState state;
        BOOST_CHECK(!CheckRollingCoinbaseMaturity(tx, state, coins, chain[148], params));
        BOOST_CHECK_EQUAL(state.GetRejectReason(), "bad-txns-coinbase-immature-time");
    }
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, outpoints, COIN_HEIGHT);
        TxValidationState state;
        BOOST_CHECK(CheckRollingCoinbaseMaturity(tx, state, coins, chain[149], params));
    }
}

// An output mined before the flag day is exempt however recent it is.
BOOST_AUTO_TEST_CASE(check_rolling_maturity_grandfathers)
{
    constexpr int64_t T0{1'000'000};
    constexpr int COIN_HEIGHT{100};
    auto chain{SyntheticChain(200, T0)};

    Consensus::Params params;
    params.CoinbaseMaturitySeconds = 50;

    const std::vector<COutPoint> outpoints{COutPoint{Txid::FromUint256(uint256{7}), 0}};
    const CTransaction tx{SpendingAll(outpoints)};
    CCoinsView backend;

    LOCK(cs_main);
    params.CoinbaseMaturityRollingHeight = COIN_HEIGHT + 1;
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, outpoints, COIN_HEIGHT);
        TxValidationState state;
        BOOST_CHECK(CheckRollingCoinbaseMaturity(tx, state, coins, chain[148], params));
    }
    params.CoinbaseMaturityRollingHeight = COIN_HEIGHT;
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, outpoints, COIN_HEIGHT);
        TxValidationState state;
        BOOST_CHECK(!CheckRollingCoinbaseMaturity(tx, state, coins, chain[148], params));
    }
}

// Several inputs from one generation output take the same answer as one.
BOOST_AUTO_TEST_CASE(check_rolling_maturity_many_inputs_same_height)
{
    constexpr int64_t T0{1'000'000};
    constexpr int COIN_HEIGHT{100};
    auto chain{SyntheticChain(200, T0)};

    Consensus::Params params;
    params.CoinbaseMaturityRollingHeight = 0;
    params.CoinbaseMaturitySeconds = 50;

    std::vector<COutPoint> outpoints;
    for (uint32_t n = 0; n < 8; ++n) outpoints.emplace_back(Txid::FromUint256(uint256{7}), n);
    const CTransaction tx{SpendingAll(outpoints)};
    CCoinsView backend;

    LOCK(cs_main);
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, outpoints, COIN_HEIGHT);
        TxValidationState state;
        BOOST_CHECK(!CheckRollingCoinbaseMaturity(tx, state, coins, chain[148], params));
    }
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, outpoints, COIN_HEIGHT);
        TxValidationState state;
        BOOST_CHECK(CheckRollingCoinbaseMaturity(tx, state, coins, chain[149], params));
    }
}

// Inputs from generation outputs at different heights are judged on their own
// anchors, so the per-height shortcut cannot leak one answer onto another.
BOOST_AUTO_TEST_CASE(check_rolling_maturity_mixed_heights)
{
    constexpr int64_t T0{1'000'000};
    constexpr int64_t SECONDS{50};
    auto chain{SyntheticChain(200, T0)};

    Consensus::Params params;
    params.CoinbaseMaturityRollingHeight = 0;
    params.CoinbaseMaturitySeconds = SECONDS;

    // Anchors are MTP(99) == T0+94 and MTP(109) == T0+104, so at chain[149]
    // (MTP == T0+144) the first has matured and the second has not.
    const COutPoint old_op{Txid::FromUint256(uint256{7}), 0};
    const COutPoint new_op{Txid::FromUint256(uint256{8}), 0};
    CMutableTransaction mtx;
    mtx.vin.emplace_back(old_op);
    mtx.vin.emplace_back(new_op);
    mtx.vout.emplace_back(CTxOut{40 * COIN, CScript()});
    const CTransaction tx{mtx};
    CCoinsView backend;

    LOCK(cs_main);
    {
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, {old_op}, 100);
        AddCoinbaseAt(coins, {new_op}, 110);
        TxValidationState state;
        BOOST_CHECK(!CheckRollingCoinbaseMaturity(tx, state, coins, chain[149], params));
        BOOST_CHECK_EQUAL(state.GetRejectReason(), "bad-txns-coinbase-immature-time");
    }
    {
        // Ten seconds later the second one has matured too.
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, {old_op}, 100);
        AddCoinbaseAt(coins, {new_op}, 110);
        TxValidationState state;
        BOOST_CHECK(CheckRollingCoinbaseMaturity(tx, state, coins, chain[159], params));
    }
    {
        // Order must not matter: the immature one first.
        CMutableTransaction reversed;
        reversed.vin.emplace_back(new_op);
        reversed.vin.emplace_back(old_op);
        reversed.vout.emplace_back(CTxOut{40 * COIN, CScript()});
        CCoinsViewCache coins{&backend};
        AddCoinbaseAt(coins, {old_op}, 100);
        AddCoinbaseAt(coins, {new_op}, 110);
        TxValidationState state;
        BOOST_CHECK(!CheckRollingCoinbaseMaturity(CTransaction{reversed}, state, coins, chain[149], params));
    }
}

BOOST_AUTO_TEST_SUITE_END()
