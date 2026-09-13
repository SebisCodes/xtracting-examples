"""Batching and triage - the two places where money is at stake.

A wrong batch is refused by the API and costs a round. A wrong triage costs
more: a 402 counted as a failure retires healthy documents, and a retried
"202 without jobId" pays for the same extraction twice.
"""

from __future__ import annotations

import re

import pytest

from app.store import new_tag
from app.submit import ApiError, Batch, Client, build_batches, explain
from crawlkit.tests.standin.fake_xtracting import FakeXtracting

#: The CHECK in database/init/03-scraper.sql. The tag has to satisfy it, and
#: the reason it exists is the auto-split suffix.
TAG_CHECK = re.compile(r"^[A-Za-z0-9_]+$")


def rows(*specs):
    return [{"text_tag": tag, "text_source_uri": uri, "content": content}
            for tag, uri, content in specs]


# -- the tag -----------------------------------------------------------

def test_the_tag_carries_the_source_and_no_hyphen():
    """On a split the platform appends `-01`. A hyphen inside the tag would
    make the suffix unparseable, and reconcile could no longer tell which
    archived task belongs to which submission."""
    tag = new_tag(42)
    assert tag.startswith("xs_42_")
    assert "-" not in tag
    assert TAG_CHECK.match(tag)


def test_two_tags_are_never_the_same():
    assert len({new_tag(1) for _ in range(500)}) == 500


# -- the four limits ---------------------------------------------------

def test_the_task_cap_splits_a_long_queue():
    queue = rows(*[(f"xs_1_{n:012x}", f"https://x/{n}", "text") for n in range(25)])
    batches, rejected = build_batches(queue, max_tasks=10, max_bytes=10_000_000,
                                      max_chars=1000)
    assert [len(batch) for batch in batches] == [10, 10, 5]
    assert rejected == []


def test_the_byte_cap_splits_before_the_gateway_does():
    """5 MB is the limit of the gateway in front of the API, and above it the
    answer is an HTML error page - which is not a message anyone can act on."""
    queue = rows(*[(f"xs_1_{n:012x}", f"https://x/{n}", "x" * 400) for n in range(10)])
    batches, _ = build_batches(queue, max_tasks=1000, max_bytes=1200, max_chars=10_000)
    assert len(batches) > 1
    assert all(batch.bytes_used <= 1200 for batch in batches)


def test_content_that_is_too_long_is_rejected_not_truncated():
    """A shortened document would look complete in the archive - and nobody
    would ever find out which half is missing."""
    queue = rows(("xs_1_aaaaaaaaaaaa", "https://x/1", "x" * 2000))
    batches, rejected = build_batches(queue, max_tasks=10, max_bytes=100_000,
                                      max_chars=1000)
    assert batches == []
    assert rejected[0].tag == "xs_1_aaaaaaaaaaaa"
    assert "NOT truncated" in rejected[0].reason


def test_empty_content_is_rejected_with_a_reason():
    """An empty task is billed and yields nothing."""
    queue = rows(("xs_1_bbbbbbbbbbbb", "https://x/1", "   \n "))
    batches, rejected = build_batches(queue, max_tasks=10, max_bytes=100_000,
                                      max_chars=1000)
    assert batches == [] and "empty" in rejected[0].reason


def test_a_single_document_larger_than_the_request_is_rejected_not_looped():
    """Leaving it in the queue would mean claiming it, failing, and counting an
    attempt on every round until it is retired - without the reason ever being
    written down anywhere."""
    queue = rows(("xs_1_cccccccccccc", "https://x/1", "x" * 5000))
    batches, rejected = build_batches(queue, max_tasks=10, max_bytes=1000,
                                      max_chars=1_000_000)
    assert batches == [] and "request limit" in rejected[0].reason


def test_the_same_source_twice_goes_into_two_batches():
    """Our own rule, not the API's: answers are attributed by tag, and on a
    split the tag gains a suffix. Two items with the same source in one batch
    would be indistinguishable exactly when it matters."""
    queue = rows(("xs_1_dddddddddddd", "https://x/1", "a"),
                 ("xs_1_eeeeeeeeeeee", "https://x/1", "b"),
                 ("xs_1_ffffffffffff", "https://x/2", "c"))
    batches, _ = build_batches(queue, max_tasks=100, max_bytes=100_000,
                               max_chars=1000)
    # The repeat closes the first batch; what comes after it joins the second.
    assert [len(batch) for batch in batches] == [1, 2]
    assert batches[0].tags == ["xs_1_dddddddddddd"]
    assert batches[1].tags == ["xs_1_eeeeeeeeeeee", "xs_1_ffffffffffff"]


