# prana deploy — tailnet exposure (spec Layer 2)

Tailscale is a *host/deploy* concern: the PWA and other clients are
location-agnostic and consume absolute URLs. This directory is the home
of that config (platform spec, "Where it lives — NOT in the PWA
folder").

## Current serve map (machine: `intergalactic`, tailnet `tail807360.ts.net`)

| Public (tailnet-only, HTTPS) | Local backend | What |
|---|---|---|
| `https://intergalactic.tail807360.ts.net` | `127.0.0.1:8799` | akhada dashboard + /talk |
| `https://intergalactic.tail807360.ts.net:7443` | `127.0.0.1:7880` | LiveKit media server |
| `https://intergalactic.tail807360.ts.net:8443` | `127.0.0.1:8811` | **brain server** |

**Brain base URL for clients (`brain.baseUrl` in the phone app):**

```
https://intergalactic.tail807360.ts.net:8443
```

TLS certs are issued and rotated by Tailscale for the ts.net name —
no mixed content for the HTTPS-served PWA, nothing to renew by hand.
`tailscale serve` is tailnet-only (never `funnel`); reachability is
still not authorization — the brain requires its bearer token on every
request regardless of who can route to it.

## Apply / verify / remove

```powershell
# apply (idempotent)
./tailscale-serve.ps1

# verify
& "C:\Software\Tailscale\tailscale.exe" serve status

# remove just the brain mapping
& "C:\Software\Tailscale\tailscale.exe" serve --https=8443 off
```

## CORS pairing

The brain's origin allowlist is env-driven
(`NARADA_BRAIN_CORS_ORIGINS`, comma-separated) and set in
`~/.narada/host/components.yaml` on the `brain` component. It carries
the tailnet origins + the packaged-app origins
(`capacitor://localhost`, `https://localhost`). Wildcards are
forbidden by the spec's browser transport contract.

## ACL note

Tailnet ACLs should keep the brain/voice ports reachable from Suti's
devices only (least privilege). Ruled in the spec; configured in the
Tailscale admin console, not here.
