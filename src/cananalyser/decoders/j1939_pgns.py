"""Names for common J1939 parameter groups.

Used so the trace stays readable when the DBC file is missing a message.
Entries in a loaded DBC take precedence.
"""

PGN_NAMES: dict[int, str] = {
    0: "TSC1 Torque/Speed Control 1",
    59392: "ACKM Acknowledgment",
    59904: "RQST Request",
    60160: "TP.DT Transport Data",
    60416: "TP.CM Transport Connection Mgmt",
    60928: "AC Address Claimed",
    61440: "ERC1 Electronic Retarder Controller 1",
    61441: "EBC1 Electronic Brake Controller 1",
    61442: "ETC1 Electronic Transmission Controller 1",
    61443: "EEC2 Electronic Engine Controller 2",
    61444: "EEC1 Electronic Engine Controller 1",
    61445: "ETC2 Electronic Transmission Controller 2",
    64965: "ECUID ECU Identification",
    65213: "FD Fan Drive",
    65226: "DM1 Active Diagnostic Trouble Codes",
    65227: "DM2 Previously Active DTCs",
    65228: "DM3 Diagnostic Data Clear",
    65235: "DM11 Diagnostic Data Clear Active",
    65242: "SOFT Software Identification",
    65247: "EEC3 Electronic Engine Controller 3",
    65248: "VD Vehicle Distance",
    65249: "RC Retarder Configuration",
    65251: "EC1 Engine Configuration 1",
    65253: "HOURS Engine Hours, Revolutions",
    65254: "TD Time/Date",
    65257: "LFC Fuel Consumption (Liquid)",
    65259: "CI Component Identification",
    65260: "VI Vehicle Identification",
    65262: "ET1 Engine Temperature 1",
    65263: "EFL/P1 Engine Fluid Level/Pressure 1",
    65265: "CCVS Cruise Control/Vehicle Speed",
    65266: "LFE Fuel Economy (Liquid)",
    65269: "AMB Ambient Conditions",
    65270: "IC1 Inlet/Exhaust Conditions 1",
    65271: "VEP1 Vehicle Electrical Power 1",
    65272: "TRF1 Transmission Fluids 1",
    65276: "DD Dash Display",
}

PGN_REQUEST = 59904
PGN_TP_DT = 60160
PGN_TP_CM = 60416
PGN_ADDRESS_CLAIMED = 60928
PGN_DM1 = 65226
PGN_DM2 = 65227
