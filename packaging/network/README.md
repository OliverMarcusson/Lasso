# Lasso catch-all network

`lasso0` is a persistent TUN device that gives traffic somewhere to go when the laptop is offline, so `lassod` can see that something wants the network (spec section 7).

```
sudo ./setup-network.sh --user oliver [--deny-unit syncthing.service]...
sudo ./remove-network.sh
```

Both scripts are idempotent. Setup does three things:

- It writes `/etc/lasso/lasso.nft` from `lasso.nft.in` and installs `lasso-nftables.service`, which loads that file at boot.
- It creates the NetworkManager connection `lasso0`. This is a `tun` connection owned by the user's UID, with `10.211.0.2/30` via gateway `10.211.0.1` and `fd6c:6173:736f::2/64` via gateway `fd6c:6173:736f::1`. Both default routes have metric 30000, and DNS is `10.211.0.1` with `dns-priority 1000`.
- It adds the optional `--deny-unit` drop rules.

## Firewall (`table inet lasso`)

- **output → to_lasso:** only the user's UID and `systemd-resolved` DNS (port 53) can reach `lasso0`. Everything else is dropped, so system services such as timesyncd or NetworkManager's connectivity check fail at once with `EPERM` and never count as demand.
- **raw_output:** packets to `lasso0` are `notrack`. Without this, a connection's first SYN creates a conntrack entry with no NAT. Its later retransmits through Wi-Fi would then keep the unroutable `10.211.0.2` source.
- **postrouting:** packets from `10.211.0.2` or `fd6c:6173:736f::2` that leave through any interface other than `lasso0` are masqueraded. Once the hotspot route wins, a connection started while offline can still complete.

## DNS routing

NetworkManager talks to systemd-resolved and adds the `~.` routing domain automatically to every connection that has a default route. When several connections carry the same domain, only the one with the best (lowest) `dns-priority` keeps it (`man nm-settings-nmcli`, `ipv4.dns-priority`). NetworkManager 1.58 also sets resolved's per-link DefaultRoute flag (`SetLinkDefaultRoute`) to match.

`lasso0` therefore uses `dns-priority 1000`, which is worse than the default 100 for normal connections and 50 for VPNs. The result:

- **Offline:** `lasso0` is the only link with a default route, so it gets `~.`. Every lookup goes to `10.211.0.1` and shows up as a packet.
- **Online:** the real connection takes `~.` and `lasso0` gets neither `~.` nor DefaultRoute, so resolved does not query it at all. Per `man systemd-resolved`, unmatched queries go only to default-route links.

This needs no runtime toggling and no privilege for `lassod`. The priority must stay positive, because a negative value would exclude every other connection's DNS.

Trade-offs:

- A connection with a default route always outranks `lasso0` for DNS, even when it has no working upstream (captive or limited Wi-Fi). In that case traffic does not reach `lasso0` anyway, so this changes nothing.
- A VPN or other link that sets `~.` itself, outside NetworkManager (for example Tailscale MagicDNS), still shares queries with `lasso0` while both are up. Those queries are DNS from resolved, so they are subject to the name filter.

## Per-unit filtering (`deny_units`)

`socket cgroupv2 level N "path"` resolves the path to a cgroup ID when the rule is loaded. The cgroup must exist at that moment, and the rule stops matching once the unit restarts, because the restart creates a new cgroup. So these rules are kept out of the boot-time ruleset, and the whole file would fail to load anyway before the user logs in. Instead, `setup-network.sh --deny-unit UNIT` adds them to the `deny_units` chain best effort. Units that are not running are skipped with a warning.

Re-run the setup command after login, or after restarting a listed unit, to refresh them. The failure mode is mild: an extra trigger, which `lassod`'s gating (offline, user active, backoff) still filters. `config.deny_units` is not read by `lassod`. List the same units on the setup command line.

If this proves too fragile, the robust upgrade is unprivileged attribution in `lassod`. It would map a packet's source port to a socket inode through `/proc/net/{tcp,udp}*`, find the owning process through `/proc/*/fd`, and read its unit from `/proc/PID/cgroup`. Everything that reaches `lasso0` is the user's own, so it is all readable.

## Known limitations

- While online, an IPv6 destination on an IPv4-only network routes to `lasso0` through the `::/0` route, so it hangs instead of failing fast. Address selection prefers IPv4, because the ULA source loses against global IPv6 destinations, so this mostly affects happy-eyeballs fallbacks.
- Not verified on hardware: whether NetworkManager activates the tun connection before `lassod` has opened the device (carrier), and whether resolved's link state exactly matches the description above. After setup, check `resolvectl status lasso0` while online. It should show `Default Route: no` and no `~.`.
