"""Daily-rotating chirp pseudonyms (board card c390).

WHAT THIS TRADES AWAY, stated plainly because the whole board is built on the
opposite property. Until now a chirp carried no author signal at all: two chirps
by one person were indistinguishable (SPEC 8.3). braul asked for Reddit/Yik Yak
style names, was given the three options with their costs, and chose the middle
one: a name that identifies a person WITHIN A DAY and changes the next day. So
this deliberately gives up same-day unlinkability and keeps everything longer.

TWO THINGS DECIDE WHETHER THIS IS SAFE, and both are easy to get backwards.

1. KEYED ON THE CHIRP'S CREATION DAY, NEVER ON TODAY. Computing the name from the
   current date looks equivalent and is strictly worse than the stable pseudonym
   that was rejected: every one of an author's old chirps would re-label itself
   with today's name each morning, re-linking their entire history daily. Keyed on
   created_at, a day's chirps share a name and yesterday's keep yesterday's.

2. KEYED ON A STORED RANDOM SEED, NEVER ON THE USER ID. user ids are not secret
   to insiders - a chapter member can list their own chapter's roster - so
   hash(user_id, ...) would let them compute every chapter-mate's name for a day
   and read the board's authorship directly. That is the authorship-oracle shape
   c342 closed on blocks, reintroduced through a different door. The seed
   (users.pseudonym_seed, migration 0038) is random per user and not derivable
   from anything enumerable.

Scoped per campus as well as per day, so the same person on two campuses is not
correlatable across them.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone

# Neutral by construction. Nothing here may describe a person - no body, ethnicity,
# gender, age or ability words - because the label is attached to a real student's
# words and they did not choose it. Campus and nature nouns only (DESIGN 10.7 asks
# copy to name real things; these lean UNCG without naming anyone).
_ADJECTIVES: tuple[str, ...] = (
    "Amber", "Brisk", "Candid", "Chilly", "Copper", "Crisp", "Dapper", "Dusty",
    "Eager", "Frosty", "Gentle", "Golden", "Hazy", "Humble", "Idle", "Jolly",
    "Keen", "Lucky", "Mellow", "Nimble", "Quiet", "Rustic", "Silver", "Sleepy",
    "Snowy", "Solar", "Steady", "Sunny", "Tidy", "Velvet", "Wandering", "Witty",
)

_NOUNS: tuple[str, ...] = (
    "Acorn", "Aspen", "Beacon", "Birch", "Bridge", "Cedar", "Clover", "Comet",
    "Ember", "Fern", "Foxglove", "Harbor", "Heron", "Ivy", "Juniper", "Lantern",
    "Magnolia", "Maple", "Meadow", "Oak", "Orchard", "Pebble", "Quarry", "River",
    "Spartan", "Sparrow", "Sycamore", "Thicket", "Trellis", "Willow", "Wren", "Yarrow",
)

# Two digits on the end. 32 x 32 = 1024 name pairs alone, and on a campus with a
# few dozen people posting in one day the birthday paradox makes collisions likely -
# two strangers sharing a name for a day is not a privacy problem but it does read
# as one person arguing with themselves. x100 makes it negligible.
_SUFFIX_MODULUS = 100


def daily_pseudonym(seed: str, campus_id: uuid.UUID, created_at: datetime) -> str:
    """The label shown for one chirp: stable per (author, campus, UTC day)."""
    # A naive datetime here would be a bug, not a nuisance: .date() on one would
    # silently use whatever wall clock the row was built with. Everything from the
    # database is timezone-aware; assume UTC only if something else slips through.
    moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    day = moment.astimezone(timezone.utc).date().isoformat()

    # The UTC day boundary is a deliberate simplification: campuses carry no
    # timezone, so a US campus sees its names roll over during the evening rather
    # than at local midnight. Harmless - a rotation instant is arbitrary - but it is
    # a choice, not an oversight, and the fix is a campus timezone column.
    digest = hmac.new(
        seed.encode("utf-8"),
        f"{campus_id}:{day}".encode("utf-8"),
        hashlib.sha256,
    ).digest()

    adjective = _ADJECTIVES[int.from_bytes(digest[0:4], "big") % len(_ADJECTIVES)]
    noun = _NOUNS[int.from_bytes(digest[4:8], "big") % len(_NOUNS)]
    suffix = int.from_bytes(digest[8:12], "big") % _SUFFIX_MODULUS
    return f"{adjective}-{noun}-{suffix:02d}"
