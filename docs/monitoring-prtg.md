# Monitoring the NetOps server with PRTG

PRTG watches the server from outside. It uses four sources and needs **no agent, SNMP
or SSH account on the server**: fewer ways in means less to secure.

| What | PRTG sensor | Tells you |
|---|---|---|
| The VM itself | **VMware Virtual Machine (SOAP)**, through vCenter | CPU, memory, disk and network of the VM, and whether it is powered on |
| The tools | **HTTP Data Advanced** on `/api/monitoring/prtg` | Devices failing backup, devices with no good backup for 48 h, firmware behind standard, version checks failing, free space on the data disk, hours since the last server backup |
| The certificate | **SSL Certificate** on port 443 | Days until the HTTPS certificate expires |
| Security events | **Syslog Receiver** | Firewall blocks, logins, sudo, audit-rule hits, app audit events (logins, failed logins, changes) |

PRTG is a monitoring and alerting tool, not a SIEM: it counts and alerts on messages but
isn't a long-term, tamper-proof log store. The logs also stay on the server (journal and
`/var/log`), and the nightly backups are copied off-box. If a SIEM arrives later, forward
to it as well (security-hardening.md §9).

## 1. Firewall

In `/etc/nftables.conf`, set `PRTG_HOSTS` to the address of the PRTG probe that will
monitor this server. The template already allows:

* the probe → server on TCP 443 (health and certificate sensors) and ICMP (ping);
* the server → probe on UDP/TCP 514 (syslog).

Apply it with the safe-apply steps in security-hardening.md §5.

## 2. Health sensor (HTTP Data Advanced)

1. Create a token and put it in the settings:
   ```bash
   openssl rand -hex 32
   sudo nano /etc/netops/netops.env          # NETOPS_MONITORING_TOKEN=<the value>
   sudo systemctl restart netops
   ```
   Until a token is set, the endpoint is switched off (404).
2. Test it from the server:
   ```bash
   curl -s --resolve netops.corp.local:443:127.0.0.1 \
        -H "Authorization: Bearer <token>" https://netops.corp.local/api/monitoring/prtg
   ```
3. In PRTG, add a device for the server (its DNS name), then add an **HTTP Data Advanced** sensor:
   * **URL:** `https://netops.corp.local/api/monitoring/prtg`
   * **Request headers:** `Authorization: Bearer <token>`. If your PRTG version has no
     header field, use `https://netops.corp.local/api/monitoring/prtg?token=<token>`
     instead; nginx keeps this URL out of its access log.
   * **Scanning interval:** 5 minutes.
   * The PRTG probe must trust your internal CA: import the CA certificate into the
     probe server's *Local Computer → Trusted Root Certification Authorities* store.

The channels it creates:

| Channel | Warning / error when | Meaning |
|---|---|---|
| Devices failing backup | > 0 / > 5 | The last backup attempt failed. The GUI shows the reason |
| Devices without a good backup in 48 h | – / > 0 | Includes new devices that have never succeeded |
| Devices never backed up | – | New devices waiting for their first backup |
| Firmware: devices behind standard | – | For reports; add a limit if you want an alert |
| Firmware: version checks failing | > 0 / – | A device couldn't be logged in to or its output couldn't be read |
| Data disk free (%) | < 20 / < 10 | `/var/lib/netops`: mostly firmware images |
| Hours since server backup | > 26 / > 50 | The nightly `netops-backup` hasn't run successfully |

PRTG applies these limits only when it first creates a channel; change them later in
each channel's settings. If the sensor can't reach the page, PRTG marks it down, which
also covers "the app has stopped".

## 3. Certificate and VM sensors

* **SSL Certificate sensor** on the same device, port 443. Warn at 30 days and error at
  7 days before expiry.
* **VMware Virtual Machine (SOAP)** sensor through your vCenter, if you don't already
  monitor the VM. Choose the NetOps VM. It uses PRTG's vCenter credentials, not anything
  on the server.
* Optional: a **Ping** sensor.

## 4. Syslog (security events)

1. On the PRTG probe, add a **Syslog Receiver** sensor (UDP 514) to the server's device.
2. On the server, forward the security-relevant messages to it:
   ```bash
   # audit rule hits into syslog as well as /var/log/audit/audit.log
   sudo sed -i 's/^active = no/active = yes/' /etc/audit/plugins.d/syslog.conf
   sudo systemctl restart auditd
   # what to forward, and to where
   sudo cp /opt/netops/current/deploy/hardening/rsyslog-prtg.conf /etc/rsyslog.d/90-prtg.conf
   sudo nano /etc/rsyslog.d/90-prtg.conf        # set target="<PRTG probe address>"
   sudo rsyslogd -N1 && sudo systemctl restart rsyslog
   logger -p auth.warning "netops syslog test"  # should appear in the PRTG sensor
   ```
   Forwarded messages:
   * app audit events;
   * `sshd` and `sudo`;
   * firewall drops (`nft-in-drop`, `nft-out-drop`);
   * audit-rule hits (`key="netops-…"`, `sshd`, `sudoers`, `identity`, `admin-commands`);
   * NetOps service starts and stops.
3. In the sensor's filters, raise an **error** for messages that should never happen and
   a **warning** for ones worth a look. Check the filter syntax in your PRTG version's
   manual. The patterns to match:

   | Error (act now) | Warning (look today) |
   |---|---|
   | `nft-out-drop`: the server tried to connect somewhere it isn't allowed. **Treat as a possible compromise** | `login.failed`: repeated failed GUI logins |
   | `key="netops-secrets"` or `key="netops-code"` outside a planned change | `Accepted publickey`: an SSH login (check it's expected) |
   | `action=login` for the break-glass user (`admin`) | `nft-in-drop`: blocked connection attempts |
   | `sudoers` or `identity` audit keys outside a planned change | `netops.service` stopping or restarting |

Syslog over UDP isn't encrypted. That's acceptable here because it stays on the
management network between the server and the probe, and the messages contain no
passwords or keys. If your PRTG version accepts syslog over TLS, prefer that.
