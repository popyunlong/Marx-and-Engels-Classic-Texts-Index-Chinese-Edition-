"""Isolated accounts/corpus for the homepage access boundary, never production data."""
from datetime import timedelta

from membership_refresh_fixture import A, M, user, login
from chapter_fixture import make_corpus


def account(kind):
    u = user("support_plus_20" if kind in {"member", "expired"} else None)
    if kind == "expired":
        with M._connect() as connection:
            connection.execute("UPDATE subscriptions SET expires_at=? WHERE user_id=?",
                               ((M.utc_now() - timedelta(days=1)).isoformat(), u["id"]))
            connection.commit()
    if kind == "disabled":
        with M._connect() as connection:
            connection.execute("UPDATE users SET is_active=0 WHERE id=?", (u["id"],))
            connection.commit()
    return u


def install_corpus(monkeypatch):
    corpus, toc = make_corpus()
    monkeypatch.setattr(A, "corpus", corpus)
    monkeypatch.setattr(A, "_public_book_keys", lambda **kwargs: set(corpus.books))
    monkeypatch.setattr(A, "_chapter_scope_version", lambda: "fuzzy-v1")
    monkeypatch.setattr(A, "_record_community_trend", lambda *a, **k: None)
    return corpus, toc
