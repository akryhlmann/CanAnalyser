# CanAnalyser

Desktop monitor for **J1939** and **CANopen** devices on a Kvaser interface
(built for the Kvaser USBcan Light 2xHS). It is meant for a test bench where
one product, either J1939 or CANopen, is connected at a time.

## Features

| | J1939 | CANopen |
|---|---|---|
| Trace with decoded text, filter, pause | ✔ | ✔ |
| Per-ID overview (count, period, latest data) | ✔ | ✔ |
| Signal values | DBC signals; `n/a` / `error` shown for J1939 reserved ranges | PDOs via EDS **or** mapping learned from SDO traffic; SDO reads/writes |
| Multi-frame | TP BAM and RTS/CTS reassembly | Expedited and segmented SDO (block transfer is shown but not reassembled) |
| Nodes | Address claim (NAME), PGNs seen | NMT state, measured heartbeat interval, heartbeat timeout |
| Faults | DM1/DM2 (SPN, FMI, OC, lamps) | EMCY with active-set tracking and error register |
| Transmit | – | NMT start, stop, pre-op, reset node, reset comm |
| Record / replay | BLF, ASC, candump .log, TRC, CSV | same |

### Incomplete DBC / EDS files

- **J1939:** PGNs missing from the DBC are still named from a built-in table of
  common PGNs and shown as raw data. DM1 decoding does not need the DBC; when
  the DBC defines SPNs, fault descriptions use those names. You can load
  several DBC files.
- **CANopen:** PDO mappings are taken from the EDS, then **overridden by
  whatever the master is seen writing or reading** in objects 0x1400–0x1BFF.
  If you capture the device's start-up configuration (or no EDS at all), the
  PDOs still decode, just without object names. Objects missing from the EDS
  are shown as `0xIIII:SS`.

## Setup (Windows)

