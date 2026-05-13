# UPnP Tool — Design

## Summary

Merge `discovery.sh` and `request_upnp.sh` into a single Python 3 script,
`upnp.py`, that performs UPnP IGD discovery and port-mapping operations
without requiring root. The existing `nmap`-based discovery is preserved as
an explicit, sudo-gated escape hatch (`scan` subcommand). The default port
list from `request_upnp.sh` is preserved as a built-in preset so the
"quick action" use case remains a one-liner.

A separate cron-driven wrapper for the double-NAT setup will be written
later and is out of scope here.

## Goals

- One script replaces both existing scripts.
- No root required for normal operation. Sudo is only invoked by the
  explicit `scan` subcommand.
- Discovery uses unicast SSDP M-SEARCH (no multicast, no nmap), so it
  works on wired interfaces where the router filters SSDP multicast.
- Handles the randomized `rootDesc.xml` path by always running SSDP
  discovery before SOAP calls, with an on-disk cache to amortize the
  cost across invocations within a single router uptime.
- Supports inspecting current state (`status`), adding mappings
  (`forward`), and removing mappings (`unforward`).
- Both human-readable and `--json` output everywhere, so the future cron
  wrapper can consume structured data without re-parsing tables.

## Non-goals

- Does not implement the double-NAT traversal logic itself. That belongs
  in the cron wrapper which will call this script twice (once per
  router).
- Does not depend on `miniupnpc`/`upnpc`. SOAP is done natively.
- Does not aim to be a general-purpose UPnP control point — only the
  IGD `WANIPConnection` / `WANPPPConnection` services are targeted.
- No IPv6 / PCP / NAT-PMP support.

## File layout

```
upnp/
├── upnp.py                                 # the script
├── discovery.sh                            # kept until replacement is verified, then removed
├── request_upnp.sh                         # kept until replacement is verified, then removed
├── .upnp-cache.json                        # discovery cache (created at runtime, gitignored)
└── docs/superpowers/specs/2026-05-12-upnp-tool-design.md
```

The cache lives next to the script (`./.upnp-cache.json` relative to
`upnp.py`), not in `~/.cache`. The script resolves its own directory via
`Path(__file__).resolve().parent`.

## Dependencies

Python 3.10+, standard library only:

- `socket` — UDP for SSDP, TCP via urllib for SCPD/SOAP
- `urllib.request` — fetching rootDesc XML and posting SOAP envelopes
- `xml.etree.ElementTree` — parsing rootDesc and SOAP responses
- `argparse` — CLI dispatch
- `ipaddress`, `subprocess`, `json`, `pathlib`, `dataclasses`, `time`, `re`

No third-party packages, no `requirements.txt`.

## Internal structure

Single file, organized into clearly separated sections of module-level
functions. Each section is independently testable and could be split
into its own module later without restructuring callers.

1. **SSDP layer** — `ssdp_msearch(host, st, mx, timeout) -> list[dict]`
2. **SCPD layer** — `fetch_root_desc(url) -> RootDesc`,
   `find_wan_service(root) -> ServiceInfo`
3. **SOAP layer** — `soap_call(service, action, args) -> dict`,
   `SoapFault` exception
4. **Cache layer** — `cache_load()`, `cache_get(host)`, `cache_put(...)`,
   `cache_invalidate(host)`
5. **Gateway resolution** — `default_gateway()` parses `ip route`,
   `local_ip_for(host)` opens a connected UDP socket to find the
   outgoing source address.
6. **Subcommand handlers** — one function per subcommand, all sharing
   a `resolve_service(args)` helper that owns the cache-then-discover
   logic.

## CLI surface

```
upnp.py discover [HOST]                        — print rootDesc URL(s) + device info
upnp.py scan [HOST]                            — re-execs `sudo nmap -sU -p 1900 --script=upnp-info HOST`
upnp.py status     [common-opts] [--json]      — external IP + existing port mappings
upnp.py forward    [common-opts] [forward-opts] [PORTS...]
upnp.py unforward  [common-opts] [PORTS...]
```

Common opts (apply to `status`, `forward`, `unforward`):

```
-u, --url URL        explicit rootDesc URL; bypasses discovery and cache
    --host HOST      SSDP target; default = default gateway from `ip route`
    --no-cache       force re-discovery, ignore any cached entry
    --timeout SECS   SSDP timeout; default 3
    --json           machine-readable output
-v, --verbose        log SOAP envelopes and responses to stderr
```

`forward` opts:

```
    --proto P        tcp | udp | both     default: both
    --lease SECS     default: 0 (permanent — matches old behavior)
    --desc TEXT      default: 'upnp.sh:<hostname>'
    --internal IP    destination IP for the mapping; default = the
                     script host's source IP toward the gateway
    --preset NAME    built-in preset name; mutually exclusive with PORTS
```

### PORTS syntax

A port spec is one of:

