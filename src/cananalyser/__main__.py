from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cananalyser", description="J1939 / CANopen bus monitor")
    parser.add_argument("--dbc", action="append", default=[], help="J1939 DBC file to load (repeatable)")
    parser.add_argument(
        "--eds", action="append", default=[], metavar="FILE:NODE",
        help="EDS/DCF file and node ID, e.g. drive.eds:5 (repeatable)",
    )
    parser.add_argument("--protocol", choices=["j1939", "canopen"], help="preselect protocol")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from PySide6.QtWidgets import QApplication

    from cananalyser.gui.main_window import CANOPEN, J1939, MainWindow

    app = QApplication(sys.argv[:1])
    window = MainWindow()
    for path in args.dbc:
        window.j1939.load_dbc(path)
        window._loaded_files.append(path)
    for spec in args.eds:
        path, _, node = spec.rpartition(":")
        window.canopen.load_eds(path, int(node))
        window._loaded_files.append(path)
    if args.protocol:
        window.protocol.setCurrentText(J1939 if args.protocol == "j1939" else CANOPEN)
    elif args.eds and not args.dbc:
        window.protocol.setCurrentText(CANOPEN)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
