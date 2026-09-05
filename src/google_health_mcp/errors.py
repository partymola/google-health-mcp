"""The exception base this package raises deliberately.

Imports nothing from the package, so `api`, `auth` and the tools can all raise
from it without a cycle.
"""


class GoogleHealthError(Exception):
    """An error this package raises on purpose, with text written for the model.

    `wants_live_hint` asks `require_auth` to append the advice about re-fetching
    a window, which is worded one way normally and another in offline mode. The
    raise site therefore never writes that sentence itself: a refusal is raised
    rather than returned, so it does not pass the response path that corrects
    the hint, and a raise site holding its own copy would be corrected only
    where someone remembered to. The hint is appended after a space, so a
    message that asks for one ends in a full stop. Reading the attribute is
    safe on any caught instance because every class here descends from this
    one, which `test_every_exception_this_package_defines_reaches_the_model`
    pins for its own reasons.

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

    wants_live_hint = False


class InvalidDateError(GoogleHealthError, ValueError):
    """A date argument the tools cannot parse.

    Keeps `ValueError` so `parse_date`'s existing contract is unchanged for
    any caller catching it, which `test_invalid_format_raises` and
    `test_invalid_relative_raises` pin.
    """


class UnknownExerciseType(GoogleHealthError):
    """A workout filter matching no name the cache holds.

    Google names the workouts, so what a caller may ask for is whatever the
    cache holds rather than a set this package could declare in a schema.
    Its message names those, which is why it must reach the model, and it
    says the cache rather than the person: a name absent from it can be a
    window that was never synced, which is also why it asks for the hint.
    """

    wants_live_hint = True


class LiveRefreshFailed(GoogleHealthError, RuntimeError):
    """A `live=True` query could not refresh the window it was asked for.

    Keeps `RuntimeError` because `tests/test_refresh_before_query.py` catches
    that. Its message carries the data type and the sync's status word, never
    the notes, since these paths handle API responses.
    """
