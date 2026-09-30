"""Contract identity (pure): OCC-style option symbols, normalized and cross-checked.

``contract_id = ROOT + YYMMDD + C|P + strike x 1000 as 8 digits`` (for example ``META261016C00700000``). The
provider symbol is kept verbatim beside it; an optional ``O:`` prefix (Polygon/Massive) is accepted.

The OCC root is part of the identity because (underlying, expiration, type, strike) is not globally unique:
after corporate actions, adjusted contracts (e.g. root ``META1``, non-100 deliverables) can share those four values
with a standard contract. A root that differs from the underlying is recorded (``root_matches_underlying``), not
rejected.
"""
from datetime import date
from decimal import Decimal
import re

OCC = re.compile(r"(?:O:)?([A-Z][A-Z0-9.]{0,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})")
TYPE_CODE = {"C": "call", "P": "put"}
TYPE_LETTER = {v: k for k, v in TYPE_CODE.items()}


class IdentityError(ValueError):
    """The symbol is not a parseable OCC-style option symbol."""


def parse(provider_symbol):
    """(root, expiration date, option_type, strike Decimal) from an OCC-style symbol; IdentityError otherwise."""
    if not isinstance(provider_symbol, str):
        raise IdentityError("option symbol must be a string")
    match = OCC.fullmatch(provider_symbol.strip())
    if not match:
        raise IdentityError("option symbol is not OCC-style")
    root, yy, mm, dd, code, strike = match.groups()
    try:
        expiration = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise IdentityError("option symbol has an impossible expiration date") from None
    return root, expiration, TYPE_CODE[code], Decimal(strike) / 1000


def contract_id(root, expiration, option_type, strike):
    """The normalized id. The strike must be representable in thousandths (the OCC encoding)."""
    thousandths = strike * 1000
    if thousandths != thousandths.to_integral_value() or not 0 < thousandths < 10 ** 8:
        raise IdentityError("strike is not representable in the OCC encoding")
    return f"{root}{expiration:%y%m%d}{TYPE_LETTER[option_type]}{int(thousandths):08d}"
