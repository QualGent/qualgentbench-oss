from qualgentbench.failures import RATE_LIMITED, classify, exclusion_reason, is_excluded, retry_after_seconds


def test_a_limit_at_the_end_of_the_transcript_is_the_stop_reason():
    transcript = "x" * 10_000 + '{"type":"error","error":{"type":"rate_limit_error"}}'
    assert classify(transcript, 0) == RATE_LIMITED


def test_a_retried_blip_in_a_finished_episode_is_not_a_failure():
    transcript = '{"error":{"type":"rate_limit_error"}}' + "tool call " * 5000
    assert classify(transcript, 0, {"device_actions": 120}) is None


def test_an_early_limit_plus_a_dead_episode_is_rate_limited():
    transcript = "429 Too Many Requests" + "noise " * 5000
    assert classify(transcript, 1) == RATE_LIMITED
    assert classify(transcript, 0, {"env_failure": True}) == RATE_LIMITED


def test_codex_and_overload_spellings():
    for tail in ("rate_limit_exceeded", "HTTP 529 Overloaded", "status: 429",
                 "Rate limit reached for gpt-5.5", "overloaded_error"):
        assert classify("…" + tail, 0) == RATE_LIMITED, tail


def test_unrelated_failures_are_not_reclassified():
    assert classify("Traceback: ValueError", 1, {"env_failure": True}) is None
    assert classify("", 1) is None


def test_exclusion_predicate_covers_rate_limits():
    assert is_excluded({"failure_class": RATE_LIMITED})
    assert is_excluded({"infra_failure": True})
    assert not is_excluded({"truncated": True, "f1": 0.2})
    assert exclusion_reason({"failure_class": RATE_LIMITED}).startswith("rate_limited")
    assert exclusion_reason({"f1": 0.2}) == ""


def test_bedrock_and_claude_code_api_throttling_is_rate_limited():
    # codex on amazon-bedrock-runtime, claude-code on Bedrock, claude-code's API error line, Fireworks.
    for tail in ('{"type":"error","message":"ThrottlingException: Rate exceeded"}',
                 "API Error: 429 Too many tokens, please wait before trying again.",
                 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
                 '{"type":"turn.failed","error":{"message":"exceeded retry limit, last status: 429"}}'):
        assert classify("x" * 9000 + tail, 1) == RATE_LIMITED, tail


def test_retry_after_is_read_from_the_last_mention():
    assert retry_after_seconds("429 Too Many Requests; retry-after: 20") == 20
    assert retry_after_seconds('{"retry_after_ms": 1500}') == 1.5
    assert retry_after_seconds("Rate limit reached. Please try again in 12s.") == 12
    assert retry_after_seconds("please retry after 2 minutes") == 120
    assert retry_after_seconds("retry-after: 5 ... later: Retry-After: 30") == 30
    assert retry_after_seconds("ThrottlingException: Too many tokens") is None
    assert retry_after_seconds("") is None
