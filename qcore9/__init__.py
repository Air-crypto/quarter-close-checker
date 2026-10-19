"""qcore9: tier 1 checking for any document set whose numbers obey linear identities.

    import qcore9 as qcore
    result = qcore.run(bundle_dir, extraction, page_text_mode="text")   # or "notext"
    card = qcore.score(bundle_dir, extraction, result)

See README.md for the bundle format and the algorithm.
"""

from .core import run  # noqa: F401
from .score import score  # noqa: F401
from .bundle import load as load_bundle, parse_identities  # noqa: F401