1. Install **Kvaser Drivers for Windows** from kvaser.com. It includes `canlib32.dll`, which python-can uses.
2. Install Python 3.10 or newer.
3. In the repository folder:

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e .[dev]
   ```

## Running

```powershell
cananalyser                                   # plain start
cananalyser --dbc engine.dbc --dbc extra.dbc  # J1939 with DBC files
cananalyser --eds drive.eds:5                 # CANopen, EDS for node 5
```

Pick protocol, interface (Kvaser channels are listed by device name and serial
number), and bitrate, then **Connect**.

**Load DBC / EDS…** accepts both file types: a `.dbc` is used for J1939, and
an `.eds`/`.dcf` for CANopen (you're asked for the node ID; a node already
seen on the bus is pre-filled). While disconnected, the protocol selector
switches to match the file.

The interface connects in **normal mode**. It ACKs frames but transmits
nothing on its own; it only sends when you press an NMT button. This is needed
on a bench where the device under test is the only other node.
**Listen only** (silent mode) is for networks that already have other nodes
to ACK frames, such as a vehicle. With a single device it receives nothing.

The status bar shows hardware bus load, error frames and overruns. If nothing
arrives within 3 seconds it suggests the likely cause: only error frames
point to a wrong bitrate, and a silent bus points to listen-only, the channel
or the wiring.

### Joystick view (Danfoss JS1-H)

The **Joystick** tab fills automatically once JS1-H data arrives. For J1939
that means BJM/EJM messages decoded with `databases/danfoss_js1h/js1h_j1939.dbc`;
for CANopen, TPDOs carrying JS1-H objects (6401h, 6404h, 6020h), with the
JS1-H EDS loaded or the mapping learned from the bus.

- **XY plot:** base X/Y in ±100 %, with a trail of recent positions. J1939
  positions are combined with their direction bits to give signed values.
- **Bars:** one per grip input: 2 on J1939 (EJM grip X/Y), 3 on CANopen (grips 1–3).
- **Button LEDs:** pressed, error and not available. On CANopen, buttons that
  are not enabled are shown faded.
- **Axis status:** neutral and detent indicators. Sensor errors (1022 counts,
  or neutral state "error") show in red.

The selector lists each joystick seen: J1939 source address with message
set 1 (BJM1/EJM1) or 3 (BJM3/EJM3), or CANopen node ID.

### Redundancy check (JS1-H SBW)

For joysticks that send BJM1 from two controllers, the **Redundancy** tab
compares the two channels: a primary, and a secondary that sends every data
byte bit-inverted. The defaults are SA 51 (0x33) for the primary and 52
(0x34) for the secondary; both can be changed in the tab. The secondary is
inverted back before decoding, so both channels appear as normal BJM1
signals.

Each primary/secondary pair from the same cycle is checked:

| Check | Rule |
|---|---|
| X / Y position | Within the position tolerance (default 2 %). Direction counts, and both channels reporting a sensor error counts as agreement. |
| Status bits, buttons 1–12 | Equal |
| Inversion | The secondary must be closer to the inverse of the primary than to a copy of it |
| Pairing gap | Time from primary to secondary (min/avg/max). Frames more than the max gap apart are counted as unpaired. |
| Channels | Period, frame count and timeout (default 100 ms), including a channel that never transmits |

A disagreement becomes a fault only after it lasts for "Fault after"
consecutive pairs (default 3). That way a button edge or neutral threshold
seen one cycle apart by the two controllers is counted but not raised.
Active faults also appear in the Faults tab. The position differences and
the pairing gap are available in the Trend tab. On the Joystick tab, the
partner channel is drawn as an orange ring.

**This is diagnostic monitoring only, not a safety function.**

The *Simulator (JS1-H SBW redundant, J1939)* injects, every 30 s, a +6 % X
offset, a 2 s secondary dropout and a stuck button 1.

### Trend plot

The **Trend** tab plots any numeric signal over time, in windows from 10 s up
to the whole recording. Check signals in the list, or double-click a row in
the Signals tab. The joystick's signed axes are available as
"Joystick … (signed)" series. J1939 `n/a` and `error` values are drawn as
gaps. About the last 20 minutes are kept per signal at a 20 ms rate.

### Without hardware

Choose **Simulator (engine ECU / I/O node)** or **Simulator (JS1-H joystick)**
as the interface. The first starts a simulated J1939 engine ECU or a CANopen
node on an in-process virtual bus.

The JS1-H simulator moves all axes, cycles the buttons, and every 15 s reports
a 3-second grip Y voltage fault (DM1 SPN 2663 or EMCY 0x3110). It loads the
bundled JS1-H DBC or EDS by itself. For CANopen, send NMT Start to node 16 to
make it transmit PDOs. Load
`examples/sim_j1939.dbc` or `examples/sim_node.eds` (node 5) to see decoding.
The CANopen simulator obeys NMT commands. Its master also remaps TPDO1 at boot,
differently from the deliberately out-of-date example EDS, which shows the
learned-mapping feature at work.

## Sharing with colleagues (Windows app, no Python needed)

The app can be packaged as a folder containing `CanAnalyser.exe`, zipped as
`CanAnalyser-windows.zip`. Colleagues unzip it and double-click the `.exe`.
Each PC still needs **Kvaser Drivers for Windows** installed. The Kvaser DLL
is not bundled. The bundled `databases` folder sits next to the `.exe`, and
the **Load DBC / EDS…** dialog opens there.

There are two ways to get the zip:

- **From GitHub:** every push to `main` runs the *Build Windows app* workflow.
  Download `CanAnalyser-windows` from the run's **Artifacts** under the
  Actions tab. Pushing a tag such as `v0.2.0` also attaches the zip to a
  GitHub Release, which gives colleagues a stable download link.
- **On your own PC:**

  ```powershell
  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
  ```

  This creates `dist\CanAnalyser-windows.zip`.

## Azure DevOps

`azure-pipelines.yml` does the same as the GitHub workflow: it runs the tests
on a Windows agent, publishes the test results, builds the app, and publishes
`CanAnalyser-windows.zip` as a pipeline artifact. It runs on every push to
`main` and on `v*` tags.

After importing the repo into Azure Repos:

1. **Pipelines → New pipeline → Azure Repos Git →** pick the repo **→ Existing
   Azure Pipelines YAML file →** `/azure-pipelines.yml`, then **Run**.
2. Optional, to build pull requests: **Repos → Branches → main → Branch
   policies → Build validation**, and add this pipeline. Azure Repos ignores
   the `pr:` section in the YAML.
3. Download the zip from a run's summary page, under **Artifacts**. To give
   colleagues a fixed location, add a step that copies it to a file share or
   publishes it to an Azure Artifacts feed.

The `.github` folder can be deleted once the repo lives in Azure DevOps.

## Tests

```
pytest
```

The tests use the python-can virtual bus and the simulators, so they need
neither hardware nor a display.

## Architecture

```
src/cananalyser/
  bus.py           Monitor: frame source (Kvaser / virtual / log replay), reader thread,
                   recording, NMT transmit. No GUI code.
  decoders/        J1939Decoder, CANopenDecoder -> DecodedFrame (model.py)
  sim.py           J1939 and CANopen traffic simulators
  gui/             PySide6 main window and table models (refreshed every 100 ms)
```

Decoding runs on the reader thread. The GUI drains a queue in batches, so a
busy bus does not stall the UI. Each decoder handles more than 70k frames/s on
a desktop CPU, while a fully loaded 1 Mbit/s bus carries about 8k.

Because `bus.py` and `decoders/` do not depend on Qt, a web dashboard (for
example FastAPI with WebSockets) could later be added on the same engine,
next to the desktop UI.
