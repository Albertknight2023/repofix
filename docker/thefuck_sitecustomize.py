"""Restore the historical pytest marker API used by thefuck's old tests.

This lives only in the dedicated thefuck image; benchmark source and tests are
not rewritten. Old get_marker() looked up the nearest marker, matching the
modern get_closest_marker() method used below.
"""

import pytest


if not hasattr(pytest.Item, "get_marker"):
    pytest.Item.get_marker = pytest.Item.get_closest_marker
