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


# --- upgrade pre/post checks (IOS-XE) ------------------------------------------

ALARMS_OK = """System Totals  Critical: 0  Major: 0  Minor: 0

Source                     Time                   Severity    Description [Index]
------                     ------                 --------    -------------------
"""

ALARMS_BAD = """System Totals  Critical: 1  Major: 0  Minor: 1

Source                     Time                   Severity    Description [Index]
------                     ------                 --------    -------------------
Switch 1                   Oct 01 2026 09:12:44   CRITICAL    Power Supply Bay 2 Failed [3]
Switch 2                   Oct 01 2026 09:13:10   MINOR       Temperature Inlet High [1]
"""

ENV_OK = """Switch   FAN     Speed   State   Airflow direction
---------------------------------------------------
  1       1     14240     OK     Front to Back
  1       2     14240     OK     Front to Back
FAN PS-1 is OK
FAN PS-2 is NOT PRESENT
SW  PID                 Serial#     Status           Sys Pwr  PoE Pwr  Watts
--  ------------------  ----------  ---------------  -------  -------  -----
1A  PWR-C1-715WAC       DCB2133ABCD  OK              Good     Good     715
1B  Not Present
Sensor List:  Environmental Monitoring
 Sensor           Location        State               Reading       Range(min-max)
 PS1 Vout          1               GOOD                56491 mV      na
 SYSTEM INLET      1               GREEN               28 Celsius    -5 - 46
"""

ENV_BAD = ENV_OK.replace("  1       2     14240     OK", "  1       2         0     FAILED")

CPU_OK = "CPU utilization for five seconds: 5%/0%; one minute: 6%; five minutes: 7%\n"
CPU_HIGH = "CPU utilization for five seconds: 95%/2%; one minute: 91%; five minutes: 88%\n"
MEM_OK = "Processor Pool Total: 1453127928 Used:  362436052 Free: 1090691876\n"

INSTALL_OK = """[ Switch 1 2 ] Installed Package(s) Information:
State (St): I - Inactive, U - Activated & Uncommitted,
            C - Activated & Committed, D - Deactivated & Uncommitted
--------------------------------------------------------------------------------
Type  St   Filename/Version
--------------------------------------------------------------------------------
IMG   I    17.06.05.0.1234
IMG   C    17.09.04a.0.6

--------------------------------------------------------------------------------
Auto abort timer: inactive
--------------------------------------------------------------------------------
"""

INSTALL_PENDING = INSTALL_OK.replace("IMG   C    17.09.04a.0.6", "IMG   U    17.12.04.0.11").replace(
    "Auto abort timer: inactive", "Auto abort timer: active , time before rollback - 05:40:12")

UNSAVED_NONE = "\n!Contextual Config Diffs:\n!No changes were found\n"
UNSAVED_SOME = """
!Contextual Config Diffs:
interface GigabitEthernet1/0/5
 +description new-printer
+ntp server 10.1.1.10
"""

STACK_OK = """Switch/Stack Mac Address : 0011.2233.4455 - Local Mac Address
Mac persistency wait time: Indefinite
                                             H/W   Current
Switch#   Role    Mac Address     Priority Version  State
-------------------------------------------------------------
*1       Active   0011.2233.4455     15     V01     Ready
 2       Standby  0011.2233.4466     14     V01     Ready
"""

STACK_BAD = STACK_OK.replace("V01     Ready\n \n", "").replace(
    " 2       Standby  0011.2233.4466     14     V01     Ready",
    " 2       Member   0011.2233.4466     14     V01     Removed")

IP_BRIEF = """Interface              IP-Address      OK? Method Status                Protocol
Vlan1                  unassigned      YES NVRAM  administratively down down
Vlan34                 10.3.34.1       YES NVRAM  up                    up
GigabitEthernet1/0/1   unassigned      YES unset  up                    up
GigabitEthernet1/0/2   unassigned      YES unset  down                  down
GigabitEthernet2/0/13  unassigned      YES unset  up                    up
"""

CDP_DETAIL = """-------------------------
Device ID: DUB01-CORE-01.corp.local
Entry address(es):
  IP address: 10.3.0.1
Platform: cisco C9500-48Y4C,  Capabilities: Router Switch IGMP
Interface: GigabitEthernet1/0/1,  Port ID (outgoing port): TwentyFiveGigE1/0/1
Holdtime : 155 sec
-------------------------
Device ID: GH-AS02
Entry address(es):
  IP address: 10.3.0.12
Platform: cisco C9300-48P,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/1/1,  Port ID (outgoing port): GigabitEthernet1/1/1
Holdtime : 140 sec
"""

LLDP_DETAIL = """------------------------------------------------
Local Intf: Gi2/0/13
Chassis id: 0030.4604.c02f
Port id: 1
System Name: A-DUB01-GH-100-3

------------------------------------------------
Local Intf: Gi1/0/1
Chassis id: 00aa.bbcc.dd01
System Name: DUB01-CORE-01

Total entries displayed: 2
"""

ETHERCHANNEL = """Flags:  D - down        P - bundled in port-channel
        I - stand-alone s - suspended
Group  Port-channel  Protocol    Ports
------+-------------+-----------+-----------------------------------------------
1      Po1(SU)         LACP      Gi1/1/1(P)  Gi2/1/1(P)
"""

MAC_COUNT = """Mac Entries for Vlan 34:
---------------------------
Dynamic Address Count  : 40
Static  Address Count  : 0
Total Mac Addresses    : 40

Total Mac Addresses for this criterion: 52
"""

ASA_CPU = "CPU utilization for 5 seconds = 1%; 1 minute: 2%; 5 minutes: 3%\n"
ASA_MEM = "Free memory:        6144000000 bytes (75%)\nUsed memory:        2048000000 bytes (25%)\n"
ASA_FAILOVER_BAD = ASA_FAILOVER.replace("Other host: Secondary - Standby Ready",
                                        "Other host: Secondary - Failed")
