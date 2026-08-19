"""Market digest service: scheduled daily market summary generation.

Step 1: config + data models + async harness tools.
Step 2: agent + generation service (Redis persistence, down-tolerant).
"""

from src.services.digest.models import Digest, DigestSection
from src.services.digest.service import generate_digest

__all__ = ["Digest", "DigestSection", "generate_digest"]
