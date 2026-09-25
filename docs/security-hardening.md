# Security hardening: the NetOps server

## Why this server needs extra care

The NetOps server holds:

* **login credentials for every switch and firewall;**
* every **running config**, which contains password hashes, SNMP communities and VPN keys;
* from firmware phase 3, the ability to **reload devices**.

Anyone who takes over this server effectively controls the network. Treat it like a
domain controller: an isolated, minimal, closely watched, tier-0 system.

Being air-gapped reduces the risk but doesn't remove it. The threats that remain are:

* someone, or a compromised machine, already inside the network;
* malware carried in on transfer media;
* insiders;
* stolen backups or VM snapshots.

The steps below are ordered so each one builds on the last. Template files are in
`deploy/hardening/`; after installation they are also in
`/opt/netops/current/deploy/hardening/`.

**Quick checklist**

- [ ] §1 Server in the management network; GUI and SSH reachable only from admin subnets
- [ ] §2 VM encrypted; hypervisor console and snapshots restricted
- [ ] §3 Minimal OS; unneeded packages and services removed
- [ ] §4 Named admin accounts, SSH keys only (hardware keys if possible), sudo logged
- [ ] §5 Host firewall: default deny, **inbound and outbound**
- [ ] §6 Kernel and network settings
- [ ] §7 Service sandboxing checked (`systemd-analyze security netops`)
- [ ] §8 HTTPS with an internal CA certificate; app settings locked down
- [ ] §9 Logs forwarded off the server, with alerts
- [ ] §10 File-integrity baseline (AIDE); audit rules loaded
- [ ] §11 Devices accept SSH only from the server and jump hosts, with least-privilege accounts
- [ ] §12 Patching, backups, key custody and transfer process agreed and written down
- [ ] §13 Quarterly checks booked

---

## 1. Where the server sits on the network

* Put it on the **network management VLAN/VRF**, next to the device management
  interfaces. It must not be on a user VLAN.
* It must not be reachable from user networks. Only these may connect:
  * the **admin workstations or jump hosts** (for SSH);
  * the subnets of the people who use the GUI (for HTTPS);
  * and ideally, only privileged access workstations (PAWs) for both.
* Enforce this on the network too (ACLs on the management gateway or firewall), not
  only on the host firewall (§5). Two independent layers mean one mistake isn't enough
  to expose the server.
* It needs **no** inbound access from the devices. (Firmware phase 2 will add HTTPS
  image downloads for AlliedWare Plus switches; that rule will be added then.)

## 2. Virtual machine and hypervisor

* **Encrypt the VM** (vSphere VM Encryption / Hyper-V shielded VM with vTPM). If the
  hypervisor can't, use LUKS in the Ubuntu installer and accept entering the
  passphrase at the console after each reboot.
* **Snapshots and VM backups contain the credential key and the database.** Restrict who
  can take, copy or export them, as strictly as who can log in to the server.
* Restrict **console access** to the VM to the same admin group.
* Remove unused virtual hardware (CD drive, floppy, USB, sound). Disable shared folders,
  copy/paste and drag-and-drop.
* Keep the host's time sync off; the VM uses chrony and your NTP server.

## 3. Minimal operating system

Start from **Ubuntu Server (minimized)** without snaps (see install-airgap.md §2), then
remove what isn't needed:

```bash
sudo apt purge -y snapd cloud-init modemmanager avahi-daemon cups* 2>/dev/null
sudo apt autoremove --purge -y
systemctl list-units --type=service --state=running    # review: anything you don't recognise?
ss -tulpn                                              # only sshd (22), nginx (80/443), chronyd and the app on 127.0.0.1:8000
```

