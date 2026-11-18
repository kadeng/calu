"""Tests that need real root: calu's privileged paths as they run in production (D206-D228).

Every other layer uses the tests' own uid as a stand-in for root. These run only as root and
only when `CALU_ROOT_TESTS=1` is set, which the root box does (`uitest/rootbox`,
`docker exec calu-rootbox rootbox-tests`): they create directories below `/var/lib/calu` and
`/run/calu` and start programs as another uid, which no developer machine should have done to
it by a test run.
"""
