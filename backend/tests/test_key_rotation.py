"""Master key rotation (docs/kubernetes.md §10.1): ``SecretBox`` with previous keys,
``exradius rotate-key`` and download signatures across a rotation."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from exradius import downloads
from exradius.api.app import create_app
from exradius.config import Settings
from exradius.crypto import SecretBox, generate_secret_key
from exradius.key_rotation import encrypted_columns, rotate_encrypted
from exradius.models import GitCredential, Provider, RoutingSettings, SecretStore, SecretStoreEntry
from tests.test_api import FakeControl, _startup_done


def fernet_decrypt(key: str, token: str) -> str:
    """``token`` decrypted with ``key`` alone, independent of ``SecretBox``."""
    return Fernet(key.encode()).decrypt(token.encode()).decode()


# SecretBox with previous keys ----------------------------------------------------------------


def test_secret_box_encrypts_with_the_primary_key_only():
    primary, old = generate_secret_key(), generate_secret_key()
    token = SecretBox(primary, [old]).encrypt("s3cret")

    assert fernet_decrypt(primary, token) == "s3cret"
    with pytest.raises(Exception):
        fernet_decrypt(old, token)
    assert SecretBox(primary).decrypt(token) == "s3cret"  # needs no previous key


def test_secret_box_decrypts_values_under_the_primary_and_every_previous_key():
    primary, old, older = generate_secret_key(), generate_secret_key(), generate_secret_key()
    box = SecretBox(primary, [old, older])

    assert box.decrypt(SecretBox(primary).encrypt("a")) == "a"
    assert box.decrypt(SecretBox(old).encrypt("b")) == "b"
    assert box.decrypt(SecretBox(older).encrypt("c")) == "c"


def test_secret_box_without_previous_keys_cannot_read_values_under_an_old_key():
    primary, old = generate_secret_key(), generate_secret_key()
    token = SecretBox(old).encrypt("s3cret")

    with pytest.raises(RuntimeError, match="cannot decrypt secret"):
        SecretBox(primary).decrypt(token)


def test_secret_box_fails_cleanly_for_a_value_under_an_unknown_key():
    primary, old, unknown = generate_secret_key(), generate_secret_key(), generate_secret_key()
    box = SecretBox(primary, [old])
    token = SecretBox(unknown).encrypt("s3cret")

    with pytest.raises(RuntimeError, match="cannot decrypt secret") as decrypt_error:
        box.decrypt(token)
    with pytest.raises(RuntimeError, match="EXRADIUS_SECRET_KEY_PREVIOUS") as rotate_error:
        box.rotate(token)
    for error in (decrypt_error.value, rotate_error.value):
        assert "s3cret" not in str(error) and token not in str(error)
    with pytest.raises(RuntimeError):
        box.decrypt("not a fernet token")


def test_secret_box_rotate_re_encrypts_under_the_primary():
    primary, old = generate_secret_key(), generate_secret_key()
    box = SecretBox(primary, [old])

    rotated = box.rotate(SecretBox(old).encrypt("s3cret"))
    assert fernet_decrypt(primary, rotated) == "s3cret"
    # A value already under the primary is simply encrypted again with it.
    again = box.rotate(rotated)
    assert fernet_decrypt(primary, again) == "s3cret"


def test_secret_box_names_the_setting_that_holds_an_invalid_key_without_its_value():
    primary = generate_secret_key()

    with pytest.raises(RuntimeError, match="EXRADIUS_SECRET_KEY is not set"):
        SecretBox("")
    with pytest.raises(RuntimeError, match="^EXRADIUS_SECRET_KEY holds") as primary_error:
        SecretBox("not-a-fernet-key")
    assert "not-a-fernet-key" not in str(primary_error.value)
    with pytest.raises(RuntimeError, match="EXRADIUS_SECRET_KEY_PREVIOUS holds") as previous_error:
        SecretBox(primary, ["bad-previous-key"])
    assert "bad-previous-key" not in str(previous_error.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("   ", []),
        (",", []),
        ("A", ["A"]),
        ("A,B", ["A", "B"]),
        (" A , B ", ["A", "B"]),
        ("A,,B,", ["A", "B"]),
        ("\nA,\tB\n", ["A", "B"]),
    ],
)
def test_previous_keys_are_comma_separated_and_trimmed(raw, expected):
    assert Settings(secret_key=generate_secret_key(), secret_key_previous=raw).previous_secret_keys == expected


def test_previous_keys_are_read_from_the_environment(monkeypatch):
    primary, old, older = generate_secret_key(), generate_secret_key(), generate_secret_key()
    monkeypatch.setenv("EXRADIUS_SECRET_KEY", primary)
    monkeypatch.setenv("EXRADIUS_SECRET_KEY_PREVIOUS", f"{old}, {older}")

    settings = Settings()
    assert settings.secret_key == primary
    assert settings.previous_secret_keys == [old, older]
    box = SecretBox(settings.secret_key, settings.previous_secret_keys)
    assert box.decrypt(SecretBox(older).encrypt("x")) == "x"


def test_an_install_without_previous_keys_behaves_as_before():
    settings = Settings(secret_key=generate_secret_key())
    assert settings.previous_secret_keys == []
    assert settings.download_verify_keys == [settings.download_signing_key]


# rotate-key ----------------------------------------------------------------------------------


def test_rotation_finds_every_encrypted_column_by_its_suffix():
    found = {f"{table.name}.{column.name}" for table, columns in encrypted_columns().items() for column in columns}
    assert found == {
        "git_credentials.value_encrypted",
        "secret_store_entries.value_encrypted",
        "providers.credential_encrypted",
        "routing_settings.openai_key_encrypted",
    }


def seed(db_path: Path, key: str) -> dict[str, uuid.UUID]:
    """One value of each kind encrypted with ``key``, plus empty values that are skipped."""
    box = SecretBox(key)
    engine = create_engine(f"sqlite:///{db_path}")
    ids: dict[str, uuid.UUID] = {}
    with Session(engine) as session:
        store = SecretStore(name="billing")
        session.add(store)
        session.flush()
        entries = [
            SecretStoreEntry(store_id=store.id, key="DB_PASSWORD", value_encrypted=box.encrypt("db-pw")),
            SecretStoreEntry(store_id=store.id, key="STRIPE_KEY", value_encrypted=box.encrypt("sk_test")),
            SecretStoreEntry(store_id=store.id, key="EMPTY", value_encrypted=""),
        ]
        git = GitCredential(host="github.com", kind="token", value_encrypted=box.encrypt("ghp_x"))
        provider = Provider(slug="anthropic", name="Anthropic", kind="anthropic", auth_method="api_key",
                            credential_encrypted=box.encrypt("sk-ant-x"), is_default=True)
        routing = RoutingSettings(id=1, openai_key_encrypted=box.encrypt("sk-openai"))
        session.add_all([*entries, git, provider, routing])
        session.commit()
        ids = {"entry": entries[0].id, "stripe": entries[1].id, "empty": entries[2].id,
               "git": git.id, "provider": provider.id}
    engine.dispose()
    return ids


EXPECTED_COUNTS = {
    "git_credentials.value_encrypted": 1,
    "secret_store_entries.value_encrypted": 2,
    "providers.credential_encrypted": 1,
    "routing_settings.openai_key_encrypted": 1,
}

PLAINTEXTS = {"db-pw", "sk_test", "ghp_x", "sk-ant-x", "sk-openai"}


def stored(db_path: Path) -> dict[str, str | None]:
    """Every stored ciphertext, by a readable name."""
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        values: dict[str, str | None] = {}
        for entry in session.scalars(select(SecretStoreEntry)):
            values[f"entry:{entry.key}"] = entry.value_encrypted
        for git in session.scalars(select(GitCredential)):
            values[f"git:{git.host}"] = git.value_encrypted
        for provider in session.scalars(select(Provider)):
            values[f"provider:{provider.slug}"] = provider.credential_encrypted
        for routing in session.scalars(select(RoutingSettings)):
            values["routing"] = routing.openai_key_encrypted
    engine.dispose()
    return values


def decrypt_all(values: dict[str, str | None], key: str) -> dict[str, str | None]:
    box = SecretBox(key)  # the new key alone, no previous keys
    return {name: box.decrypt(token) if token else token for name, token in values.items()}


PLAIN = {
    "entry:DB_PASSWORD": "db-pw",
    "entry:STRIPE_KEY": "sk_test",
    "entry:EMPTY": "",
    "git:github.com": "ghp_x",
    "provider:anthropic": "sk-ant-x",
    "routing": "sk-openai",
}


def rotate_key_cli(db_path: Path, primary: str, previous: str = "") -> subprocess.CompletedProcess:
    """``exradius rotate-key`` as its own process, the way ``kubectl exec`` runs it."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("EXRADIUS_")}
    env.update(
        EXRADIUS_DATABASE_URL=f"sqlite+aiosqlite:///{db_path}",
        EXRADIUS_SECRET_KEY=primary,
        EXRADIUS_SECRET_KEY_PREVIOUS=previous,
    )
    script = Path(sys.executable).parent / "exradius"
    command = [str(script)] if script.exists() else [sys.executable, "-m", "exradius.cli"]
    return subprocess.run([*command, "rotate-key"], env=env, capture_output=True, text=True, timeout=120)


