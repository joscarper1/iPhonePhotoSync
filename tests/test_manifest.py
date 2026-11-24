import pytest

from iphonephotosync.database.manifest import Manifest, ManifestError
from iphonephotosync.domain.models import (
    DeviceFile, InvalidTransition, ItemStatus, SessionStatus, VerifyStatus,
)

UDID = "SIM-1"
F = DeviceFile("/DCIM/100APPLE/IMG_0001.HEIC", 100, 555)
SHA = "a" * 64


@pytest.fixture
def mf(tmp_path):
    m = Manifest(tmp_path / "_Manifest" / "manifest.sqlite")
    yield m
    m.close()


def discover(mf, f=F):
    sid = mf.start_session(UDID)
    iid, created = mf.upsert_discovered(sid, UDID, f)
    return sid, iid, created


def to_verified(mf, iid, sha=SHA):
    mf.transition(iid, ItemStatus.STAGED, sha256=sha, sha1_source="b" * 40)
    mf.transition(iid, ItemStatus.VERIFIED, verify_status=VerifyStatus.VERIFIED_FULL)


def test_schema_created_with_backup_id_and_version(mf):
    assert mf.get_meta("schema_version") == "2"
    assert len(mf.backup_id) == 36


def test_backup_id_persists_across_reopen(tmp_path):
    p = tmp_path / "m.sqlite"
    with Manifest(p) as a:
        bid = a.backup_id
    with Manifest(p) as b:
        assert b.backup_id == bid


def test_upsert_is_idempotent_and_fills_mime(mf):
    sid, iid, created = discover(mf)
    assert created
    iid2, created2 = mf.upsert_discovered(sid, UDID, F)
    assert (iid2, created2) == (iid, False)
    row = mf.get_item(iid)
    assert row["mime"] == "image/heic" and row["copy_status"] == "discovered"
    assert row["verify_status"] == "not_verified" and row["original_name"] == "IMG_0001.HEIC"


def test_same_name_different_folder_are_distinct_items(mf):
    sid = mf.start_session(UDID)
    a, _ = mf.upsert_discovered(sid, UDID, DeviceFile("/DCIM/100APPLE/IMG_0002.JPG", 5, 1))
    b, _ = mf.upsert_discovered(sid, UDID, DeviceFile("/DCIM/101APPLE/IMG_0002.JPG", 5, 1))
    assert a != b


def test_changed_content_same_path_is_new_item(mf):
    sid = mf.start_session(UDID)
    a, _ = mf.upsert_discovered(sid, UDID, DeviceFile("/DCIM/100APPLE/IMG_9.JPG", 5, 1))
    b, created = mf.upsert_discovered(sid, UDID, DeviceFile("/DCIM/100APPLE/IMG_9.JPG", 6, 2))
    assert created and a != b


def test_cannot_skip_states(mf):
    _, iid, _ = discover(mf)
    with pytest.raises(InvalidTransition):
        mf.transition(iid, ItemStatus.COMMITTED, sha256=SHA, rel_path="2024/x.heic",
                      verify_status=VerifyStatus.VERIFIED_FULL)
    assert mf.get_item(iid)["copy_status"] == "discovered"   # sin cambios tras el rechazo


def test_commit_requires_positive_verification(mf):
    _, iid, _ = discover(mf)
    mf.transition(iid, ItemStatus.STAGED, sha256=SHA)
    mf.transition(iid, ItemStatus.VERIFIED)  # verify_status sigue en not_verified
    with pytest.raises(ManifestError):
        mf.transition(iid, ItemStatus.COMMITTED, rel_path="2024/01 - Enero/x.heic")
    assert mf.get_item(iid)["copy_status"] == "verified"


def test_commit_requires_sha_and_path(mf):
    _, iid, _ = discover(mf)
    to_verified(mf, iid)
    with pytest.raises(ManifestError):
        mf.transition(iid, ItemStatus.COMMITTED)        # falta rel_path
    mf.transition(iid, ItemStatus.COMMITTED, rel_path="2024/01 - Enero/Sin ubicación/IMG_0001.HEIC",
                  final_name="IMG_0001.HEIC", capture_dt="2024-01-05T10:00:00", date_source="exif")
    row = mf.get_item(iid)
    assert row["copy_status"] == "committed" and row["imported_at"] and row["sha256"] == SHA
    assert mf.find_committed_by_sha256(SHA)["id"] == iid


