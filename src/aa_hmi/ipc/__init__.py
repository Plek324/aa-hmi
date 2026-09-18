"""ipc -- the local client-program <-> aa-hmi-daemon socket protocol.

See docs/ipc-protocol.md for the full, language-agnostic wire spec. This
package is the reference (Python) implementation of that spec: protocol.py
is the wire codec, server.py is the daemon side, client.py is what any
Python client program (including the example apps in examples/) imports.
"""