* Mount `/var/lib/netops` with `nodev,nosuid,noexec` (install-airgap.md §2).
* Make `/tmp` a private, non-executable tmpfs:
  ```bash
  echo 'tmpfs /tmp tmpfs defaults,nodev,nosuid,noexec,size=2G,mode=1777 0 0' | sudo tee -a /etc/fstab
  sudo mount -o remount /tmp 2>/dev/null || sudo mount /tmp
  ```
  (If an `apt` install ever fails with "permission denied" in `/tmp`, run
  `sudo mount -o remount,exec /tmp` for the upgrade, then remount it with `noexec`.)
* **AppArmor** stays on in enforcing mode (the Ubuntu default): `sudo aa-status`.
* **Login banner:**
  `sudo cp /opt/netops/current/deploy/hardening/issue.net /etc/issue.net /etc/issue`

## 4. Accounts, SSH and sudo

* **One named account per admin.** No shared accounts, and never log in as root.
  Put the admins in a group:
  ```bash
  sudo groupadd netops-admins
  sudo usermod -aG netops-admins,sudo alice
  sudo passwd -l root                         # root has no password login
  ```
* **SSH keys only.** Best is a **hardware-backed key** (FIDO2: YubiKey, SoloKey and
  similar). It gives two factors without any extra software on the server:
  ```bash
  # on the admin's workstation:
  ssh-keygen -t ed25519-sk -O resident -C "alice netops"
  # add the .pub line to ~alice/.ssh/authorized_keys on the server
  ```
* **Harden sshd.** Test a key login in a second window before closing your current session:
  ```bash
  sudo cp /opt/netops/current/deploy/hardening/sshd-netops.conf /etc/ssh/sshd_config.d/10-netops.conf
  sudo sshd -t && sudo systemctl reload ssh
  ```
  This turns off passwords and root login, allows only the `netops-admins` group,
  disables forwarding and tunnels, and allows only modern algorithms.
* **sudo:** admins log in with a key, but sudo still asks for their own password
  (store it in the vault). Log every sudo session:
  ```bash
  echo 'Defaults use_pty,log_output,logfile=/var/log/sudo.log,timestamp_timeout=5' | sudo tee /etc/sudoers.d/10-netops
  sudo chmod 0440 /etc/sudoers.d/10-netops && sudo visudo -c
  ```
* **Idle sessions:** `echo 'TMOUT=900; readonly TMOUT; export TMOUT' | sudo tee /etc/profile.d/tmout.sh`

## 5. Host firewall (nftables): default deny, in and out

Outbound filtering is the most important part. If someone does get in, they shouldn't
be able to use this server to reach anything except the devices it manages.

```bash
sudo cp /opt/netops/current/deploy/hardening/nftables.conf /etc/nftables.conf
sudo nano /etc/nftables.conf        # set ADMIN_NETS, GUI_NETS, DEVICE_NETS, DC/DNS/NTP/SYSLOG hosts
sudo nft -c -f /etc/nftables.conf   # syntax check only
```

**Apply it safely over SSH.** Load it with an automatic undo in 5 minutes, check that you
can still connect, then make it permanent:

```bash
sudo systemd-run --on-active=5min --unit=nft-undo /usr/sbin/nft flush ruleset
sudo nft -f /etc/nftables.conf
# From a NEW ssh session: can you log in? Does the GUI work? Does "Backup now" work?
sudo systemctl stop nft-undo.timer     # it works: cancel the undo
sudo systemctl disable --now ufw 2>/dev/null; sudo systemctl enable --now nftables
```

Dropped traffic is logged: `journalctl -k | grep nft-`. **An `nft-out-drop` line means
something on the server tried to connect somewhere it shouldn't.** Investigate every one.

## 6. Kernel and network settings

```bash
sudo cp /opt/netops/current/deploy/hardening/sysctl-netops.conf /etc/sysctl.d/90-netops.conf
sudo sysctl --system
```

These settings turn off routing, redirects and source routing. They hide kernel
addresses, restrict debugging of other processes and block unprivileged BPF. They also
**disable IPv6**; remove those three lines if your management network uses IPv6.

## 7. The NetOps service (already done by the installer)

What the installer set up, for your security review:

