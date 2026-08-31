"""The exception base this package raises deliberately.

Imports nothing from the package, so `api`, `auth` and the tools can all raise
from it without a cycle.
"""


class GoogleHealthError(Exception):
    """An error this package raises on purpose, with text written for the model.

    `require_auth` converts these into `ToolError`, whose message `mcp` keeps
    on the wire; every other exception reaches the client as `Error executing
    tool <name>`. Anything not descended from this is treated as unplanned,
    and its text is what must not travel: a filesystem error on the token file
    or the cache names an absolute path, which is measured rather than assumed
    and is what the masking test raises.

    Membership is not a promise about the message. What each raise site may
    say is governed by the leak rules in AGENTS.md, and `TokenRefused` carries
    a resolved path in one message, which `google_get` keeps off the wire by
    catching that type.
    """


class InvalidDateError(GoogleHealthError, ValueError):
    """A date argument the tools cannot parse.

    Keeps `ValueError` so `parse_date`'s existing contract is unchanged for
    any caller catching it, which `test_invalid_format_raises` and
    `test_invalid_relative_raises` pin.
    """


class LiveRefreshFailed(GoogleHealthError, RuntimeError):
    """A `live=True` query could not refresh the window it was asked for.

    Keeps `RuntimeError` because `tests/test_refresh_before_query.py` catches
    that. Its message carries the data type and the sync's status word, never
    the notes, since these paths handle API responses.
    """
