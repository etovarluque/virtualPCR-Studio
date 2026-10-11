"""Entry point of the packaged .exe: the same as `python -m vpcr`."""
from vpcr.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
