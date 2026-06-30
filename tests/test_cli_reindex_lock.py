"""Coverage for the lock guard on `flask reindex-embeddings` (audit round 4, A3-C2).

The CLI must acquire the same Redis lock as the RQ reindex task so two
operators (CLI + admin UI) can't race on Chroma writes. --force lets an
operator override a stale lock; the lock is always released in finally
so a Ctrl-C or crash doesn't permanently jam future runs.
"""
from unittest.mock import MagicMock, patch


def test_reindex_cli_refuses_when_lock_held(app):
    """Lock already held → CLI exits 1 without touching the corpus."""
    runner = app.test_cli_runner()
    fake_redis = MagicMock()
    # SET NX returns False → another reindex is in progress.
    fake_redis.set.return_value = False

    with patch.dict("os.environ", {"REDIS_URL": "redis://fake/0"}), \
         patch("redis.Redis.from_url", return_value=fake_redis), \
         patch("app.services.rag_service._embed") as emb:
        result = runner.invoke(args=["reindex-embeddings"])

    assert result.exit_code == 1
    assert "another reindex is in progress" in result.output
    emb.assert_not_called()
    fake_redis.delete.assert_not_called()  # we never owned the lock


def test_reindex_cli_acquires_lock_and_releases(app, db):
    """Happy path: SET NX wins, reindex runs (no docs in fixture so it's a
    fast no-op), DELETE is called in finally."""
    runner = app.test_cli_runner()
    fake_redis = MagicMock()
    fake_redis.set.return_value = True  # NX wins → first runner

    with patch.dict("os.environ", {"REDIS_URL": "redis://fake/0"}), \
         patch("redis.Redis.from_url", return_value=fake_redis):
        result = runner.invoke(args=["reindex-embeddings"])

    assert result.exit_code == 0, result.output
    # Lock was acquired then released.
    assert any(
        call.args and call.args[0] == "myeline:embed_reindex_lock"
        and call.kwargs.get("nx") is True
        for call in fake_redis.set.call_args_list
    )
    fake_redis.delete.assert_called_with("myeline:embed_reindex_lock")


def test_reindex_cli_force_overrides_held_lock(app, db):
    """--force replaces the lock value via SET (no NX) so the run can proceed."""
    runner = app.test_cli_runner()
    fake_redis = MagicMock()
    # SET without NX always returns True (matches Redis semantics);
    # behaviour-wise the CLI must consider the lock as owned.
    fake_redis.set.return_value = True

    with patch.dict("os.environ", {"REDIS_URL": "redis://fake/0"}), \
         patch("redis.Redis.from_url", return_value=fake_redis):
        result = runner.invoke(args=["reindex-embeddings", "--force"])

    assert result.exit_code == 0, result.output
    # The first .set() call uses the forced path (no nx=True).
    first_set = fake_redis.set.call_args_list[0]
    assert first_set.args[0] == "myeline:embed_reindex_lock"
    assert first_set.kwargs.get("nx") is None or first_set.kwargs.get("nx") is False
    fake_redis.delete.assert_called_with("myeline:embed_reindex_lock")


def test_reindex_cli_proceeds_without_redis(app, db):
    """REDIS_URL missing → CLI warns and runs anyway (sovereign edge case)."""
    runner = app.test_cli_runner()

    # The conftest may set REDIS_URL globally; patch os.getenv so the
    # CLI sees REDIS_URL as unset specifically.
    real_getenv = __import__("os").getenv

    def _faux_getenv(name, default=None):
        if name == "REDIS_URL":
            return None
        return real_getenv(name, default)

    with patch("os.getenv", side_effect=_faux_getenv):
        result = runner.invoke(args=["reindex-embeddings"])
    assert result.exit_code == 0
    assert "REDIS_URL not set" in result.output


def test_reindex_cli_redis_failure_proceeds_without_lock(app, db):
    """Redis unreachable → warn, proceed without serialisation, never DELETE
    (we don't own the lock)."""
    runner = app.test_cli_runner()
    with patch.dict("os.environ", {"REDIS_URL": "redis://fake/0"}), \
         patch("redis.Redis.from_url", side_effect=RuntimeError("redis down")):
        result = runner.invoke(args=["reindex-embeddings"])
    assert result.exit_code == 0
    assert "Could not contact Redis" in result.output