* The app runs as **`netops`**, a system account with no password and no shell. It
  listens only on **127.0.0.1:8000**, so it can only be reached through nginx.
* **The code is owned by root** and read-only to the service. A flaw in the app can't be
  used to change the app itself.
* **Sandbox** (`/etc/systemd/system/netops.service`):
  * the service can write only to `/var/lib/netops`;
  * it can't see home folders or devices;
  * it can't gain privileges or load kernel modules;
  * it can only use normal network sockets and a limited set of system calls.
  Check the score with `systemd-analyze security netops`.
* **Secrets:** `credential.key` and `netops.env` are `root:netops 0640`. Device passwords
  are encrypted in the database with that key, so a copy of the database alone exposes nothing.
* **Audit events** (logins, failed logins, every change) are written to the database
  and to the service log, which §9 forwards to your SIEM.

## 8. HTTPS and app settings

* Use a certificate from your **internal CA** (install-airgap.md §6). The nginx config:
  * allows only TLS 1.2/1.3 with modern ciphers;
  * redirects HTTP to HTTPS and sends HSTS;
  * hides the nginx version;
  * sets the client address itself, so clients can't fake it in the audit log.
* Distribute your internal CA to admin browsers, so nobody gets used to clicking
  through certificate warnings.
* In `/etc/netops/netops.env`:

  | Setting | Value | Why |
  |---|---|---|
  | `NETOPS_LDAP_URL` | `ldaps://…` with `NETOPS_LDAP_CA_FILE` set | Passwords never cross the network unencrypted; the DC's identity is checked |
  | `NETOPS_LDAP_VIEWER_GROUP` | A specific group | If it's empty, every domain user can sign in |
  | `NETOPS_LDAP_ADMIN_GROUP` | A small group | Admins can see device configs and change everything |
  | `NETOPS_LOCAL_ADMIN_PASSWORD_HASH` | Empty, or a long vault-held password | Break-glass only; alert on every use (§9) |
  | `NETOPS_SESSION_HTTPS_ONLY` | `true` | Login cookie never sent over plain HTTP |
  | `NETOPS_SESSION_MAX_AGE_HOURS` | `8`–`12` | Sessions expire |
  | `NETOPS_VIEWERS_CAN_READ_CONFIGS` | `false` | Configs contain secrets |
  | `NETOPS_API_DOCS` | `false` | Doesn't publish a map of every endpoint |

* Keep the **AD groups** small and review their members every quarter.

## 9. Logging and alerting

Logs that stay only on the server can be deleted by an attacker. Forward them over TLS
to your SIEM or log server (`rsyslog-gnutls` is in the bundle):

```bash
sudo tee /etc/rsyslog.d/90-siem.conf <<'EOF'
global(DefaultNetstreamDriverCAFile="/etc/netops/siem-ca.pem")
*.* action(type="omfwd" target="siem.corp.local" port="6514" protocol="tcp"
           StreamDriver="gtls" StreamDriverMode="1" StreamDriverAuthMode="x509/name"
           queue.type="LinkedList" queue.filename="siem" queue.saveOnShutdown="on")
EOF
sudo systemctl restart rsyslog
```

Alert on:

| Event | Where it shows | Why |
|---|---|---|
| `nft-out-drop` | kernel log | Something on the server tried to reach a place it shouldn't. Possible compromise |
| `action=login` by the local admin account | `netops.audit` | Break-glass was used |
| Many `action=login.failed` | `netops.audit` | Password guessing |
| `netops-secrets`, `netops-code`, `sshd`, `sudoers`, `identity` keys | auditd (`/var/log/audit/audit.log`) | Someone read the key or changed code, SSH, sudo or accounts |
| SSH logins | `sshd` in auth.log | Expected only from named admins, at expected times |
| Service restarts | systemd | Unexpected restarts may be tampering or a crash |

## 10. File integrity and audit trail