def counts_of(output: str) -> dict[str, int]:
    return {name: int(count) for name, count in (line.rsplit(": ", 1) for line in output.splitlines())}


async def test_rotate_encrypted_re_encrypts_every_value_with_the_primary(db_path, sessions):
    old, new = generate_secret_key(), generate_secret_key()
    seed(db_path, old)

    async with sessions() as session, session.begin():
        counts = await rotate_encrypted(session, SecretBox(new, [old]))

    assert counts == EXPECTED_COUNTS
    values = stored(db_path)
    assert values["entry:EMPTY"] == ""  # an empty value stays empty and is not counted
    assert decrypt_all(values, new) == PLAIN


async def test_rotate_encrypted_counts_values_already_under_the_primary(db_path, sessions):
    old, new = generate_secret_key(), generate_secret_key()
    seed(db_path, old)
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:  # one value was written by a pod that already has the new key
        git = session.scalar(select(GitCredential))
        git.value_encrypted = SecretBox(new).encrypt("ghp_new")
        session.commit()
    engine.dispose()

    async with sessions() as session, session.begin():
        counts = await rotate_encrypted(session, SecretBox(new, [old]))

    assert counts == EXPECTED_COUNTS
    assert decrypt_all(stored(db_path), new) == {**PLAIN, "git:github.com": "ghp_new"}