def test_the_task_carries_content_source_and_tag_and_nothing_else():
    """The API's schema. An extra key is a 400 for the whole batch."""
    queue = rows(("xs_1_111111111111", "https://x/1", "text"))
    batches, _ = build_batches(queue, max_tasks=10, max_bytes=100_000, max_chars=100)
    assert set(batches[0].tasks[0]) == {"content", "source", "tag"}


# -- the triage --------------------------------------------------------

@pytest.fixture()
def api():
    with FakeXtracting() as fake:
        yield fake


def test_a_batch_comes_back_with_a_job_id(api):
    with Client(api.url, "test-key") as client:
        result = client.submit_batch([{"content": "hello", "source": "https://x/1",
                                       "tag": "xs_1_222222222222"}])
    assert result.job_id and result.total_tasks == 1


def test_the_split_is_reported_as_more_tasks_than_documents(api):
    """`totalTasks` larger than the batch means the platform cut the content -
    and the parts come back tagged `-1`, `-2`."""
    api.state.split_at = 10
    with Client(api.url, "test-key") as client:
        result = client.submit_batch([{"content": "x" * 35, "source": "https://x/1",
                                       "tag": "xs_1_333333333333"}])
    assert result.total_tasks == 4
    assert api.tags() == ["xs_1_333333333333-01", "xs_1_333333333333-02",
                          "xs_1_333333333333-03", "xs_1_333333333333-04"]


@pytest.mark.parametrize("status", [402, 429])
def test_backpressure_is_a_state_not_a_failure(api, status):
    api.state.next_submit_status = status
    with Client(api.url, "test-key") as client:
        with pytest.raises(ApiError) as raised:
            client.submit_batch([{"content": "a", "source": "https://x/1",
                                  "tag": "xs_1_444444444444"}])
    assert raised.value.is_backpressure and not raised.value.is_permanent


@pytest.mark.parametrize("status", [400, 401, 403, 451])
def test_a_permanent_refusal_is_not_retried(api, status):
    api.state.next_submit_status = status
    with Client(api.url, "test-key") as client:
        with pytest.raises(ApiError) as raised:
            client.submit_batch([{"content": "a", "source": "https://x/1",
                                  "tag": "xs_1_555555555555"}], retries=3)
    assert raised.value.is_permanent
    # One attempt, not three: the answer would be the same every time.
    assert len(api.state.submissions) == 1


def test_a_temporary_failure_is_retried_and_then_works(api):
    api.state.next_submit_status = [500, 502]
    with Client(api.url, "test-key") as client:
        result = client.submit_batch([{"content": "a", "source": "https://x/1",
                                       "tag": "xs_1_666666666666"}],
                                     retries=3, backoff=1.0)
    assert result.job_id
    assert len(api.state.submissions) == 3


def test_a_202_without_a_job_id_is_an_error_and_says_why(api):
    """Submitted, billed, and not attributable. Retrying it would pay twice
    for certain, so it is not retried - it is made visible."""
    api.state.next_submit_status = 202
    with Client(api.url, "test-key") as client:
        with pytest.raises(ApiError) as raised:
            client.submit_batch([{"content": "a", "source": "https://x/1",
                                  "tag": "xs_1_777777777777"}])
    assert raised.value.status == 202
    assert "cannot be attributed" in str(raised.value)
    assert not raised.value.is_permanent  # it gets its own branch in the triage


def test_a_bad_key_is_a_401(api):
    with Client(api.url, "bad") as client:
        with pytest.raises(ApiError) as raised:
            client.submit_batch([{"content": "a", "source": "https://x/1",
                                  "tag": "xs_1_888888888888"}])
    assert raised.value.status == 401 and raised.value.is_permanent


def test_the_message_explains_the_codes_that_really_occur():
    assert "balance" in explain(402)
    assert "XTRACTING_API_KEYS" in explain(401)
    assert explain(418) == ""


def test_an_empty_batch_is_never_sent(api):
    """`build_batches` never produces one, and this is the guard that says so
    out loud: the API answers 400 to an empty task list."""
    batches, _ = build_batches([], max_tasks=10, max_bytes=1000, max_chars=100)
    assert batches == []
    assert Batch().tasks == []
