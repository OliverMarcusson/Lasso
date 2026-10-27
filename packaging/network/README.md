# Lasso catch-all network

`lasso0` is a persistent TUN device that gives traffic somewhere to go when the laptop is offline, so `lassod` can see that something wants the network (spec section 7).

```
sudo ./setup-network.sh --user oliver
sudo ./remove-network.sh
```

Both scripts are idempotent. Setup does two things:

- It writes `/etc/lasso/lasso.nft` from `lasso.nft.in` and installs `lasso-nftables.service`, which loads that file at boot.
- It creates the NetworkManager connection `lasso0`. This is a `tun` connection owned by the user's UID, with `10.211.0.2/30` via gateway `10.211.0.1` and `fd6c:6173:736f::2/64` via gateway `fd6c:6173:736f::1`. Both default routes have metric 30000, and DNS is `10.211.0.1` with `dns-priority 1000`.

## Firewall (`table inet lasso`)

- **output → to_lasso:** only the user's UID and `systemd-resolved` UDP DNS (port 53) can reach `lasso0`. Everything else is dropped, so system services such as timesyncd or NetworkManager's connectivity check fail at once with `EPERM` and never count as demand. Resolved's TCP DNS fallback is dropped too; it only follows a UDP query that the watcher already saw.
- **raw_output:** packets to `lasso0` are `notrack`. Without this, a connection's first SYN creates a conntrack entry with no NAT. Its later retransmits through Wi-Fi would then keep the unroutable `10.211.0.2` source.
- **postrouting:** packets from `10.211.0.2` or `fd6c:6173:736f::2` that leave through any interface other than `lasso0` are masqueraded. Once the hotspot route wins, a connection started while offline can still complete.

## DNS routing

NetworkManager talks to systemd-resolved and configures each link's DNS routing. A connection with a default route gets resolved's per-link DefaultRoute flag (`SetLinkDefaultRoute`), and NetworkManager also adds the `~.` routing domain in some cases. Among connections that would carry the same default-route domain, only the one with the best (lowest) `dns-priority` keeps it (`man nm-settings-nmcli`, `ipv4.dns-priority`). With positive-priority automatic defaults, a link can end up with `DefaultRoute=yes` without a literal `~.` domain, so the checks below look at DefaultRoute, not only at `~.`.

`lasso0` therefore uses `dns-priority 1000`, which is worse than the default 100 for normal connections and 50 for VPNs. The result:

- **Offline:** `lasso0` is the only link with a default route, so it gets the default route for DNS. Every lookup goes to `10.211.0.1` and shows up as a packet.
- **Online:** the real connection takes the default DNS route and `lasso0` gets no DefaultRoute (and no `~.`), so resolved does not query it at all. Per `man systemd-resolved`, unmatched queries go only to default-route links.

This needs no runtime toggling and no privilege for `lassod`. The priority must stay positive, because a negative value would exclude every other connection's DNS.

Trade-offs:

- A connection with a default route always outranks `lasso0` for DNS, even when it has no working upstream (captive or limited Wi-Fi). In that case traffic does not reach `lasso0` anyway, so this changes nothing.
- A VPN or other link that sets `~.` itself, outside NetworkManager (for example Tailscale MagicDNS), still shares queries with `lasso0` while both are up. Those queries are DNS from resolved, so they are subject to the name filter.

## Per-unit filtering (`deny_units`)

`lassod` enforces `config.deny_units` itself, with no root needed. For a demand packet that is not DNS, the watcher maps the packet's source address and port to a socket inode through `/proc/net/{tcp,tcp6,udp,udp6}`, finds the user's process holding that socket in `/proc/PID/fd`, and reads `/proc/PID/cgroup`. If any cgroup path component equals a configured name (for example `syncthing.service` or an `app-*.scope`), the packet is not demand. If the owner cannot be found, it counts as demand.

## Known limitations

- While online, an IPv6 destination on an IPv4-only network routes to `lasso0` through the `::/0` route. `lassod` answers every IPv6 packet it reads with a TCP RST (for SYNs) or an ICMPv6 destination unreachable, rate-limited to 100 per second, so the sender fails fast and falls back to IPv4. This happens whether or not the machine is online. Replies are never sent to ICMPv6 errors, multicast destinations or unspecified/multicast sources.
- Not verified on hardware: whether NetworkManager activates the tun connection before `lassod` has opened the device (carrier), and whether resolved's link state exactly matches the description above. After setup, check `resolvectl status lasso0` while online. It should show `Default Route: no` and no `~.`.
