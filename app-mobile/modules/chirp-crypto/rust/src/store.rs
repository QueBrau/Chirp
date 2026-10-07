//! Encrypted state store: one SQLite file per account namespace.
//!
//! # Records
//! Every record is `(rtype, rid, gen, tag, nonce, ct)`. `ct` is ChaCha20-Poly1305
//! under the 32-byte store key with a fresh random 96-bit nonce, and the AAD binds
//!
//! ```text
//! "chirp-e2ee-v1/store-record" || user_id || generation(u32) ||
//!   lp(rtype) || lp(rid) || lp(tag) || gen(u64)
//! ```
//!
//! so a record copied from another namespace, to another position, or under another
//! store generation does not authenticate. `rid` and `tag` are either constants or
//! keyed hashes (`idx`) of identifiers, so the file does not reveal which peers or
//! conversations exist. Wrong store key, renamed file and tampering all surface as
//! the same fixed `UnlockFailed` / `StoreCorrupt`.
//!
//! # Store generation and rollback
//! A header record (itself AEAD-protected) carries a monotonically increasing store
//! generation `store_gen`, bumped by every commit, plus
//!  - a manifest `(rtype, rid) -> gen` of every MUTABLE record (account, sessions,
//!    trust, key map, outbox...), so an older copy of one of those cannot be swapped
//!    in: its `gen` would not equal the manifest's;
//!  - a count of every APPEND-ONLY record type (messages, replay marks), so rows
//!    cannot be silently dropped.
//!
//! Limit, stated plainly: restoring an OLDER WHOLE FILE is self-consistent and cannot
//! be detected without a counter stored outside the file. `store_generation()` is
//! exposed so the platform layer can pin it in the Keychain/Keystore later.
//!
//! # Transactions
//! Every state change is staged in a [`Tx`] and applied by [`Store::commit`] as ONE
//! SQLite transaction (records + new header). Nothing is cached in memory between
//! operations except the header mirror, which is only updated after a successful
//! COMMIT, so the in-memory view is always either the fully old or fully new state.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

use crate::envelope::hex;
use crate::error::{Error, Result};
use crate::types::Namespace;

pub(crate) const T_HDR: &str = "hdr";
pub(crate) const T_ACCOUNT: &str = "acct";
pub(crate) const T_DEVICE: &str = "dev";
pub(crate) const T_KEYS: &str = "keys";
pub(crate) const T_SESSION: &str = "sess";
pub(crate) const T_TRUST: &str = "trust";
pub(crate) const T_OUTBOX: &str = "outbox";
pub(crate) const T_MESSAGE: &str = "msg";
pub(crate) const T_REPLAY_CIPHER: &str = "rplc";
pub(crate) const T_REPLAY_MESSAGE: &str = "rplm";

const MUTABLE: [&str; 6] = [T_ACCOUNT, T_DEVICE, T_KEYS, T_SESSION, T_TRUST, T_OUTBOX];
const APPEND_ONLY: [&str; 3] = [T_MESSAGE, T_REPLAY_CIPHER, T_REPLAY_MESSAGE];

const FORMAT_VERSION: i64 = 1;
const HEADER_RID: &[u8] = b"\x00";
const AAD_DOMAIN: &[u8] = b"chirp-e2ee-v1/store-record";
const INDEX_DOMAIN: &[u8] = b"chirp-e2ee-v1/index-key";

type RecordId = Vec<u8>;
/// `(gen, tag, nonce, ct)` as stored.
type RawRow = (i64, Option<Vec<u8>>, Vec<u8>, Vec<u8>);

fn is_mutable(rtype: &str) -> bool {
    MUTABLE.contains(&rtype)
}

/// Where a simulated crash is injected inside [`Store::commit`] (tests only).
#[cfg(test)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FailPoint {
    /// After the first record is written, before the rest.
    MidTransaction,
    /// All records and the header written, COMMIT not issued.
    BeforeCommit,
    /// COMMIT succeeded, but the call fails before returning to the caller.
    AfterCommit,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct Header {
    v: i64,
    store_gen: u64,
    next_seq: u64,
    /// `"rtype:hex(rid)" -> gen` for every mutable record.
    mutable: BTreeMap<String, u64>,
    /// `rtype -> row count` for every append-only record type.
    counts: BTreeMap<String, u64>,
}

