"""In silico PCR over FASTA targets, driving the virtualPCR JAR."""
import sys
from pathlib import Path

__version__ = "1.0.0"

APP_NAME = "virtualPCR Studio"

# Folder holding Run vPCR.bat, README.md and jar/. Used to locate the JAR.
# In the packaged .exe the code lives in a bundle folder; jar/ and output/ sit
# beside the executable instead.
APP_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)
JAR_DIR = APP_ROOT / "jar"
