// Copyright (c) 2026 The Bitcoin Knots developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <chainparams.h>
#include <consensus/consensus.h>
#include <consensus/params.h>
#include <test/util/setup_common.h>
#include <util/chaintype.h>

#include <boost/test/unit_test.hpp>

#include <limits>

BOOST_FIXTURE_TEST_SUITE(coinbase_maturity_tests, BasicTestingSetup)

// The extended maturity covers outputs from the start height until RDTS
// expires, and nothing once it has.
BOOST_AUTO_TEST_CASE(long_maturity_window)
{
    constexpr int H{1000};
    constexpr int64_t E{2'000'000'000};
    Consensus::Params params;
    params.CoinbaseMaturityLongStartHeight = H;
    params.RdtsExpiryTime = E;

    BOOST_CHECK_EQUAL(params.CoinbaseMaturityLongFrom(0), H);
    BOOST_CHECK_EQUAL(params.CoinbaseMaturityLongFrom(E - 1), H);
    BOOST_CHECK_EQUAL(params.CoinbaseMaturityLongFrom(E), std::numeric_limits<int>::max());
    BOOST_CHECK_EQUAL(params.CoinbaseMaturityLongFrom(E + 1), std::numeric_limits<int>::max());
}

// Whatever the depth, it is never weaker than the ordinary rule: the regtest
// parser rejects a shorter one, so no reachable schedule can loosen maturity.
BOOST_AUTO_TEST_CASE(long_depth_is_never_weaker)
{
    Consensus::Params params;
    BOOST_CHECK(params.CoinbaseMaturityLong >= COINBASE_MATURITY);
    BOOST_CHECK_EQUAL(params.CoinbaseMaturityLong, COINBASE_MATURITY_LONG);
}

// Unscheduled is the default, and then no output is ever covered.
BOOST_AUTO_TEST_CASE(unscheduled_is_inert)
{
    const Consensus::Params defaults{};
    for (const int64_t mtp : {std::numeric_limits<int64_t>::min(), int64_t{0},
                              int64_t{2'000'000'000}, std::numeric_limits<int64_t>::max()}) {
        BOOST_CHECK_EQUAL(defaults.CoinbaseMaturityLongFrom(mtp), std::numeric_limits<int>::max());
    }

    for (const auto chain : {ChainType::MAIN, ChainType::TESTNET, ChainType::TESTNET4, ChainType::SIGNET, ChainType::REGTEST}) {
        // Hold the params alive: GetConsensus() returns a reference into them.
        const auto params{CreateChainParams(*m_node.args, chain)};
        const Consensus::Params& consensus{params->GetConsensus()};
        BOOST_CHECK_EQUAL(consensus.CoinbaseMaturityLongStartHeight, std::numeric_limits<int>::max());
        // No block's median-time-past can bring a real chain into the window.
        BOOST_CHECK_EQUAL(consensus.CoinbaseMaturityLongFrom(0), std::numeric_limits<int>::max());
        BOOST_CHECK_EQUAL(consensus.CoinbaseMaturityLongFrom(std::numeric_limits<uint32_t>::max()),
                          std::numeric_limits<int>::max());
    }
}

BOOST_AUTO_TEST_SUITE_END()
