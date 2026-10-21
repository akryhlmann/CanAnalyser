"""Generate DBC and EDS files for Danfoss JS1-H heavy duty joysticks.

Source: Danfoss Technical Information "JS1 Heavy Duty Joysticks",
BC171886484518en-001601 (revision 1601, April 2026), appendices
"CAN J1939 protocol" and "CAN+ and CANopen protocol".

These are reconstructions from that manual, not official Danfoss files.
Where the manual contradicts itself, the choice made is commented here
and listed in databases/danfoss_js1h/README.md.

Run:  python tools/make_js1h_databases.py
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "databases" / "danfoss_js1h"
SOURCE = "Danfoss TI BC171886484518en-001601 (rev 1601)"

# --------------------------------------------------------------------------
# J1939
# --------------------------------------------------------------------------

# Bit positions in the manual are "byte/bit" with bit 1 = LSB, so the DBC
# (Intel, LSB-first) start bit is byte * 8 + bit - 1.
NEUTRAL = {0: "Not in neutral position", 1: "In neutral position", 2: "Error indicator", 3: "Not available"}
NEGATIVE = {0: "Not on negative side of neutral", 1: "On negative side of neutral", 2: "Error indicator", 3: "Not available"}
POSITIVE = {0: "Not on positive side of neutral", 1: "On positive side of neutral", 2: "Error indicator", 3: "Not available"}
BUTTON = {0: "Button not pressed", 1: "Button pressed", 2: "Error indicator", 3: "Not available"}


def _axis_signals(prefix: str, neg: str, pos: str, base_byte: int, spn: int | None):
    b = base_byte * 8
    return [
        (f"{prefix}_NeutralPosStatus", b + 0, 2, 1, "", NEUTRAL, None),
        (f"{prefix}_{neg}NegPosStatus", b + 2, 2, 1, "", NEGATIVE, None),
        (f"{prefix}_{pos}PosPosStatus", b + 4, 2, 1, "", POSITIVE, None),
        # 0..1000 counts = 0..100 % of travel; 1022 = error, 1023 = not available.
        (f"{prefix}_Position", b + 6, 10, 0.1, "%", None, spn),
    ]


def bjm_signals(with_spn: bool):
    sigs = _axis_signals("X", "LeverLeft", "LeverRight", 0, 2660 if with_spn else None)
    sigs += _axis_signals("Y", "LeverBack", "LeverForward", 2, 2661 if with_spn else None)
    sigs += [
        ("Y_DetentPosStatus", 4 * 8 + 4, 2, 1, "", None, None),
        ("X_DetentPosStatus", 4 * 8 + 6, 2, 1, "", None, None),
    ]
    # Each byte 5..7 holds four buttons, highest button number in the low bits.
    for byte, buttons in ((5, (4, 3, 2, 1)), (6, (8, 7, 6, 5)), (7, (12, 11, 10, 9))):
        for i, button in enumerate(buttons):
            sigs.append((f"Button{button}_PressedStatus", byte * 8 + 2 * i, 2, 1, "", BUTTON, None))
    return sigs


def ejm_signals(with_spn: bool):
    sigs = _axis_signals("GripX", "LeverLeft", "LeverRight", 0, 2662 if with_spn else None)
    sigs += _axis_signals("GripY", "LeverBack", "LeverForward", 2, 2663 if with_spn else None)
    sigs += [
        ("GripY_DetentPosStatus", 6 * 8 + 4, 2, 1, "", None, None),
        ("GripX_DetentPosStatus", 6 * 8 + 6, 2, 1, "", None, None),
    ]
    return sigs


# (name, PGN, signals, comment). Priority 3, 8 bytes, 20 ms for all.
J1939_MESSAGES = [
    ("BJM1", 0xFDD6, bjm_signals(True), "Basic Joystick Message 1"),
    ("EJM1", 0xFDD7, ejm_signals(True), "Extended Joystick Message 1"),
    ("BJM3", 0xFDDA, bjm_signals(False), "Basic Joystick Message 3"),
    ("EJM3", 0xFDDB, ejm_signals(False), "Extended Joystick Message 3"),
]
# The source address depends on the model code; 0xFE is a placeholder. The
# CanAnalyser decoder (and J1939-aware tools) match on PGN, not on SA.
PLACEHOLDER_SA = 0xFE
PRIORITY = 3


def make_dbc() -> str:
    lines = ['VERSION ""', "", "", "NS_ :", "", "BS_:", "", "BU_: JS1H", "", ""]
    attrs, vals, comments = [], [], []
    for name, pgn, signals, comment in J1939_MESSAGES:
        frame_id = (PRIORITY << 26) | (pgn << 8) | PLACEHOLDER_SA
        dbc_id = frame_id | 0x80000000  # extended-frame flag in DBC
        lines.append(f"BO_ {dbc_id} {name}: 8 JS1H")
        for sname, start, length, factor, unit, choices, spn in signals:
            maximum = (((1 << length) - 1) * factor) if not unit else 100
            lines.append(
                f' SG_ {sname} : {start}|{length}@1+ ({factor:g},0) [0|{maximum:g}] "{unit}" Vector__XXX'
            )
            if choices:
                pairs = " ".join(f'{v} "{t}"' for v, t in choices.items())
                vals.append(f"VAL_ {dbc_id} {sname} {pairs} ;")
            if spn:
                attrs.append(f'BA_ "SPN" SG_ {dbc_id} {sname} {spn};')
        lines.append("")
        comments.append(f'CM_ BO_ {dbc_id} "{comment}, PGN {pgn} (0x{pgn:04X}). SA in ID is a placeholder.";')
        attrs.insert(0, f'BA_ "VFrameFormat" BO_ {dbc_id} 3;')
        attrs.insert(0, f'BA_ "GenMsgCycleTime" BO_ {dbc_id} 20;')

    lines += [
        "",
        f'CM_ "JS1-H heavy duty joystick, SAE J1939 at 250 kbit/s. Generated from {SOURCE}.";',
        *comments,
        'BA_DEF_ SG_ "SPN" INT 0 524287;',
        'BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN","ExtendedCAN","reserved","J1939PG";',
        'BA_DEF_ BO_ "GenMsgCycleTime" INT 0 65535;',
        'BA_DEF_ "ProtocolType" STRING ;',
        'BA_DEF_DEF_ "SPN" 0;',
        'BA_DEF_DEF_ "VFrameFormat" "J1939PG";',
        'BA_DEF_DEF_ "GenMsgCycleTime" 0;',
        'BA_DEF_DEF_ "ProtocolType" "";',
        'BA_ "ProtocolType" "J1939";',
        *attrs,
        *vals,
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CANopen
# --------------------------------------------------------------------------

U8, U16, U32, S16, STR = 0x0005, 0x0006, 0x0007, 0x0003, 0x0009


class Var:
    def __init__(self, name, dtype, access, default=None, pdo=False, unit="", factor=None, desc=""):
        self.name, self.dtype, self.access, self.default = name, dtype, access, default
        self.pdo, self.unit, self.factor, self.desc = pdo, unit, factor, desc


def _fmt_default(value) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value:X}" if value > 9 else str(value)
    return str(value)


def _var_lines(section: str, var: Var, object_type: int = 0x7) -> list[str]:
    lines = [
        f"[{section}]",
        f"ParameterName={var.name}",
        f"ObjectType=0x{object_type:X}",
        f"DataType=0x{var.dtype:04X}",
        f"AccessType={var.access}",
        f"DefaultValue={_fmt_default(var.default)}",
        f"PDOMapping={1 if var.pdo else 0}",
    ]
    if var.unit:
        lines.append(f"Unit={var.unit}")
    if var.factor is not None:
        lines.append(f"Factor={var.factor:g}")
    if var.desc:
        lines.append(f"Description={var.desc}")
    return lines + [""]


def mapping(entries: list[int]) -> dict[int, Var]:
    subs = {0: Var("Number of mapped application objects", U8, "ro", len(entries))}
    for i, entry in enumerate(entries, start=1):
        subs[i] = Var(f"Mapping entry {i}", U32, "ro", entry)
    return subs


def tpdo_comm(n: int) -> dict[int, Var]:
    # "+ 230" in the manual is 2^30 (bit 30: RTR not allowed). Sub 4 is not
    # implemented on the device (SDO abort 06090011h), so it is left out.
    return {
        0: Var("Highest sub-index supported", U8, "const", 5),
        1: Var(f"COB-ID used by TPDO{n}", U32, "ro", f"$NODEID+0x{0x40000000 + 0x80 + 0x100 * n:X}"),
        2: Var("Transmission type", U8, "ro", 0xFE, desc="FEh: event driven (manufacturer specific)"),
        3: Var("Inhibit time", U16, "rw", 0, unit="100 us"),
        5: Var("Event timer", U16, "rw", 20, unit="ms"),
    }


def button_entries(pressed: range, enabled: range) -> tuple[list[int], list[int]]:
    return (
        [0x60200001 | (b << 8) for b in pressed],
        [0x60200001 | ((24 + b) << 8) for b in enabled],
    )


def canopen_objects(variant: str) -> dict[int, tuple[str, int, object]]:
    """Return {index: (name, object_type, Var | {sub: Var})}."""
    objs: dict[int, tuple[str, int, object]] = {
        0x1000: ("Device type", 0x7, Var("Device type", U32, "const", 0x01050191, desc="Joystick without digital outputs")),
        0x1001: ("Error register", 0x7, Var("Error register", U8, "ro", 0)),
        0x1003: (
            "Pre-defined error field",
            0x8,
            {0: Var("Log size", U8, "rw", 0), **{i: Var(f"Error {i} code", U32, "ro", 0) for i in range(1, 25)}},
        ),
        0x1008: ("Manufacturer device name", 0x7, Var("Manufacturer device name", STR, "const", "JOYSTICK")),
        0x1009: ("Manufacturer hardware version", 0x7, Var("Manufacturer hardware version", STR, "const", "")),
        0x100A: ("Manufacturer software version", 0x7, Var("Manufacturer software version", STR, "const", "")),
        0x1014: ("COB-ID EMCY", 0x7, Var("COB-ID EMCY", U32, "ro", "$NODEID+0x80")),
        # Doc: U16 with max 2^32-1 (typo); 20 ms default, takes effect after reboot.
        0x1017: ("Producer heartbeat time", 0x7, Var("Producer heartbeat time", U16, "rw", 20, unit="ms")),
        0x1018: (
            "Identity object",
            0x9,
            {
                0: Var("Highest sub-index supported", U8, "const", 4),
                1: Var("Vendor-ID", U32, "const", 0x19),
                2: Var("Product code", U32, "const", 0x1),
                3: Var("Revision number", U32, "const", 0x10001),
                4: Var("Serial number", U32, "const", 0),
            },
        ),
        0x1200: (
            "SDO server parameter",
            0x9,
            {
                0: Var("Highest sub-index supported", U8, "const", 2),
                1: Var("COB-ID client to server (rx)", U32, "ro", "$NODEID+0x600"),
                2: Var("COB-ID server to client (tx)", U32, "ro", "$NODEID+0x580"),
            },
        ),
    }

    if variant == "can":
        # 1A00h sub0 says 49 but 50 entries are listed; 50 entries = exactly 8 bytes.
        pressed, enabled = button_entries(range(1, 25), range(1, 25))
        tpdo1 = [0x60000108, *pressed, 0x60000208, *enabled]
        pdo_count = 3
    else:  # CAN+
        pressed, enabled = button_entries(range(1, 25), range(1, 13))
        tpdo1 = [0x60000108, *pressed[:12], 0x60000204, *pressed[12:], 0x60000204, *enabled]
        pdo_count = 4

    maps = {
        1: tpdo1,
        2: [0x64010110, 0x64010210, 0x64050110, 0x64040102, 0x64040202, 0x64060102],
        3: [0x64010310, 0x64010410, 0x64010510, 0x64040302, 0x64040402, 0x64040502],
        4: [0x64010610, 0x64010710, 0x64010810, 0x64010910],
    }
    for n in range(1, pdo_count + 1):
        objs[0x1800 + n - 1] = (f"TPDO{n} communication parameter", 0x9, tpdo_comm(n))
        objs[0x1A00 + n - 1] = (f"TPDO{n} mapping parameter", 0x9, mapping(maps[n]))

    objs[0x6000] = (
        "Read input 8-bit",
        0x8,
        {
            0: Var("Number of inputs 8-bit", U8, "ro", 2),
            1: Var("Proportional memory freeze", U8, "ro", 0, pdo=True, desc="Not supported"),
            2: Var("Reserved", U8, "ro", 0, pdo=True),
        },
    )
    # The manual's 6020h table puts "Freeze bit 1-8" at sub 1-8 and buttons at
    # 9-56, but every PDO mapping entry addresses pressed status at sub 1-24
    # and enabled status at sub 25-48. The PDO mapping is what is on the wire,
    # so the object is laid out to match it.
    objs[0x6020] = (
        "Read input bit (buttons)",
        0x8,
        {
            0: Var("Number of input bits", U8, "ro", 48),
            **{b: Var(f"Button {b} pressed", U8, "ro", 0, pdo=True, desc="0: not pressed, 1: pressed") for b in range(1, 25)},
            **{24 + b: Var(f"Button {b} enabled", U8, "ro", 0, pdo=True, desc="0: not enabled, 1: enabled") for b in range(1, 25)},
        },
    )
    axis_names = ["X-Axis", "Y-Axis", "Grip 1", "Grip 2", "Grip 3", "Axis 5", "Axis 6", "Axis 7", "Axis 8", "Axis 9"]
    if variant == "canplus":
        # TPDO4 names these mapped inputs after connector pins.
        for i, pin in zip(range(5, 9), ("X100p11", "X100p12", "X100p30", "X100p15")):
            axis_names[i] += f" ({pin})"
    objs[0x6401] = (
        "Read analog inputs 16-bit",
        0x8,
        {
            0: Var("Number of analog inputs 16-bit", U8, "ro", 10),
            **{
                i: Var(f"{name} proportional" if i <= 5 else name, S16, "ro", 0, pdo=True, unit="%", factor=0.1)
                for i, name in enumerate(axis_names, start=1)
            },
        },
    )
    objs[0x6404] = (
        "Manufacturer specific input 2-bit",
        0x8,
        {
            0: Var("Number of inputs 2-bit", U8, "ro", 10),
            **{
                i: Var(f"{name} neutral state", U8, "ro", 0, pdo=True,
                       desc="0: neutral, 1: out of neutral, 2: error, 3: not available")
                for i, name in enumerate(axis_names, start=1)
            },
        },
    )
    objs[0x6405] = (
        "Manufacturer specific reserved 16-bit",
        0x8,
        {0: Var("Number of entries", U8, "ro", 1), 1: Var("Reserved 1", U16, "ro", 0, pdo=True)},
    )
    objs[0x6406] = (
        "Manufacturer specific reserved 2-bit",
        0x8,
        {0: Var("Number of entries", U8, "ro", 1), 1: Var("Reserved 1", U8, "ro", 0, pdo=True)},
    )
    return objs


def make_eds(variant: str) -> str:
    objs = canopen_objects(variant)
    product = "JS1-H CAN / CANalog (CANopen)" if variant == "can" else "JS1-H CAN+ (CANopen)"
    mandatory = [i for i in (0x1000, 0x1001, 0x1018) if i in objs]
    optional = [i for i in objs if i < 0x2000 and i not in mandatory]
    manufacturer = [i for i in objs if 0x2000 <= i < 0x6000]
    profile = [i for i in objs if i >= 0x6000]
    optional += profile  # device profile objects are listed as optional objects
    tx_pdos = sum(1 for i in objs if 0x1800 <= i < 0x1A00)

    lines = [
        "[FileInfo]",
        f"FileName=js1h_{variant}.eds",
        "FileVersion=1",
        "FileRevision=0",
        "EDSVersion=4.0",
        f"Description=Reconstructed from {SOURCE}. Not an official Danfoss EDS.",
        "CreatedBy=CanAnalyser tools/make_js1h_databases.py",
        "",
        "[DeviceInfo]",
        "VendorName=Danfoss",
        "VendorNumber=0x19",
        f"ProductName={product}",
        "ProductNumber=0x1",
        "RevisionNumber=0x10001",
        "BaudRate_10=0",
        "BaudRate_20=0",
        "BaudRate_50=0",
        "BaudRate_125=1",
        "BaudRate_250=1",
        "BaudRate_500=1",
        "BaudRate_800=0",
        "BaudRate_1000=0",
        "SimpleBootUpMaster=0",
        "SimpleBootUpSlave=1",
        "Granularity=0",
        "DynamicChannelsSupported=0",
        "GroupMessaging=0",
        "NrOfRXPDO=0",
        f"NrOfTXPDO={tx_pdos}",
        "LSS_Supported=1",
        "",
        "[DummyUsage]",
        *[f"Dummy000{i}=0" for i in range(1, 8)],
        "",
        "[Comments]",
        "Lines=0",
        "",
    ]
    for title, indices in (("MandatoryObjects", mandatory), ("OptionalObjects", optional), ("ManufacturerObjects", manufacturer)):
        lines.append(f"[{title}]")
        lines.append(f"SupportedObjects={len(indices)}")
        lines += [f"{n}=0x{index:04X}" for n, index in enumerate(indices, start=1)]
        lines.append("")

    for index in sorted(objs):
        name, otype, body = objs[index]
        section = f"{index:04X}"
        if isinstance(body, Var):
            lines += _var_lines(section, body, otype)
            continue
        lines += [f"[{section}]", f"ParameterName={name}", f"ObjectType=0x{otype:X}", f"SubNumber={len(body)}", ""]
        for sub, var in sorted(body.items()):
            lines += _var_lines(f"{section}sub{sub:X}", var)
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "js1h_j1939.dbc").write_text(make_dbc(), newline="\r\n")
    (OUT / "js1h_can.eds").write_text(make_eds("can"), newline="\r\n")
    (OUT / "js1h_canplus.eds").write_text(make_eds("canplus"), newline="\r\n")
    print(f"Written to {OUT}")


if __name__ == "__main__":
    main()