- `60010`            — single port, takes protocol from `--proto`
- `60010/tcp`        — explicit protocol overrides `--proto` for this entry
- `60010/udp`        — same
- `60010/both`       — TCP and UDP, regardless of `--proto`
- `60010-60015`      — inclusive range, protocol from `--proto`
- `60010-60015/udp`  — range with explicit protocol
- `60010-60015/both` — range, both protocols

Multiple specs are space-separated. With `forward` and no PORTS and no
`--preset`, the script applies `--preset default`. With `unforward` and
no PORTS, the script exits 2 with `error: nothing to unforward` — there
is no implicit preset on `unforward` because removing a preset's worth
of ports is rarely what the user wants and the wrapper should always
target specific ports.

### Built-in presets

```python
PRESETS = {
    "default": [
        "60010/both", "60020/both", "60022/both", "60023/both",
        "60024/both", "60030/both", "60042/both", "60050/both",
        "60060/both", "60061/both", "60062/both", "60080/both",
        "60081/both", "60082/both", "60083/both", "60090/both",
        "30030/both",
    ],
}
```

This list is exactly the `portlist` from `request_upnp.sh` (commit
state at 2025-08-08). New presets can be added by editing this dict.

## Discovery + cache flow

Every operation that needs a `(rootDesc URL, control URL, service type)`
triple goes through `resolve_service(args)`:

1. If `--url` is given:
   - Fetch rootDesc, find WAN service, return triple. Do not touch cache.
2. Else determine `host` (`--host` or default gateway).
3. If `--no-cache` is not set and cache has a fresh entry for `host`
   (age < 24h), return cached triple.
4. Send SSDP unicast M-SEARCH to `host:1900`:
   ```
   M-SEARCH * HTTP/1.1
   HOST: <host>:1900
   MAN: "ssdp:discover"
   MX: 2
   ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1

   ```
   Listen on the same socket for up to `--timeout` seconds; collect
   responses; pick the first with a `LOCATION:` header whose host
   matches `host` (or any if `host` was a hostname).
5. On no response, retry once with `MX: 5` and double timeout, then
   exit 2 with a clear error.
6. Fetch rootDesc, find WAN service, write the triple to cache, return.

On any cached-call SOAP failure with status 404 or UPnPError code 401
("Invalid Action"), the cache entry is invalidated and `resolve_service`
is retried once. A second failure surfaces to the user.

## SOAP layer details

A single `soap_call(service, action, args)` function:

- Builds the envelope using string templates (XML is rigid enough here
  that this is simpler and safer than `ElementTree.tostring`).
- POSTs to `service.control_url` with headers:
  ```
  Content-Type: text/xml; charset="utf-8"
  SOAPAction: "<service_type>#<action>"
  ```
- Parses response. On HTTP 200, returns the `<u:{action}Response>`
  children as a dict.
- On HTTP 500, parses the SOAP fault, raises `SoapFault(code, desc)`.

Actions used by this script:

- `GetExternalIPAddress() -> {NewExternalIPAddress}`
- `GetGenericPortMappingEntry(NewPortMappingIndex) -> {NewRemoteHost,
  NewExternalPort, NewProtocol, NewInternalPort, NewInternalClient,
  NewEnabled, NewPortMappingDescription, NewLeaseDuration}`
- `AddPortMapping(NewRemoteHost="", NewExternalPort, NewProtocol,
  NewInternalPort, NewInternalClient, NewEnabled=1,
  NewPortMappingDescription, NewLeaseDuration)`
- `DeletePortMapping(NewRemoteHost="", NewExternalPort, NewProtocol)`

## Subcommand behavior

### `discover [HOST]`

Resolves `host` (arg or default gateway), runs SSDP, fetches rootDesc.
Writes to cache. Prints:

```
Host:         192.168.0.1
RootDesc URL: http://192.168.0.1:1900/fqxbs/rootDesc.xml
Friendly:     <friendlyName from rootDesc>
Manufacturer: <manufacturer>
Model:        <modelName>
Service:      urn:schemas-upnp-org:service:WANIPConnection:1
Control URL:  http://192.168.0.1:1900/.../ctl/IPConn
```

Exit 0 on success, 2 on no SSDP response, 3 on SCPD parse failure.

### `scan [HOST]`

If not effective UID 0, re-execs `sudo nmap -sU -p 1900
--script=upnp-info <host>`. If sudo is not available, prints an error
and exits 1. Otherwise runs nmap directly. Output is nmap's raw output.
This is purely an escape hatch for cases the SSDP path can't crack.

### `status`

Resolves service. Calls `GetExternalIPAddress`. Iterates
`GetGenericPortMappingEntry(i)` from i=0, stopping when the router
returns UPnPError 713 ("SpecifiedArrayIndexInvalid"). Prints:

```
Host:        192.168.0.1
External IP: 1.2.3.4

Idx  Proto  Ext     Internal              Lease    Description
0    TCP    60010   10.0.0.10:60010       0        upnp.sh:host
1    UDP    60010   10.0.0.10:60010       0        upnp.sh:host
...
```

