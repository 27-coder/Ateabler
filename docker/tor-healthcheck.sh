#!/bin/sh
set -eu

# A listening SOCKS port is not enough: tor-resolve succeeds only after Tor
# has bootstrapped a usable circuit and resolved the hostname through it.
resolved="$(
  tor-resolve check.torproject.org 127.0.0.1:9050 2>/dev/null
)"

[ -n "${resolved}" ]