fn manifest_key(rtype: &str, rid: &[u8]) -> String {
    format!("{rtype}:{}", hex(rid))
}

pub(crate) struct Put {
    pub rtype: &'static str,
    pub rid: Vec<u8>,
    pub tag: Option<Vec<u8>>,
    pub value: Zeroizing<Vec<u8>>,
}

/// A staged set of record writes. Nothing touches disk until [`Store::commit`].
#[derive(Default)]
pub(crate) struct Tx {
    pub(crate) puts: Vec<Put>,
    pub(crate) next_seq: Option<u64>,
}

impl Tx {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.puts.is_empty() && self.next_seq.is_none()
    }

    /// Stage a write. Mutable records replace an earlier staged write of the same key;
    /// staging an append-only key twice is a programming error.
    pub(crate) fn put(
        &mut self,
        rtype: &'static str,
        rid: Vec<u8>,
        tag: Option<Vec<u8>>,
        value: Zeroizing<Vec<u8>>,
    ) -> Result<()> {
        if let Some(existing) = self
            .puts
            .iter_mut()
            .find(|p| p.rtype == rtype && p.rid == rid)
        {
            if !is_mutable(rtype) {
                return Err(Error::Internal);
            }
            existing.tag = tag;
            existing.value = value;
            return Ok(());
        }
        self.puts.push(Put {
            rtype,
            rid,
            tag,
            value,
        });
        Ok(())
    }
}

pub(crate) struct Store {
    conn: Connection,
    cipher: ChaCha20Poly1305,
    idx_key: Zeroizing<[u8; 32]>,
    ns: Namespace,
    header: Header,
    path: PathBuf,
    #[cfg(test)]
    pub(crate) fail: Option<FailPoint>,
}

/// File name for a namespace inside the caller's directory.
pub(crate) fn namespace_path(dir: &Path, ns: &Namespace) -> PathBuf {
    dir.join(format!(
        "chirp-e2ee-{}-g{}.db",
        hex(&ns.user_id),
        ns.generation
    ))
}

fn lp(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u32).to_be_bytes());
    out.extend_from_slice(bytes);
}

fn aad(ns: &Namespace, rtype: &str, rid: &[u8], tag: Option<&[u8]>, gen_: u64) -> Vec<u8> {
    let mut out = Vec::with_capacity(AAD_DOMAIN.len() + 64 + rtype.len() + rid.len());
    out.extend_from_slice(AAD_DOMAIN);
    out.extend_from_slice(&ns.user_id);
    out.extend_from_slice(&ns.generation.to_be_bytes());
    lp(&mut out, rtype.as_bytes());
    lp(&mut out, rid);
    lp(&mut out, tag.unwrap_or(&[]));
    out.extend_from_slice(&gen_.to_be_bytes());
    out
}

fn map_open_error(e: rusqlite::Error) -> Error {
    use rusqlite::ErrorCode;
    match &e {
        rusqlite::Error::SqliteFailure(f, _)
            if matches!(f.code, ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked) =>
        {
            Error::StoreInUse
        }
        rusqlite::Error::SqliteFailure(f, _)
            if matches!(f.code, ErrorCode::NotADatabase | ErrorCode::DatabaseCorrupt) =>
        {
            Error::UnlockFailed
        }
        _ => Error::Storage,
    }
}

