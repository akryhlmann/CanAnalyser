CanAnalyser - J1939 / CANopen monitor
=====================================

1. Install "Kvaser Drivers for Windows" from kvaser.com (once per PC).
2. Unzip this folder anywhere (e.g. Documents\CanAnalyser) and run CanAnalyser.exe.
   Keep the files in the folder together; the .exe needs them.
3. Pick protocol, the Kvaser channel (e.g. "Kvaser USBcan Light 2xHS ... ch 1"),
   and bitrate, then Connect.

Database files (DBC for J1939, EDS for CANopen) are in the "databases" folder.
Load them with "Load DBC..." / "Load EDS..." in the toolbar. You can add your own
files to that folder.

Choose "Simulator" as the interface to try the app without hardware.

If nothing is received: check the status bar. It warns about a wrong bitrate,
a Kvaser *virtual* channel, or listen-only mode.
