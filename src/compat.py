"""Compatibility patch for Python versions earlier than 3.11.

Ensures `typing.Self` exists to avoid ImportError when third-party libraries
(such as neonize) import `Self` directly from `typing`.
"""

import sys
import typing

if sys.version_info < (3, 11):
    try:
        import typing_extensions

        if not hasattr(typing, "Self"):
            typing.Self = typing_extensions.Self
    except ImportError:
        pass
