# autoupnp

A small Python 3 tool for UPnP IGD discovery and port forwarding from the
command line, designed to work on hosts where stock UPnP clients fail.

## Why

The standard discovery flow used by clients like `upnpc` sends an SSDP
`M-SEARCH` to the multicast address `239.255.255.250:1900`. Many home
routers do not relay SSDP multicast on their wired LAN ports, so the
client never hears a response — even though the router's UPnP service
is running and reachable. `nmap -sU --script=upnp-info` works around
that, but it crafts raw UDP packets and requires root.

`autoupnp` sidesteps both problems by sending a **unicast** M-SEARCH
straight to the router's IP, talking SOAP directly, and never asking
for root unless you explicitly invoke the `scan` fallback. The
randomized per-boot `rootDesc.xml` URL that some routers hand out is
cached next to the script so subsequent runs skip discovery entirely.

## Requirements

- Python 3.10+
- Standard library only — no `pip install` step
- Linux (uses `ip route` to find the default gateway)

## Quick start

```sh
./upnp.py forward          # apply the default 17-port preset
./upnp.py status           # show external IP and existing mappings
./upnp.py unforward 60010-60090 30030   # remove the preset
```

## Subcommands

| Command     | What it does                                                             | Needs sudo? |
| ----------- | ------------------------------------------------------------------------ | ----------- |
| `discover`  | Unicast SSDP M-SEARCH → print rootDesc URL + device info, populate cache | no          |
| `scan`      | `nmap -sU -p 1900 --script=upnp-info` fallback (re-execs with sudo)      | yes         |
| `status`    | External IP + current port mappings (`--json` available)                 | no          |
| `forward`   | Add port mappings (`AddPortMapping`)                                     | no          |
| `unforward` | Remove port mappings (`DeletePortMapping`)                               | no          |

All subcommands except `discover` and `scan` accept the common options
`-u/--url`, `--host`, `--no-cache`, `--timeout`, `--json`, and `-v`.

## Port spec syntax

`forward` and `unforward` accept any mix of:

- `60010` — single port, protocol from `--proto` (default `both`)
- `60010/tcp`, `60010/udp`, `60010/both` — explicit protocol override
- `60010-60015` — inclusive range
- `60010-60015/udp` — range with explicit protocol

Multiple specs can be passed positionally:

```sh
./upnp.py forward 60010/tcp 60020-60022 30030/both
```

## Default preset

`./upnp.py forward` with no arguments applies the built-in `default`
preset — 17 external ports × TCP and UDP = 34 mappings. The list lives
in the `PRESETS` dict at the top of `upnp.py`; add your own presets by
editing it.

## Cache

`upnp.py` writes `.upnp-cache.json` next to itself with mode `0600`. It
stores the discovered `rootDesc` URL, control URL, and service type per
host for 24 hours. Use `--no-cache` to bypass it, or just delete the
file. Cached calls that return 404 or `Invalid Action` auto-invalidate
and re-discover.

## Output formats

Every read/write subcommand has a human-readable default and a
`--json` variant suitable for scripted consumption.

```sh
./upnp.py status --json | jq '.mappings[] | select(.protocol=="TCP")'
```

## Testing

```sh
python -m unittest test_upnp -v
```

27 unit tests cover the pure layers: port spec parsing, SSDP response
parsing, rootDesc XML parsing, SOAP envelope building, SOAP response
and fault parsing, and the cache. One integration test that performs a
real SSDP query is gated on an environment variable:

```sh
UPNP_TEST_HOST=192.168.1.1 python -m unittest test_upnp.TestSSDPNetwork
```

## Roadmap

- A cron-driven wrapper for nested-NAT hosts that maintains forwards on
  both the inner and outer routers, re-applying them when either side
  flaps. Designed in a follow-up spec.

## Design notes

See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the implementation plan that was used to
build this tool.

## License

MIT — see `LICENSE`.