`--json` output:

```json
{
  "host": "192.168.0.1",
  "external_ip": "1.2.3.4",
  "mappings": [
    {"index": 0, "protocol": "TCP", "external_port": 60010,
     "internal_client": "10.0.0.10", "internal_port": 60010,
     "lease_duration": 0, "enabled": true,
     "description": "upnp.sh:host", "remote_host": ""}
  ]
}
```

### `forward [PORTS...]`

Resolves service, resolves internal IP (`--internal` or local source IP
toward the host), expands port specs and `--preset` into a list of
`(external_port, protocol)` pairs, then calls `AddPortMapping` for each.

Default with no PORTS and no `--preset`: applies `--preset default`.

Per-port output (text):

```
ok    TCP   60010 -> 10.0.0.10:60010   lease=0   "upnp.sh:host"
ok    UDP   60010 -> 10.0.0.10:60010   lease=0   "upnp.sh:host"
fail  TCP   60022 -> 10.0.0.10:60022   error: 718 ConflictInMappingEntry
```

`--json` output:

```json
{
  "host": "192.168.0.1",
  "internal_client": "10.0.0.10",
  "results": [
    {"protocol": "TCP", "external_port": 60010, "internal_port": 60010,
     "status": "ok"},
    {"protocol": "TCP", "external_port": 60022, "internal_port": 60022,
     "status": "fail", "error_code": 718,
     "error_description": "ConflictInMappingEntry"}
  ]
}
```

Exit 0 if all succeed, 1 if any fail (non-fatal SOAP faults), 2+ on
network errors.

### `unforward [PORTS...]`

Same port-spec expansion. Calls `DeletePortMapping` for each. Output
format mirrors `forward`. A 714 ("NoSuchEntryInArray") is reported as
`gone` (not `fail`), since the desired end state is achieved either way.

```
ok    TCP   60010
gone  UDP   60010   (no such entry)
```

No `--preset` semantics here — to clear the default preset, the wrapper
will run `status --json` and then `unforward` the specific ports.

## Cache file format

`./.upnp-cache.json` next to the script:

```json
{
  "version": 1,
  "entries": {
    "192.168.0.1": {
      "root_desc_url": "http://192.168.0.1:1900/fqxbs/rootDesc.xml",
      "control_url":   "http://192.168.0.1:1900/fqxbs/ctl/IPConn",
      "service_type":  "urn:schemas-upnp-org:service:WANIPConnection:1",
      "fetched_at":    1747008000,
      "ttl_seconds":   86400
    }
  }
}
```

The script creates the file on first write with mode 0600. A corrupt
cache file is renamed to `.upnp-cache.json.bad` and recreated; this
is logged to stderr but does not fail the operation.

## Error handling

- **SSDP no response** — retry once with `MX: 5` and 2× timeout; on
  second failure exit 2 with `error: no SSDP response from <host>:1900
  after Ns (tried MX=2 then MX=5)`.
- **SCPD fetch failure** — exit 3 with HTTP status + URL.
- **WAN service not found in rootDesc** — exit 3 with available service
  list to aid debugging.
- **SOAP fault on a port operation** — record per-port failure, continue
  with the rest of the batch. Final exit is 1 if any failed.
- **SOAP fault from cached URL (404 or UPnPError 401)** — invalidate
  the cache entry, re-discover once, retry the original operation. A
  second failure surfaces to the user with both errors annotated.
- **Permission denied / sudo unavailable for `scan`** — exit 1 with a
  one-line explanation pointing the user at `discover` instead.
- **Unparseable port spec** — argparse-level error, exit 2.

## Testing strategy

Unit-testable pieces (no network required):

- SSDP response parsing (sample raw responses as fixtures).
- rootDesc XML parsing (sample XML as fixtures, including
  `WANIPConnection` and `WANPPPConnection` variants).
- SOAP envelope construction (golden-output comparison).
- SOAP response and fault parsing.
- Port spec parser (`60010`, `60010/tcp`, `60010-60015`,
  `60010-60015/udp`, malformed inputs).
- Cache load/save/invalidate with a temp directory.

Tests live in `test_upnp.py` next to the script and use `unittest` from
the stdlib. `python -m unittest test_upnp.py` is the runner; no
external test framework.

Manual smoke tests against the actual router are the final acceptance
check and are listed in the implementation plan, not here.

## Migration

1. Implement `upnp.py` per this spec and ship `test_upnp.py`.
2. Run manual smoke tests against the home router that the existing
   `request_upnp.sh` currently targets.
3. Once `forward --preset default` reproduces the old script's effect,
   delete `discovery.sh` and `request_upnp.sh`.
4. The future cron wrapper for the double-NAT machine consumes
   `status --json`, `forward --json`, and `unforward --json` and is
   designed in a separate spec.
