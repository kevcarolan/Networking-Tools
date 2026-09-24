"""Trimmed real-world command output used by the firmware tests."""

IOS_XE_VERSION = """Cisco IOS XE Software, Version 17.09.04a
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.9.4a, RELEASE SOFTWARE (fc3)
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2023 by Cisco Systems, Inc.

ROM: IOS-XE ROMMON
BOOTLDR: System Bootstrap, Version 17.9.1r, RELEASE SOFTWARE (P)

core-sw1 uptime is 25 weeks, 3 days, 4 hours, 12 minutes
System returned to ROM by Reload Command
System image file is "flash:packages.conf"

cisco C9300-48P (X86) processor with 1331521K/6147K bytes of memory.
Processor board ID FOC2330X0AB

Base Ethernet MAC Address          : 00:11:22:33:44:55
Model Number                       : C9300-48P
System Serial Number               : FOC2330X0AB

Switch Ports Model              SW Version        SW Image              Mode
------ ----- -----              ----------        ----------            ----
*    1 65    C9300-48P          17.09.04a         CAT9K_IOSXE           INSTALL
     2 65    C9300-48P          17.09.04a         CAT9K_IOSXE           INSTALL

Configuration register is 0x102
"""

IOS_DIR = """Directory of flash:/

475137  -rw-             2097152  Jan 1 2024 10:00:00 +00:00  nvram_config
475138  -rw-                4382  Jan 1 2024 10:00:00 +00:00  packages.conf

11353194496 bytes total (8151625728 bytes free)
"""

IOS_CLASSIC_VERSION = """Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8, RELEASE SOFTWARE (fc1)
Technical Support: http://www.cisco.com/techsupport

access-sw3 uptime is 1 year, 2 weeks
System image file is "flash:/c2960x-universalk9-mz.152-7.E8/c2960x-universalk9-mz.152-7.E8.bin"

cisco WS-C2960X-48FPD-L (APM86XXX) processor (revision D0) with 524288K bytes of memory.
Processor board ID FOC1234X5YZ

Model number                    : WS-C2960X-48FPD-L
System serial number            : FOC1234X5YZ
"""

NXOS_VERSION = """Cisco Nexus Operating System (NX-OS) Software
TAC support: http://www.cisco.com/tac

Software
  BIOS: version 07.69
 NXOS: version 9.3(10)
  BIOS compile time:  04/08/2021
  NXOS image file is: bootflash:///nxos.9.3.10.bin
  NXOS compile time:  8/9/2022 12:00:00 [08/10/2022 10:04:12]

Hardware
  cisco Nexus9000 C93180YC-EX chassis
  Intel(R) Xeon(R) CPU  @ 1.80GHz with 24632140 kB of memory.
  Processor Board ID FDO21120ABC
"""

NXOS_DIR = """       4096    Jan 01 10:00:00 2024  .rpmstore/
 1978203648    Aug 10 10:00:00 2022  nxos.9.3.10.bin

Usage for bootflash://sup-local
 6040887296 bytes used
 47335612416 bytes free
 53376499712 bytes total
"""

ASA_VERSION = """
Cisco Adaptive Security Appliance Software Version 9.18(4)
SSP Operating System Version 2.12(0.498)
Device Manager Version 7.18(1)

Compiled on Thu 14-Sep-23 12:00 GMT by builders
System image file is "disk0:/cisco-asa-fp2k.9.18.4.SPA"
Config file at boot was "startup-config"

fw1 up 120 days 4 hours
failover cluster up 120 days 4 hours

Hardware:   FPR-2110, 16384 MB RAM, CPU MIPS 1200 MHz, 1 CPU (6 cores)

Serial Number: JAD23456ABC
"""

ASA_DIR = """
Directory of disk0:/

18     drwx  4096         10:00:00 Jan 01 2024  log
37     -rw-  245387264    10:00:00 Jan 01 2024  cisco-asa-fp2k.9.18.4.SPA

8571076608 bytes total (7979917312 bytes free/93% free)
"""

ASA_FAILOVER = """Failover On
Failover unit Primary
Failover LAN Interface: FOLINK Ethernet1/12 (up)
Reconnect timeout 0:00:00
        This host: Primary - Active
                Active time: 1234567 (sec)
        Other host: Secondary - Standby Ready
"""

FTD_VERSION = """-------------------[ ftd-edge1 ]--------------------
Model                     : Cisco Firepower 2110 Threat Defense (77) Version 7.2.5 (Build 208)
UUID                      : 1234abcd-0000-1111-2222-333344445555
LSP version               : lsp-rel-20230712-1621
VDB version               : 353
----------------------------------------------------

Cisco Adaptive Security Appliance Software Version 9.18(3)56
SSP Operating System Version 2.12(0.1104)

Hardware:   FPR-2110, 6854 MB RAM, CPU MIPS 1200 MHz, 1 CPU (12 cores)
Serial Number: JAD98765XYZ
"""

AWPLUS_VERSION = """AlliedWare Plus (TM) 5.5.2 06/13/22 10:14:06

Build name : x930-5.5.2-0.4.rel
Build date : Mon Jun 13 10:14:06 NZST 2022
Build type : RELEASE
"""

AWPLUS_SYSTEM = """Switch System Status                                   Mon Jan 01 10:00:00 2024

Board       ID  Bay   Board Name                         Rev  Serial number
--------------------------------------------------------------------------------
Base       414        x930-28GTX                         B-0  A04563H182400035
--------------------------------------------------------------------------------
RAM:  Total: 2048000 kB Free: 1500000 kB
"""

INVALID = "                ^\n% Invalid input detected at '^' marker.\n"
