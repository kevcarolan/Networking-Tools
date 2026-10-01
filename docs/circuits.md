# Master circuit list

The **Circuits** page holds the master circuit list: every end device or service and the
switch port it uses. When you plan an upgrade, it shows which circuits a device carries.
Firmware upgrade jobs will use it from the next phase.

## Uploading the spreadsheet

1. **Circuits → Upload spreadsheet** (admins only) and choose the workbook (`.xlsx`).
2. NetOps finds the sheet and the header row by the **column names**. The coloured band
   row above the names, the column order and extra sheets don't matter.
3. You get a **preview** before anything changes:
   * rows read, and rows **new / changed / removed** compared with the live list. Changed
     rows show the old and new value of each field;
   * **switches not found in NetOps**: their rows can't be linked yet;
   * rows with **warnings**: an invalid IP, mask, gateway, VLAN or MAC, or a missing switch/port.
4. Click **Make this the live list** or **Discard**. Every upload is kept in **Import history**.

Expected columns, from the current template:

| Band | Columns |
|---|---|
| SERVICE | IP Partition, Service |
| DEVICE INFORMATION | Engineering prefix, Device Name, Device Type, Manufacturer, Hardware Mac Address |
| LOCATION | Floor Level, Room / Area |
| IP DETAILS | IP Address, Subnet Mask, Default Gateway |
| NETWORK PORT | VLAN, TYPE, PORT, **DEVICE** |
| DRAWING DETAILS | Drawing Number, Device Drawing Reference |

**DEVICE** and **PORT** (under NETWORK PORT) are required: they link a row to a switch.
Any other missing column is left empty.

## How rows are linked to NetOps devices

- **DEVICE** is matched to a NetOps device name, ignoring upper/lower case. `GH-AS01`
  matches a device called `GH-AS01`, `gh-as01.corp.local` or `DUB01-GH-AS01`.
  * An exact name wins over a match on the end of a name.
  * If several devices match, the row shows **Several devices match**. Rename one so the
    match is unambiguous.
- Linking happens every time the list is shown. So when you add a missing switch to
  NetOps later, its rows link straight away, without uploading again.
- **PORT** short and long forms are treated as the same port: `Gi2/0/13` =
  `GigabitEthernet2/0/13`, also `Te`, `Fa`, `Eth`, `Po`, and AlliedWare Plus `port1.0.1`.

## Where it shows

- **Circuits page:**
  * tiles: total, linked, not linked, with warnings, switches;
  * search and filters by service, switch, VLAN and link status;
  * CSV export.
- **Device → History → Circuits tab:** the circuits on that switch, grouped by service and
  VLAN. This is the list an upgrade job will show and save.

## Later

A sync with NetBox, once NetBox is in the closed environment.
