// Copyright (c) 2026 The Bitcoin Knots developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <chainparams.h>
#include <consensus/params.h>
#include <test/util/setup_common.h>
#include <util/chaintype.h>

#include <boost/test/unit_test.hpp>

#include <limits>

BOOST_FIXTURE_TEST_SUITE(coinbase_freeze_params_tests, BasicTestingSetup)

// The freeze covers exactly the blocks whose parent's median-time-past is at or
// past the start and below the RDTS expiry: pin both boundaries.
BOOST_AUTO_TEST_CASE(coinbase_freeze_active_at_boundaries)
{
    constexpr int64_t S{1'900'000'000};
    constexpr int64_t E{2'000'000'000};
    Consensus::Params params;
    params.CoinbaseFreezeStartTime = S;
    params.RdtsExpiryTime = E;

    for (const int64_t mtp : {S - 1, S, S + 1, E - 1, E, E + 1}) {
        BOOST_CHECK_EQUAL(params.CoinbaseFreezeActiveAt(mtp), mtp >= S && mtp < E);
    }
    BOOST_CHECK(!params.CoinbaseFreezeActiveAt(std::numeric_limits<int64_t>::min()));
    BOOST_CHECK(!params.CoinbaseFreezeActiveAt(std::numeric_limits<int64_t>::max()));

    // The freeze does not depend on the BLAKE2b hardfork height, only on the
    // expiry instant it shares with RDTS.
    params.Blake2bHeight = std::numeric_limits<int>::max();
    BOOST_CHECK(params.CoinbaseFreezeActiveAt(S));
}

// An empty or inverted window is inert rather than inverted in meaning.
BOOST_AUTO_TEST_CASE(coinbase_freeze_empty_window_is_inert)
{
    Consensus::Params params;
    params.CoinbaseFreezeStartTime = 2'000'000'000;
    params.RdtsExpiryTime = 1'900'000'000;
    for (const int64_t mtp : {int64_t{0}, int64_t{1'900'000'000}, int64_t{2'000'000'000}, int64_t{2'100'000'000}}) {
        BOOST_CHECK(!params.CoinbaseFreezeActiveAt(mtp));
    }
}

// The defaults must leave the rule off, whatever the chain's median-time-past.
BOOST_AUTO_TEST_CASE(coinbase_freeze_default_is_unscheduled)
{
    const Consensus::Params defaults{};
    BOOST_CHECK_EQUAL(defaults.CoinbaseFreezeStartTime, std::numeric_limits<int64_t>::max());
    for (const int64_t mtp : {std::numeric_limits<int64_t>::min(), int64_t{0}, int64_t{2'000'000'000}, std::numeric_limits<int64_t>::max()}) {
        BOOST_CHECK(!defaults.CoinbaseFreezeActiveAt(mtp));
    }

    // Every shipped chain leaves it unscheduled, so no median-time-past a real
    // block can carry (an unsigned 32-bit time) can turn it on.
    for (const auto chain : {ChainType::MAIN, ChainType::TESTNET, ChainType::TESTNET4, ChainType::SIGNET, ChainType::REGTEST}) {
        const auto params{CreateChainParams(*m_node.args, chain)};
        BOOST_CHECK_EQUAL(params->GetConsensus().CoinbaseFreezeStartTime, std::numeric_limits<int64_t>::max());
        BOOST_CHECK(!params->GetConsensus().CoinbaseFreezeActiveAt(std::numeric_limits<uint32_t>::max()));
    }
}

BOOST_AUTO_TEST_SUITE_END()
