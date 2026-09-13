"""What a fetch returns, whichever engine did it.

Two engines, one shape: the caller must not have to know whether a page came
out of an HTTP request or a browser. Where the difference matters - a rendered
page has run its scripts, a fetched one has not - the answer carries `engine`
and the run report shows it, so an empty result can be traced to the site
rather than to the way it was read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: How a protection layer announces itself. Checked against the body of a 403
#: (and of a 200 that has nothing else in it): the status alone cannot tell a
#: challenge apart from an ordinary refusal, and the two need different
#: answers - a challenge means "this site wants a browser", a refusal means
#: "this address is not for you".
CHALLENGE_MARKERS = (
    "checking your browser", "just a moment", "enable javascript and cookies",
    "cf-browser-verification", "_cf_chl_opt", "cf_chl_", "ddos protection",
    "attention required", "captcha", "px-captcha", "verifying you are human",
)

_TAG = re.compile(r"<[^>]+>")


def looks_like_challenge(body: str) -> bool:
    """Is this body a bot check rather than a page?

    >>> looks_like_challenge("<h1>Checking your browser before access</h1>")
    True
    >>> looks_like_challenge("<h1>20 apartments in Zurich</h1>")
    False
    """
    text = _TAG.sub(" ", body or "")[:4000].lower()
    return any(marker in text for marker in CHALLENGE_MARKERS)


@dataclass(frozen=True)
class FetchResult:
    url: str
    #: The address after every redirect. It may differ from `url`, and then
    #: IT is the key for deduplication - otherwise the same page counts as two
    #: documents under two addresses.
    final_url: str
    status: int
    html: str
    content_type: str = ""
    #: Which engine produced this. Goes into the run report so that an empty
    #: result can be blamed on the site rather than on the browser.
    engine: str = ""
    elapsed_ms: int = 0
    bytes: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.html)


class FetchError(RuntimeError):
    """This one address could not be fetched.

    Always local: it concerns a URL, not the run. The caller logs it and
    carries on with the next address - except for `throttled`, which concerns
    the whole host and ends the run early.
    """

    def __init__(self, message: str, *, status: int = 0,
                 retry_after: float | None = None,
                 challenge: bool = False) -> None:
        super().__init__(message)
        self.status = status
        #: Seconds from the Retry-After header, when the site named one.
        self.retry_after = retry_after
        #: True when the answer was a bot check, not a refusal.
        self.challenge = challenge

    @property
    def throttled(self) -> bool:
        """Did the other side ask for room?

        This is NOT a failure of the address; it is a request for distance,
        and counting it as a failed attempt would eventually retire a
        perfectly healthy source over a few runs that were merely too quick.
        """
        return self.status in (429, 503)


class EngineUnavailable(FetchError):
    """The engine this source asks for is not in this image.

    Its own class because the answer is not "try again" but "build the image
    with the Playwright base, or switch the source to HTTP" - and the page
    that shows it says exactly that.
    """


__all__ = ["CHALLENGE_MARKERS", "EngineUnavailable", "FetchError", "FetchResult",
           "looks_like_challenge"]