async def test_rotate_encrypted_reports_zero_for_columns_without_values(db_path, sessions):
    async with sessions() as session, session.begin():
        counts = await rotate_encrypted(session, SecretBox(generate_secret_key()))
    assert counts == dict.fromkeys(EXPECTED_COUNTS, 0)


async def test_rotate_encrypted_names_the_row_it_cannot_decrypt(db_path, sessions):
    old, new, unknown = generate_secret_key(), generate_secret_key(), generate_secret_key()
    ids = seed(db_path, old)
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        session.get(SecretStoreEntry, ids["stripe"]).value_encrypted = SecretBox(unknown).encrypt("sk_test")
        session.commit()
    engine.dispose()

    with pytest.raises(RuntimeError) as error:
        async with sessions() as session, session.begin():
            await rotate_encrypted(session, SecretBox(new, [old]))

    message = str(error.value)
    assert message.startswith(f"secret_store_entries.value_encrypted (id={ids['stripe']})")
    assert not any(plain in message for plain in PLAINTEXTS)


def test_rotate_key_command_re_encrypts_and_prints_counts_only(db_path):
    old, new = generate_secret_key(), generate_secret_key()
    seed(db_path, old)

    result = rotate_key_cli(db_path, new, old)

    assert result.returncode == 0, result.stderr
    assert counts_of(result.stdout) == EXPECTED_COUNTS
    assert "every stored secret is encrypted with EXRADIUS_SECRET_KEY" in result.stderr
    for plain in PLAINTEXTS:
        assert plain not in result.stdout + result.stderr
    # After the rotation every value decrypts with the new key alone.
    assert decrypt_all(stored(db_path), new) == PLAIN


def test_rotate_key_command_changes_nothing_when_one_value_cannot_be_decrypted(db_path):
    old, new, unknown = generate_secret_key(), generate_secret_key(), generate_secret_key()
    ids = seed(db_path, old)
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:  # the last table rotate-key visits, after all the others
        assert list(encrypted_columns())[-1].name == "secret_store_entries"
        session.get(SecretStoreEntry, ids["stripe"]).value_encrypted = SecretBox(unknown).encrypt("sk_test")
        session.commit()
    engine.dispose()
    before = stored(db_path)

    result = rotate_key_cli(db_path, new, old)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "nothing was changed" in result.stderr
    assert f"secret_store_entries.value_encrypted (id={ids['stripe']})" in result.stderr
    for plain in PLAINTEXTS:
        assert plain not in result.stderr
    # Every row still holds its old ciphertext, the ones visited before the failure included.
    assert stored(db_path) == before
    assert decrypt_all({k: v for k, v in before.items() if k != "entry:STRIPE_KEY"}, old) == {
        k: v for k, v in PLAIN.items() if k != "entry:STRIPE_KEY"
    }


def test_rotate_key_command_fails_without_the_previous_key_and_changes_nothing(db_path):
    """Step 4 of §10.1: removing the old key too early is caught, not half applied."""
    old, new = generate_secret_key(), generate_secret_key()
    seed(db_path, old)
    before = stored(db_path)

    result = rotate_key_cli(db_path, new)

    assert result.returncode == 1
    assert "nothing was changed" in result.stderr
    assert stored(db_path) == before


