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

Pick protocol, interface (Kvaser channel) and bitrate, then **Connect**.
The interface starts in **Listen only** (silent) mode, so it never transmits.
Turn listen-only off to send NMT commands. Also turn it off when the device
under test is the only other node on the bus, because a silent interface does
not ACK frames.

### Without hardware

Choose **Simulator** as the interface. It starts a simulated J1939 engine ECU
or a CANopen node on an in-process virtual bus. Load
`examples/sim_j1939.dbc` or `examples/sim_node.eds` (node 5) to see decoding.
The CANopen simulator obeys NMT commands. Its master also remaps TPDO1 at boot,
differently from the deliberately out-of-date example EDS, which shows the
learned-mapping feature at work.

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
