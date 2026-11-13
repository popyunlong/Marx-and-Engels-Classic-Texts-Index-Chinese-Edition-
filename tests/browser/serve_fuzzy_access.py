"""Offline real HTTP fixture for the homepage login/member boundary."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import conftest
from fuzzy_access_fixture import A, M, account, install_corpus
from flask import session, redirect, jsonify
from werkzeug.serving import make_server
from search import Volume, TocEntry, Page
from build_index import normalize
from site_content import update_site_text_overrides
from datetime import timedelta
import urllib.request
import smtplib


class Patch:
    setattr = staticmethod(setattr)


corpus, toc = install_corpus(Patch())
raw = corpus.books["文集"][0].pages[0].raw_text
for number in range(3, 25):
    source = f"pdfs/fixture/文集-{number}.pdf"
    volume = Volume.build("文集", number, source, f"文集第{number}卷",
                          [Page(1, "10", raw, normalize(raw), 200 + number)])
    corpus.books["文集"].append(volume)
    corpus._volumes_by_source_file[source] = volume
    toc[source] = list(toc[corpus.books["文集"][0].source_file])

users = {kind: account(kind) for kind in ("registered", "member", "expired", "blocked")}
original_policy = A._load_access_policy
def policy():
    value = original_policy()
    value["users"][users["blocked"]["email"]] = {"search": False}
    return value
A._load_access_policy = policy
A.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
update_site_text_overrides({"search.fuzzy_no_access": "OLD_PAYWALL", "search.assoc_no_ai": "OLD_AI_PERMISSION"})

def denied(*a, **k):
    raise AssertionError("Unexpected external service in fuzzy browser fixture")
urllib.request.urlopen = denied
smtplib.SMTP = denied
A.AI_CLIENT.expand_associative_query = denied
A.AI_CLIENT.rank_associative_candidates = denied


@A.app.get("/fixture-login/<kind>")
def fixture_login(kind):
    session.clear()
    if kind != "guest":
        session["user_id"] = users[kind]["id"]
    return redirect("/")


@A.app.get("/fixture-expire")
def fixture_expire():
    with M._connect() as connection:
        connection.execute("UPDATE subscriptions SET expires_at=? WHERE user_id=?",
                           ((M.utc_now() - timedelta(days=1)).isoformat(), session["user_id"]))
        connection.commit()
    return redirect("/")


@A.app.get("/fixture-identity")
def identity():
    return jsonify(uid=session.get("user_id", ""))


if __name__ == "__main__":
    server = make_server("127.0.0.1", 0, A.app, threaded=True)
    print(f"READY=http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