def test_rotate_key_command_is_safe_to_run_again(db_path):
    old, new = generate_secret_key(), generate_secret_key()
    seed(db_path, old)

    first = rotate_key_cli(db_path, new, old)
    assert first.returncode == 0, first.stderr
    after_first = stored(db_path)

    second = rotate_key_cli(db_path, new, old)
    assert second.returncode == 0, second.stderr
    assert counts_of(second.stdout) == counts_of(first.stdout) == EXPECTED_COUNTS
    assert decrypt_all(stored(db_path), new) == decrypt_all(after_first, new) == PLAIN

    # And again once the old key has been removed (§10.1 step 4's "run rotate-key once more").
    third = rotate_key_cli(db_path, new)
    assert third.returncode == 0, third.stderr
    assert counts_of(third.stdout) == EXPECTED_COUNTS
    assert decrypt_all(stored(db_path), new) == PLAIN


def test_rotate_key_command_rejects_an_invalid_key_without_printing_it(db_path):
    old = generate_secret_key()
    seed(db_path, old)
    before = stored(db_path)

    bad_primary = rotate_key_cli(db_path, "not-a-fernet-key", old)
    assert bad_primary.returncode == 1
    assert "EXRADIUS_SECRET_KEY holds a value that is not a Fernet key" in bad_primary.stderr
    assert "not-a-fernet-key" not in bad_primary.stderr

    bad_previous = rotate_key_cli(db_path, generate_secret_key(), f"{old},bad-previous-key")
    assert bad_previous.returncode == 1
    assert "EXRADIUS_SECRET_KEY_PREVIOUS holds a value that is not a Fernet key" in bad_previous.stderr
    assert "bad-previous-key" not in bad_previous.stderr and old not in bad_previous.stderr

    missing = rotate_key_cli(db_path, "", old)
    assert missing.returncode == 1
    assert "EXRADIUS_SECRET_KEY is not set" in missing.stderr

    assert stored(db_path) == before


# Download signatures across a rotation -------------------------------------------------------


def settings_with(base: Settings, primary: str, previous: str = "") -> Settings:
    return base.model_copy(update={"secret_key": primary, "secret_key_previous": previous})


def test_download_links_survive_a_rotation_until_the_old_key_is_removed(settings, db_path):
    old, new = generate_secret_key(), generate_secret_key()
    path = downloads.artifact_path(uuid.uuid4())
    before = settings_with(settings, old)
    during = settings_with(settings, new, old)
    after = settings_with(settings, new)

    old_sig = downloads.sign(before, path)
    new_sig = downloads.sign(during, path)
    assert old_sig != new_sig
    assert downloads.sign(after, path) == new_sig  # signed with the primary only

    # Signed before the rotation: verifies while the old key is a previous key...
    assert downloads.verify(before, path, old_sig)
    assert downloads.verify(during, path, old_sig)
    # ...also after rotate-key, which changes no key setting...
    seed(db_path, old)
    result = rotate_key_cli(db_path, new, old)
    assert result.returncode == 0, result.stderr
    assert downloads.verify(during, path, old_sig)
    # ...and stops once the old key is removed.
    assert not downloads.verify(after, path, old_sig)

    # Signed under the new primary: verifies throughout, never under the old key alone.
    assert downloads.verify(during, path, new_sig)
    assert downloads.verify(after, path, new_sig)
    assert not downloads.verify(before, path, new_sig)

    # A signature stays bound to its path whatever the key.
    other = downloads.transcript_path(uuid.uuid4())
    assert not downloads.verify(during, other, old_sig)
    assert not downloads.verify(during, other, new_sig)
    assert not downloads.verify(during, path, None)
    assert not downloads.verify(during, path, "")


def test_signed_urls_use_the_primary_key(settings):
    old, new = generate_secret_key(), generate_secret_key()
    run_id = uuid.uuid4()
    url = downloads.transcript_url(settings_with(settings, new, old), run_id)
    assert url.endswith(f"?sig={downloads.sign(settings_with(settings, new), downloads.transcript_path(run_id))}")


def get_transcript(settings: Settings, store, url: str) -> int:
    """The status of ``GET url`` without a session, from an api started with ``settings``."""
    app = create_app(settings, store=store, broker=FakeControl())
    with TestClient(app) as client:
        client.portal.call(_startup_done, app)
        return client.get(url).status_code


def test_download_links_signed_with_a_removed_key_answer_401(settings, store):
    old, new = generate_secret_key(), generate_secret_key()
    run_id = uuid.uuid4()  # no such run: a 404 means the signature was accepted
    path = downloads.transcript_path(run_id)
    old_url = f"{path}?sig={downloads.sign(settings_with(settings, old), path)}"
    new_url = f"{path}?sig={downloads.sign(settings_with(settings, new), path)}"

    assert get_transcript(settings_with(settings, new, old), store, old_url) == 404
    assert get_transcript(settings_with(settings, new, old), store, new_url) == 404
    assert get_transcript(settings_with(settings, new), store, old_url) == 401
    assert get_transcript(settings_with(settings, new), store, new_url) == 404
