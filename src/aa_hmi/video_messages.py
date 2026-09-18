"""video_messages.py -- channel numbers and message IDs for the TCP/TLS
Android-Auto-Wireless video session (distinct from messages.py's Bluetooth
RFCOMM bootstrap protocol -- this is the OTHER protocol this project now
speaks, over WiFi/TCP port 29880 rather than classic Bluetooth).

Confirmed by direct capture+decrypt against a real TF811BT/Podofo display
in the aa_pi2display sibling project (aa_session.py, real-protocol-findings.md)
-- ported here verbatim as constants, not re-derived. See
docs/video-protocol-notes.md for the full writeup.
"""

DISPLAY_PORT = 29880

CHANNEL_CONTROL = 0
CHANNEL_INPUT = 1
CHANNEL_VIDEO = 3

MSG_VERSION_RESPONSE = 2
MSG_SSL_HANDSHAKE = 3
MSG_SERVICE_DISCOVERY_REQUEST = 5
MSG_SHUTDOWN_REQUEST = 0x000F

AV_MEDIA_WITH_TIMESTAMP_INDICATION = 0x0000
AV_SETUP_REQUEST = 0x8000
AV_STOP_INDICATION = 0x8002
VIDEO_FOCUS_REQUEST = 0x8007
INPUT_EVENT_INDICATION = 0x8001

# Wire-frame flags byte: 0x0F marks a channel-open frame (confirmed from a
# real phone's own capture), 0x0B is normal per-channel traffic thereafter.
FLAGS_CHANNEL_OPEN = 0x0F
FLAGS_NORMAL = 0x0B