impl Store {
    /// Open (or create) the namespace's store. Any integrity failure, including a wrong
    /// key, is `UnlockFailed`; a second opener of the same file is `StoreInUse`.
    pub(crate) fn open(dir: &Path, ns: Namespace, key: &[u8; 32]) -> Result<Store> {
        create_private_dir(dir)?;
        let path = namespace_path(dir, &ns);
        let conn = Connection::open(&path).map_err(|_| Error::Storage)?;
        restrict_file_mode(&path);
        conn.busy_timeout(std::time::Duration::ZERO)
            .map_err(|_| Error::Storage)?;
        conn.execute_batch(
            "PRAGMA locking_mode=EXCLUSIVE; PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;",
        )
        .map_err(map_open_error)?;
        // First write transaction takes the exclusive lock for the connection's life.
        conn.execute_batch("BEGIN EXCLUSIVE")
            .map_err(map_open_error)?;

        let cipher = ChaCha20Poly1305::new(&Key::from(*key));
        let idx_key = {
            let mut h = Sha256::new();
            h.update(INDEX_DOMAIN);
            h.update(key);
            Zeroizing::new(<[u8; 32]>::from(h.finalize()))
        };
        let mut store = Store {
            conn,
            cipher,
            idx_key,
            ns,
            header: Header::default(),
            path,
            #[cfg(test)]
            fail: None,
        };
        match store.init_or_load() {
            Ok(()) => {
                store
                    .conn
                    .execute_batch("COMMIT")
                    .map_err(|_| Error::Storage)?;
                Ok(store)
            }
            Err(e) => {
                let _ = store.conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    fn init_or_load(&mut self) -> Result<()> {
        let version: i64 = self
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .map_err(map_open_error)?;
        let tables: i64 = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='records'",
                [],
                |r| r.get(0),
            )
            .map_err(map_open_error)?;
        if version == 0 && tables == 0 {
            // Brand new namespace.
            self.conn
                .execute_batch(
                    "CREATE TABLE records (
                        rtype TEXT NOT NULL,
                        rid BLOB NOT NULL,
                        gen INTEGER NOT NULL,
                        tag BLOB,
                        nonce BLOB NOT NULL,
                        ct BLOB NOT NULL,
                        PRIMARY KEY (rtype, rid)
                     ) WITHOUT ROWID;
                     CREATE INDEX records_by_tag ON records (rtype, tag, rid);",
                )
                .map_err(|_| Error::Storage)?;
            self.conn
                .execute_batch(&format!("PRAGMA user_version={FORMAT_VERSION}"))
                .map_err(|_| Error::Storage)?;
            let header = Header {
                v: FORMAT_VERSION,
                ..Header::default()
            };
            self.write_header(&header)?;
            self.header = header;
            return Ok(());
        }
        if version != FORMAT_VERSION || tables != 1 {
            return Err(Error::UnlockFailed);
        }
        let header = self.read_header()?;
        self.verify_against_rows(&header)?;
        self.header = header;
        Ok(())
    }

    fn write_header(&self, header: &Header) -> Result<()> {
        let plain = Zeroizing::new(serde_json::to_vec(header).map_err(|_| Error::Internal)?);
        let aad = aad(&self.ns, T_HDR, HEADER_RID, None, header.store_gen);
        let (nonce, ct) = self.seal(&aad, &plain)?;
        self.conn
            .execute(
                "INSERT OR REPLACE INTO records (rtype, rid, gen, tag, nonce, ct)
                 VALUES (?1, ?2, ?3, NULL, ?4, ?5)",
                params![T_HDR, HEADER_RID, header.store_gen as i64, &nonce[..], ct],
            )
            .map_err(|_| Error::Storage)?;
        Ok(())
    }

    fn read_header(&self) -> Result<Header> {
        let row: Option<(i64, Vec<u8>, Vec<u8>)> = self
            .conn
            .query_row(
                "SELECT gen, nonce, ct FROM records WHERE rtype=?1 AND rid=?2",
                params![T_HDR, HEADER_RID],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(map_open_error)?;
        let (gen_, nonce, ct) = row.ok_or(Error::UnlockFailed)?;
        let gen_ = u64::try_from(gen_).map_err(|_| Error::UnlockFailed)?;
        let aad = aad(&self.ns, T_HDR, HEADER_RID, None, gen_);
        let plain = self
            .open_ct(&aad, &nonce, &ct)
            .map_err(|_| Error::UnlockFailed)?;
        let header: Header = serde_json::from_slice(&plain).map_err(|_| Error::UnlockFailed)?;
        if header.v != FORMAT_VERSION || header.store_gen != gen_ {
            return Err(Error::UnlockFailed);
        }
        Ok(header)
    }

    /// Cheap structural check (no decryption): the rows on disk match the header's
    /// manifest of mutable records and counts of append-only records.
    fn verify_against_rows(&self, header: &Header) -> Result<()> {
        let mut seen_mutable: BTreeMap<String, u64> = BTreeMap::new();
        let mut counts: BTreeMap<String, u64> = BTreeMap::new();
        let mut stmt = self
            .conn
            .prepare("SELECT rtype, rid, gen FROM records")
            .map_err(|_| Error::Storage)?;
        let mut rows = stmt.query([]).map_err(|_| Error::Storage)?;
        while let Some(row) = rows.next().map_err(|_| Error::Storage)? {
            let rtype: String = row.get(0).map_err(|_| Error::Storage)?;
            let rid: Vec<u8> = row.get(1).map_err(|_| Error::Storage)?;
            let gen_: i64 = row.get(2).map_err(|_| Error::Storage)?;
            let gen_ = u64::try_from(gen_).map_err(|_| Error::StoreCorrupt)?;
            if rtype == T_HDR {
                continue;
            }
            if is_mutable(&rtype) {
                seen_mutable.insert(manifest_key(&rtype, &rid), gen_);
            } else if APPEND_ONLY.contains(&rtype.as_str()) {
                if gen_ > header.store_gen {
                    return Err(Error::StoreCorrupt);
                }
                *counts.entry(rtype).or_default() += 1;
            } else {
                return Err(Error::StoreCorrupt);
            }
        }
        let expected_counts: BTreeMap<String, u64> = header
            .counts
            .iter()
            .filter(|(_, n)| **n > 0)
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        if seen_mutable != header.mutable || counts != expected_counts {
            return Err(Error::StoreCorrupt);
        }
        Ok(())
    }

    fn seal(&self, aad: &[u8], plaintext: &[u8]) -> Result<([u8; 12], Vec<u8>)> {
        let mut nonce = [0u8; 12];
        getrandom::fill(&mut nonce).map_err(|_| Error::Internal)?;
        let ct = self
            .cipher
            .encrypt(
                &Nonce::from(nonce),
                Payload {
                    msg: plaintext,
                    aad,
                },
            )
            .map_err(|_| Error::Internal)?;
        Ok((nonce, ct))
    }

    fn open_ct(&self, aad: &[u8], nonce: &[u8], ct: &[u8]) -> Result<Zeroizing<Vec<u8>>> {
        let nonce: [u8; 12] = nonce.try_into().map_err(|_| Error::StoreCorrupt)?;
        self.cipher
            .decrypt(&Nonce::from(nonce), Payload { msg: ct, aad })
            .map(Zeroizing::new)
            .map_err(|_| Error::StoreCorrupt)
    }

    /// Keyed hash used for record ids and tags: `SHA-256(idx_key || lp(label) || data)`.
    pub(crate) fn idx(&self, label: &str, data: &[u8]) -> Vec<u8> {
        let mut h = Sha256::new();
        h.update(&self.idx_key[..]);
        h.update((label.len() as u32).to_be_bytes());
        h.update(label.as_bytes());
        h.update(data);
        h.finalize().to_vec()
    }

    /// Run raw SQL on the live connection (tests only: provoke genuine SQLite failures).
    #[cfg(test)]
    pub(crate) fn exec_for_test(&self, sql: &str) {
        self.conn.execute_batch(sql).unwrap();
    }

    pub(crate) fn namespace(&self) -> Namespace {
        self.ns
    }

    pub(crate) fn store_generation(&self) -> u64 {
        self.header.store_gen
    }

    pub(crate) fn next_seq(&self) -> u64 {
        self.header.next_seq
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }

    /// Enforce the version policy for a record that was just authenticated.
    fn check_version(&self, rtype: &str, rid: &[u8], gen_: u64) -> Result<()> {
        if is_mutable(rtype) {
            if self.header.mutable.get(&manifest_key(rtype, rid)) == Some(&gen_) {
                Ok(())
            } else {
                Err(Error::StoreCorrupt)
            }
        } else if gen_ <= self.header.store_gen {
            Ok(())
        } else {
            Err(Error::StoreCorrupt)
        }
    }

    fn decode_row(
        &self,
        rtype: &str,
        rid: &[u8],
        gen_: i64,
        tag: Option<Vec<u8>>,
        nonce: &[u8],
        ct: &[u8],
    ) -> Result<Zeroizing<Vec<u8>>> {
        let gen_ = u64::try_from(gen_).map_err(|_| Error::StoreCorrupt)?;
        let aad = aad(&self.ns, rtype, rid, tag.as_deref(), gen_);
        let plain = self.open_ct(&aad, nonce, ct)?;
        self.check_version(rtype, rid, gen_)?;
        Ok(plain)
    }

    /// Load one record, or `None` if it does not exist AND the manifest agrees.
    pub(crate) fn get(&self, rtype: &str, rid: &[u8]) -> Result<Option<Zeroizing<Vec<u8>>>> {
        let row: Option<RawRow> = self
            .conn
            .query_row(
                "SELECT gen, tag, nonce, ct FROM records WHERE rtype=?1 AND rid=?2",
                params![rtype, rid],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        match row {
            Some((gen_, tag, nonce, ct)) => {
                Ok(Some(self.decode_row(rtype, rid, gen_, tag, &nonce, &ct)?))
            }
            None => {
                if is_mutable(rtype) && self.header.mutable.contains_key(&manifest_key(rtype, rid))
                {
                    Err(Error::StoreCorrupt)
                } else {
                    Ok(None)
                }
            }
        }
    }

    pub(crate) fn exists(&self, rtype: &str, rid: &[u8]) -> Result<bool> {
        let found: Option<i64> = self
            .conn
            .query_row(
                "SELECT 1 FROM records WHERE rtype=?1 AND rid=?2",
                params![rtype, rid],
                |r| r.get(0),
            )
            .optional()?;
        Ok(found.is_some())
    }

    /// All records of a type with the given tag (e.g. every session for one peer device).
    pub(crate) fn get_tagged(
        &self,
        rtype: &str,
        tag: &[u8],
    ) -> Result<Vec<(RecordId, Zeroizing<Vec<u8>>)>> {
        let mut stmt = self.conn.prepare(
            "SELECT rid, gen, nonce, ct FROM records WHERE rtype=?1 AND tag=?2 ORDER BY rid",
        )?;
        let rows = stmt.query_map(params![rtype, tag], |r| {
            Ok((
                r.get::<_, Vec<u8>>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, Vec<u8>>(2)?,
                r.get::<_, Vec<u8>>(3)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (rid, gen_, nonce, ct) = row?;
            let plain = self.decode_row(rtype, &rid, gen_, Some(tag.to_vec()), &nonce, &ct)?;
            out.push((rid, plain));
        }
        Ok(out)
    }

    /// Newest-first page of an append-only type: tag match, `rid < before` (if given).
    pub(crate) fn page_desc(
        &self,
        rtype: &str,
        tag: &[u8],
        before_rid: Option<&[u8]>,
        limit: usize,
    ) -> Result<Vec<Zeroizing<Vec<u8>>>> {
        let upper: Vec<u8> = before_rid
            .map(|b| b.to_vec())
            .unwrap_or_else(|| vec![0xff; 16]);
        let mut stmt = self.conn.prepare(
            "SELECT rid, gen, nonce, ct FROM records
             WHERE rtype=?1 AND tag=?2 AND rid < ?3 ORDER BY rid DESC LIMIT ?4",
        )?;
        let rows = stmt.query_map(params![rtype, tag, upper, limit as i64], |r| {
            Ok((
                r.get::<_, Vec<u8>>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, Vec<u8>>(2)?,
                r.get::<_, Vec<u8>>(3)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (rid, gen_, nonce, ct) = row?;
            out.push(self.decode_row(rtype, &rid, gen_, Some(tag.to_vec()), &nonce, &ct)?);
        }
        Ok(out)
    }

    /// Apply a staged transaction atomically: all records plus the new header, or
    /// nothing. The in-memory header only moves after COMMIT succeeds.
    pub(crate) fn commit(&mut self, tx: Tx) -> Result<()> {
        if tx.is_empty() {
            return Ok(());
        }
        let new_gen = self
            .header
            .store_gen
            .checked_add(1)
            .ok_or(Error::Internal)?;
        let mut header = self.header.clone();
        header.store_gen = new_gen;
        if let Some(seq) = tx.next_seq {
            if seq < header.next_seq {
                return Err(Error::Internal);
            }
            header.next_seq = seq;
        }
        for put in &tx.puts {
            if is_mutable(put.rtype) {
                header
                    .mutable
                    .insert(manifest_key(put.rtype, &put.rid), new_gen);
            } else {
                *header.counts.entry(put.rtype.to_owned()).or_default() += 1;
            }
        }

        // Seal everything first (borrows `self` immutably), then run the SQL transaction.
        let mut sealed = Vec::with_capacity(tx.puts.len());
        for put in &tx.puts {
            let aad = aad(&self.ns, put.rtype, &put.rid, put.tag.as_deref(), new_gen);
            sealed.push(self.seal(&aad, &put.value)?);
        }
        let header_sealed = {
            let plain = Zeroizing::new(serde_json::to_vec(&header).map_err(|_| Error::Internal)?);
            let aad = aad(&self.ns, T_HDR, HEADER_RID, None, new_gen);
            self.seal(&aad, &plain)?
        };

        // `Transaction` rolls back on drop, which is exactly the crash-before-commit story.
        #[cfg(test)]
        let fail = self.fail;
        let sql = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        for (i, (put, (nonce, ct))) in tx.puts.iter().zip(sealed.iter()).enumerate() {
            let verb = if is_mutable(put.rtype) {
                "INSERT OR REPLACE"
            } else {
                "INSERT"
            };
            sql.execute(
                &format!(
                    "{verb} INTO records (rtype, rid, gen, tag, nonce, ct)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)"
                ),
                params![put.rtype, put.rid, new_gen as i64, put.tag, &nonce[..], ct],
            )?;
            #[cfg(test)]
            if i == 0 && fail == Some(FailPoint::MidTransaction) {
                drop(sql);
                self.fail = None;
                return Err(Error::Storage);
            }
            let _ = i;
        }
        sql.execute(
            "INSERT OR REPLACE INTO records (rtype, rid, gen, tag, nonce, ct)
             VALUES (?1, ?2, ?3, NULL, ?4, ?5)",
            params![
                T_HDR,
                HEADER_RID,
                new_gen as i64,
                &header_sealed.0[..],
                header_sealed.1
            ],
        )?;
        #[cfg(test)]
        if fail == Some(FailPoint::BeforeCommit) {
            drop(sql);
            self.fail = None;
            return Err(Error::Storage);
        }
        sql.commit()?;
        self.header = header;
        #[cfg(test)]
        if fail == Some(FailPoint::AfterCommit) {
            self.fail = None;
            return Err(Error::Storage);
        }
        Ok(())
    }
}

/// Owner-only file mode, best effort (the directory is already private; this is a second
/// line of defense and failing to apply it is not worth refusing to open the store).
#[cfg(unix)]
fn restrict_file_mode(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_file_mode(_path: &Path) {}

#[cfg(unix)]
fn create_private_dir(dir: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .map_err(|_| Error::Storage)
}

#[cfg(not(unix))]
fn create_private_dir(dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir).map_err(|_| Error::Storage)
}

/// Delete a namespace's files (database and rollback journal). Needs no key: the
/// platform layer deletes the store key separately, which is the real erasure.
pub(crate) fn remove_namespace_files(dir: &Path, ns: &Namespace) -> Result<()> {
    let path = namespace_path(dir, ns);
    let mut journal = path.clone().into_os_string();
    journal.push("-journal");
    for p in [path, PathBuf::from(journal)] {
        match std::fs::remove_file(&p) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(Error::Storage),
        }
    }
    Ok(())
}