* **Audit rules:** the installer loads them (`deploy/hardening/audit-netops.rules`).
  Check with `sudo auditctl -l`. They record changes to the NetOps code, settings,
  secrets, SSH, sudo, accounts and firewall, plus every command an admin runs as root.
* **AIDE:** a fingerprint of every system file, so changes you didn't make stand out.
  ```bash
  echo '!/var/lib/netops' | sudo tee /etc/aide/aide.conf.d/90_netops   # app data changes daily; don't track it
  sudo aideinit                                     # build the baseline (takes a few minutes)
  sudo cp /var/lib/aide/aide.db /root/aide.db.$(date +%F)   # also keep a copy OFF the server
  sudo aide --config /etc/aide/aide.conf --check    # compare now; Ubuntu also runs this daily
  ```
  After every planned change (patching, NetOps upgrade), check that the reported
  changes are the ones you expected, then refresh the baseline:
  `sudo aideinit -y -f`.

## 11. The network devices

The server's credentials are only as dangerous as the devices allow. On the devices:

* **Accept SSH only from the NetOps server and the jump hosts.** For example, on IOS/IOS-XE:
  ```
  ip access-list standard MGMT-SSH
   permit host 10.10.20.5          ! NetOps server
   permit 10.10.10.0 0.0.0.15      ! jump hosts
   deny any log
  line vty 0 15
   access-class MGMT-SSH in
   transport input ssh
  ip ssh version 2
  ```
  * NX-OS: an `access-class` on the vty.
  * ASA: `ssh <net> <mask> <interface>` lines.
  * FTD: set the management access list in FDM.
  * AlliedWare Plus: an access list applied to the vty lines.
* **Least-privilege accounts, through TACACS+ or RADIUS:**
  * The **backup account** may run only `show` commands, `dir`, `terminal length`/`width`
    and `enable`. Use TACACS+ command authorization to enforce this.
  * The **upgrade account** (firmware phase 2 onwards) is separate. Keep it **disabled
    except during change windows**.
  * Use different credentials for firewalls and switches (separate credential profiles).
  * Rotate the passwords at least yearly, and immediately when someone with access leaves.
* **Log device logins centrally**, and alert when the backup account logs in from
  anywhere other than the NetOps server.
* Keep device firmware current. The Firmware tool's version report shows what's behind.

## 12. Processes

* **Transfer:**
  * Use only the approved media and scanning station.
  * Record the bundle's SHA-256 in the change ticket and check it on the server
    (install-airgap.md §4).
  * Nothing else goes onto the server: no ad-hoc tools, no scripts from the internet.
* **Patching:**
  * Ubuntu updates monthly, and within days for critical advisories (install-airgap.md §8).
  * Rebuild the NetOps bundle at the same time: `build-bundle.sh` refuses to build if
    any Python package has a known vulnerability.
* **Key custody:**
  * Keep `credential.key` offline, in the vault or safe, with at least two people able
    to get it.
  * If you believe the server was compromised, **rotate every device password.** Don't
    stop at replacing the key.
* **Backups:**
  * Copy the nightly backups off the server.
  * Test a restore every quarter (install-airgap.md §9).
  * Backups contain configs, so store them with the same care as the server.
* **Leavers:** remove their SSH keys and server account, remove them from the AD groups,
  and rotate any shared secrets they knew.

## 13. Quarterly checks

- [ ] Ubuntu and NetOps up to date; `pip-audit.txt` in the latest bundle is clean
- [ ] `systemd-analyze security netops` still scores the same
- [ ] Members of `netops-admins`, `NetOps-Admins` and `NetOps-Viewers` reviewed
- [ ] `authorized_keys` on the server reviewed; no unknown keys
- [ ] Firewall rules still match the network; no unexplained `nft-out-drop` entries
- [ ] AIDE reports only expected changes
- [ ] Restore test done; `credential.key` copy confirmed readable
- [ ] Device SSH access lists and the backup account's TACACS+ permissions still correct
