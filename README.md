# avax-genesis-research

Tools that trace AVAX across the P, X and C chains:
- backward from an address to the genesis allocations that funded it;
- forward from genesis allocations to where the AVAX went.

The chain data comes from the Avalanche Data API (Glacier), through the generated
[glacier-client](https://github.com/tactical-retreat/glacier-client).

The repo holds the tooling only. Genesis data, API responses, trace results and reports are produced locally
under `data/` (gitignored). No results or conclusions from earlier runs are included.

## Setup

```bash
uv sync
uv run avax-fetch-genesis        # downloads the mainnet genesis, builds data/genesis/all_allocations.csv
cp glacier_api_keys.example.txt glacier_api_keys.txt   # optional, see "API keys"
```

### Genesis data

`avax-fetch-genesis` (`avax_research/cli/fetch_genesis.py`) downloads `genesis/genesis_mainnet.json` from
[ava-labs/avalanchego](https://github.com/ava-labs/avalanchego) at a pinned tag and checks it against a
pinned SHA-256. The file is 3.5 MB and hasn't changed since 2022. From it, the command builds
`data/genesis/all_allocations.csv`, one row per allocation (5,390 rows, 360M AVAX): X and P addresses,
the Ethereum address that controlled it, the initial, locked and total amount, the unlock schedule, and
whether it's an initial staker's reward address (with the node ID).

`category` describes only the unlock schedule in the genesis file, and every schedule is quarterly: `No lockup`,
`Single unlock`, and `4`, `6`, `16` or `40 quarterly unlocks`. The file doesn't say who received an
allocation, so neither does the table. `--category` in `avax-trace --mode from-genesis` takes these names.

### API keys

The tracer works without a key, but Glacier's unauthenticated rate limit is low, and a deep trace makes
thousands of requests. With keys:

- **Where they go:** `glacier_api_keys.txt` in the directory you run from, one key per line. `#` starts a
  comment, so you can note whose key it is. Or set `GLACIER_API_KEYS=key1,key2`, or point
  `GLACIER_API_KEYS_FILE` at a file elsewhere. `--api-key` on the CLI overrides both.
- **Why several:** each key has its own quota, and the client rotates through them one request at a time.
  Its pacing (`--rate-limit`, per key) scales with the number of keys.
- **Why a separate file:** keys must never be committed. `glacier_api_keys.txt` is in `.gitignore`. An
  earlier version of this code hardcoded shared keys; none of them work any more. Only use keys whose
  owners have agreed.
- **Getting a key:** https://build.avax.network/console/utilities/data-api-keys
- **An invalid key fails the request** with HTTP 400 (`Api key is invalid`), and the client doesn't fall back
  to unauthenticated. Remove dead keys from the file.

## Usage

```bash
# backward: where did this address's AVAX come from? (stops at genesis allocations)
uv run avax-trace --address P-avax1... --mode source --max-depth 10

# P/X addresses funded by the same sources
uv run avax-trace --address X-avax1... --mode related

# forward from an address to its destinations
uv run avax-trace --address P-avax1... --mode destinations --no-balances

# forward from genesis allocations (resumable with --checkpoint)
uv run avax-trace --mode from-genesis --category "40 quarterly unlocks" --strategy hybrid

# summarize the latest from-genesis CSV in data/results
uv run avax-report
```

`uv run avax-trace --help` lists every mode and option. Output goes to `data/results/` (CSV, JSON, Markdown,
Mermaid, Graphviz) and API responses are cached in `data/cache/glacier_cache.db`.

By default the tracer follows value transfers only (`BaseTx`, `ExportTx`, `ImportTx`; add
`--include-staking` for the rest), follows cross-chain export/import pairs, and treats outputs back to an
input address as change.

## Layout

```
avax_research/
├── api_keys.py               # key lookup: env, then glacier_api_keys.txt
├── clients/glacier_client.py # sync wrapper over glacier_client: pagination, 429 backoff, key rotation, cache
├── clients/address_resolver.py
├── models/address.py         # AvaxAddress (see Known issues)
├── models/graph.py           # trace graph
├── tracer/bfs_tracer.py      # backward/forward/related tracing
├── tracer/genesis_forward.py # from-genesis tracing (bfs/greedy/hybrid) with checkpoints
├── tracer/genesis_matcher.py # lookups in all_allocations.csv
├── tracer/cross_chain_linker.py
├── cache/                    # SQLite response cache, token-bucket rate limiter
├── export/                   # CSV/JSON, Markdown, Mermaid/Graphviz
└── cli/                      # avax-trace, avax-report, avax-fetch-genesis
```

## Known issues

- **C-Chain addresses are treated as re-encodings of P/X addresses.** `AvaxAddress` keeps one 20-byte value
  and prints it as `0x...`, `X-avax1...` and `P-avax1...`. P and X really do share an address. A C-Chain
  address doesn't: it's `keccak256(pubkey)[-20:]`, while P/X is `ripemd160(sha256(pubkey))`, so the `0x...`
  form of a P/X address, and the P/X form of a `0x...` address, belong to unrelated keys. Anything that
  relies on that conversion is wrong: C-Chain columns in exports, destinations shown as `0x...` that came
  from P/X addresses, and the `from_c_address` lookups in `tracer/`. Imports and exports name their real
  C-Chain addresses (`evmOutputs[].toAddress`, `evmInputs[].fromAddress`), and a signer's public key links
  its two addresses (`glacier_tools.addresses_from_public_key` in glacier-client).
- `GlacierClient` is synchronous and has its own retry and cache code. glacier-client's async `GlacierSession`
  could replace it, but the tracers are synchronous too.
- The change heuristic only compares input and output addresses. On P-Chain exports to your own address it
  can't tell change from the exported amount.
- Two unused locals, left alone and noted in `pyproject.toml`, may mark unfinished logic:
  `bfs_tracer.py` `amount` and `genesis_forward.py` `remaining_items`.

## Tests

```bash
uv run pytest    # offline: imports, key loading, the genesis table, the client's key header
```
