"""ScienceBoard recording and analysis utilities.

The package is intentionally importable without VMware, ScienceBoard, Xlib,
NumPy, or scikit-learn. Host-side planning, validation, feature extraction,
and tests therefore run in a clean clone; optional runtime dependencies are
loaded only by the guest recorder or a formal run.
"""

PROTOCOL_VERSION = "scienceboard-matched-traces-v2"
