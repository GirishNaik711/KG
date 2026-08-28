"""cloudless.testing — pytest helpers, fixtures, and cassettes (Q25).

Surface:
  - llm_cassette(path)         — record / replay real Bedrock calls
  - record/replay shortcuts
"""
from cloudless.testing.cassettes import (
    CassetteMode,
    llm_cassette,
    set_default_mode,
)

__all__ = ["CassetteMode", "llm_cassette", "set_default_mode"]
