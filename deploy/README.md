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

## The PWA (static assets)

The built narada-phone-app is served BY THE BRAIN at `/app` on its own
origin — same-origin with the API, so the installed app needs no CORS:

```
https://intergalactic.tail807360.ts.net:8443/app/
```

Publish flow: `npm run deploy:demo` in narada-phone-app, then
`./publish-pwa.ps1` here (copies `out/narada` →
`%LOCALAPPDATA%\narada\pwa`, the brain's static mount). Not under
`~/.narada` — that repo is memory, not an asset store. (`tailscale
serve` path-mode would need local admin; the brain mount avoids it.)

## CORS pairing

The allowlist merges three additive sources, deduplicated, no
wildcards (spec's browser transport contract):

1. Code defaults: `capacitor://localhost`, `https://localhost`.
2. `NARADA_BRAIN_CORS_ORIGINS` env (components.yaml) — the supervisor
   holds env in memory, so changes here need a **host bounce**.
3. `~/.narada/brain/cors-origins.txt` (one per line, `#` comments) —
   picked up whenever the brain **process** restarts: kill it and the
   supervisor respawns it in ~5s. The deploy-time knob; the phone
   app's dev origins live here.

## ACL note

Tailnet ACLs should keep the brain/voice ports reachable from Suti's
devices only (least privilege). Ruled in the spec; configured in the
Tailscale admin console, not here.
