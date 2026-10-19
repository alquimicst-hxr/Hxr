import sqlite3
import threading

from tests.helpers import make_services


def test_init_is_idempotent(tmp_path):
    s = make_services(tmp_path)
    s.db.init()
    s.db.init()
    assert s.db.stats()["users"] == 0
    with s.db.connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("users", "admins", "plans", "subscriptions", "usage_counters", "messages", "groups",
              "group_topics", "settings", "admin_events", "audit_log", "user_blocks"):
        assert t in tables


def test_user_create_and_query(tmp_path):
    s = make_services(tmp_path)
    s.db.upsert_user(5, "ana", "Ana")
    s.db.upsert_user(5, "ana2", "Ana")
    u = s.db.get_user(5)
    assert u["username"] == "ana2" and s.db.count_users() == 1


def test_sql_injection_is_inert(tmp_path):
    s = make_services(tmp_path)
    s.db.upsert_user(6, "x'); DROP TABLE users;--", "n")
    assert s.db.get_user(6)["username"].startswith("x'")
    assert s.db.count_users() == 1


def test_block_unblock(tmp_path):
    s = make_services(tmp_path)
    s.db.block_user(7, "spam", 1)
    assert s.db.is_blocked(7)
    assert s.db.unblock_user(7) and not s.db.is_blocked(7)


def test_history_isolation_and_prune(tmp_path):
    s = make_services(tmp_path)
    s.db.add_messages(10, 0, 10, [("user", "a"), ("assistant", "b")])
    s.db.add_messages(-100, 0, 10, [("user", "grupo")])
    s.db.add_messages(-100, 5, 10, [("user", "topico5")])
    assert [r["content"] for r in s.db.recent_messages(10, 0, 10)] == ["a", "b"]
    assert [r["content"] for r in s.db.recent_messages(-100, 0, 10)] == ["grupo"]
    assert [r["content"] for r in s.db.recent_messages(-100, 5, 10)] == ["topico5"]
    s.db.add_messages(10, 0, 10, [("user", str(i)) for i in range(10)])
    s.db.prune_history(10, 0, 4)
    assert s.db.count_messages(10, 0) == 4
    assert s.db.count_messages(-100, 5) == 1
    s.db.clear_history(-100, 5)
    assert s.db.count_messages(-100, 5) == 0 and s.db.count_messages(-100, 0) == 1


def test_retention_purge(tmp_path):
    s = make_services(tmp_path)
    s.db.add_messages(1, 0, 1, [("user", "velha")])
    s.clock.t += 40 * 86400
    s.db.add_messages(1, 0, 1, [("user", "nova")])
    assert s.db.purge_older_than(30) == 1
    assert [r["content"] for r in s.db.recent_messages(1, 0, 10)] == ["nova"]


def test_set_group_field_whitelist(tmp_path):
    s = make_services(tmp_path)
    s.db.upsert_group(-5, "g")
    try:
        s.db.set_group_field(-5, "title; DROP TABLE groups", 1)
        assert False, "deveria falhar"
    except ValueError:
        pass


def test_concurrent_writers_do_not_lose_updates(tmp_path):
    s = make_services(tmp_path)
    errors = []

    def work(i):
        try:
            for _ in range(10):
                s.db.set_setting("k", str(i))
                s.db.add_messages(1, 0, i, [("user", "x")])
        except sqlite3.Error as e:  # noqa
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert s.db.count_messages(1, 0) == 60