def test_committed_is_final(mf):
    _, iid, _ = discover(mf)
    to_verified(mf, iid)
    mf.transition(iid, ItemStatus.COMMITTED, rel_path="x/y.heic")
    for s in (ItemStatus.DISCOVERED, ItemStatus.FAILED):
        with pytest.raises(InvalidTransition):
            mf.transition(iid, s)


def test_unknown_field_rejected(mf):
    _, iid, _ = discover(mf)
    with pytest.raises(ManifestError):
        mf.transition(iid, ItemStatus.FAILED, copy_status="committed")


def test_failed_then_retry_keeps_error_message(mf):
    _, iid, _ = discover(mf)
    mf.transition(iid, ItemStatus.FAILED, error="Dispositivo desconectado")
    assert mf.get_item(iid)["error"] == "Dispositivo desconectado"
    mf.transition(iid, ItemStatus.DISCOVERED)


def test_recover_interrupted_session(tmp_path):
    p = tmp_path / "m.sqlite"
    with Manifest(p) as m:
        sid = m.start_session(UDID)
        a, _ = m.upsert_discovered(sid, UDID, F)
        b, _ = m.upsert_discovered(sid, UDID, DeviceFile("/DCIM/100APPLE/IMG_2.HEIC", 7, 9))
        c, _ = m.upsert_discovered(sid, UDID, DeviceFile("/DCIM/100APPLE/IMG_3.HEIC", 8, 9))
        to_verified(m, a, "c" * 64)
        m.transition(a, ItemStatus.COMMITTED, rel_path="2024/a.heic")      # ya respaldado
        m.transition(b, ItemStatus.STAGED, sha256="d" * 64)                # a medias
        # c queda en DISCOVERED; se "cae" el proceso aquí (sin finish_session)
    with Manifest(p) as m:
        assert m.recover_interrupted() == 1
        assert m.get_item(a)["copy_status"] == "committed"                 # no se toca lo válido
        assert m.get_item(b)["copy_status"] == "discovered" and m.get_item(b)["sha256"] is None
        assert m.get_item(c)["copy_status"] == "discovered"
        status = m._conn.execute("SELECT status FROM sessions").fetchone()["status"]
        assert status == SessionStatus.INTERRUPTED.value


def test_recover_leaves_verified_items_for_reconciliation(tmp_path):
    p = tmp_path / "m.sqlite"
    with Manifest(p) as m:
        sid = m.start_session(UDID)
        a, _ = m.upsert_discovered(sid, UDID, F)
        to_verified(m, a)
        m.set_fields(a, rel_path="2024/x.heic", final_name="x.heic")
    with Manifest(p) as m:
        assert m.recover_interrupted() == 0
        assert [r["id"] for r in m.pending_commit_items()] == [a]
        assert m.get_item(a)["rel_path"] == "2024/x.heic"
        m.reset_to_discovered(a)
        row = m.get_item(a)
        assert row["copy_status"] == "discovered" and row["rel_path"] is None and row["sha256"] is None


def test_set_fields_does_not_change_status(mf):
    _, iid, _ = discover(mf)
    mf.set_fields(iid, city="Barcelona", gps_lat=41.4)
    row = mf.get_item(iid)
    assert row["copy_status"] == "discovered" and row["city"] == "Barcelona"
    with pytest.raises(ManifestError):
        mf.set_fields(iid, copy_status="committed")


def test_transaction_rolls_back_on_error(mf):
    sid = mf.start_session(UDID)
    with pytest.raises(RuntimeError):
        with mf.transaction():
            mf.upsert_discovered(sid, UDID, F)
            raise RuntimeError("fallo")
    assert mf.counts_by_status() == {}


def test_counts_by_status(mf):
    _, iid, _ = discover(mf)
    mf.transition(iid, ItemStatus.FAILED, error="x")
    assert mf.counts_by_status() == {"failed": 1}
