"""Names and paths. Every path is derived from a validated closed kind and a validated id, never from input text.

``<root>/<kind>/<64 lowercase hex>.json``: the hex is the object's ``sha256`` content id. Ids are exactly
``sha256:`` plus 64 lowercase hex digits; mixed case, other prefixes or lengths are rejected.
"""
import re

from artifact_store.errors import InvalidQuery, StoreUnavailable
from artifact_store.kinds import KINDS

ARTIFACT_ID = re.compile(r"sha256:[0-9a-f]{64}")
FILE_NAME = re.compile(r"([0-9a-f]{64})\.json")
TEMP_PREFIX, TEMP_SUFFIX = ".artifact-", ".tmp"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def kind_dir(kind):
    if kind not in KINDS:
        raise InvalidQuery("unknown artifact kind")
    return kind                                       # the directory name is the closed kind name itself


def checked_id(artifact_id):
    if not (isinstance(artifact_id, str) and ARTIFACT_ID.fullmatch(artifact_id)):
        raise InvalidQuery("artifact id must be sha256: followed by 64 lowercase hex digits")
    return artifact_id


def file_name(artifact_id):
    return f"{checked_id(artifact_id)[7:]}.json"


def is_temp(name):
    return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)


def checked_root(root):
    import os
    if not (isinstance(root, str) and os.path.isabs(root) and "\x00" not in root):
        raise StoreUnavailable("artifact root must be an absolute path")
    return root
